from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.error import BadRequest

from app.services.state_repository import JsonStateRepository
from app.services.telegram_transport import TelegramTransportService
from app.services.telegram_ui_scope import TelegramUiKey
from config import DefaultsConfig, TelegramConfig
from sessions.session_ui import SessionUI
from tg.rich import build_rich_message_payload


def keyboard(data="sess_active"):
    return InlineKeyboardMarkup([[InlineKeyboardButton('Выбрать <& "проект">', callback_data=data)]])


def transport(tmp_path):
    app = SimpleNamespace(state_repository=JsonStateRepository(str(tmp_path / "state.sqlite3")))
    service = TelegramTransportService(app)
    app._send_message = service.send_message
    calls = []

    async def post(endpoint, *, data):
        calls.append((endpoint, data))
        return SimpleNamespace(message_id=100 + len(calls))

    context = SimpleNamespace(bot=SimpleNamespace(_post=post, send_message=AsyncMock()))
    return service, context, calls


def test_embedded_buttons_escape_text_and_payload():
    payload = build_rich_message_payload("**Проект**", keyboard('pick:"&'))
    markdown = payload["rich_message"]["markdown"]
    assert 'data="pick:&quot;&amp;"' in markdown
    assert 'Выбрать &lt;&amp; &quot;проект&quot;&gt;' in markdown
    assert payload["reply_markup"] is None
    assert markdown.startswith("**Проект**")


def test_webapp_keyboard_is_preserved():
    markup = ReplyKeyboardMarkup([[KeyboardButton("Открыть", web_app=WebAppInfo("https://example.test"))]])
    assert build_rich_message_payload("Форма", markup)["reply_markup"] is markup


@pytest.mark.asyncio
async def test_menu_reuses_message_after_restart_and_separates_topics(tmp_path):
    service, context, calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, message_thread_id=11, text="Первый экран", reply_markup=keyboard())
    restarted = TelegramTransportService(service.bot_app)
    await restarted.send_menu(context, chat_id=1, message_thread_id=11, text="Второй экран", reply_markup=keyboard("sess_list"))
    await restarted.send_menu(context, chat_id=1, message_thread_id=12, text="Другая тема", reply_markup=keyboard())
    assert [endpoint for endpoint, _ in calls] == ["sendRichMessage", "editMessageText", "sendRichMessage"]
    assert calls[1][1]["message_id"] == first.message_id
    assert calls[2][1]["message_thread_id"] == 12
    assert restarted.menu_panel(TelegramUiKey(1, 11))["callbacks"] == ["sess_list"]


@pytest.mark.asyncio
async def test_deleted_menu_is_replaced_and_close_reopens_new_panel(tmp_path):
    service, context, calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    original = context.bot._post

    async def post(endpoint, *, data):
        if endpoint == "editMessageText":
            raise BadRequest("Message to edit not found")
        return await original(endpoint, data=data)

    context.bot._post = post
    replacement = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    assert replacement.message_id != first.message_id
    service.remember_menu_edit(1, replacement.message_id, None)
    reopened = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    assert reopened.message_id != replacement.message_id
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_rich_rejection_preserves_plain_keyboard(tmp_path):
    service, context, _calls = transport(tmp_path)
    context.bot._post = AsyncMock(side_effect=BadRequest("Rich messages unavailable"))
    context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=31))
    markup = keyboard()
    message = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=markup)
    assert message.message_id == 31
    assert context.bot.send_message.call_args.kwargs["reply_markup"] is markup


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["clearqueue", "reset", "close"])
async def test_destructive_actions_require_confirmation(tmp_path, action):
    session = SimpleNamespace(id="s1", name="Рабочая сессия", queue=["Запрос"], execution_backend="headless")
    manager = SimpleNamespace(get=lambda _chat, sid: session if sid == "s1" else None,
                              persist_session=MagicMock(return_value=True))
    app = SimpleNamespace(close_session_with_cleanup=AsyncMock(return_value=True))
    config = SimpleNamespace(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1]),
        defaults=DefaultsConfig(workdir=str(tmp_path), state_path=str(tmp_path / "state.db")),
    )
    edit = AsyncMock(return_value=True)
    ui = SessionUI(config, manager, AsyncMock(), edit, str, str, bot_app=app)
    ui._reset_session_fields = MagicMock()
    message = SimpleNamespace(chat_id=1, message_id=10)
    query = SimpleNamespace(data=f"sess_{action}:s1", message=message)
    await ui.handle_callback(query, 1, object())
    assert session.queue == ["Запрос"]
    ui._reset_session_fields.assert_not_called()
    app.close_session_with_cleanup.assert_not_awaited()
    markup = edit.call_args.kwargs["reply_markup"]
    confirmation = markup.inline_keyboard[0][0]
    assert confirmation.callback_data == f"sess_{action}:s1:confirm"
    assert confirmation.to_dict()["style"] == "danger"
    assert "Рабочая сессия" in edit.call_args.kwargs["text"]
    query.data = confirmation.callback_data
    await ui.handle_callback(query, 1, object())
    if action == "clearqueue":
        assert session.queue == []
    elif action == "reset":
        ui._reset_session_fields.assert_called_once_with(session, owner_chat_id=1)
    else:
        app.close_session_with_cleanup.assert_awaited_once()
        assert app.close_session_with_cleanup.call_args.args[:2] == ("s1", 1)


@pytest.mark.asyncio
async def test_regular_prompts_keep_removable_inline_buttons(tmp_path):
    service, context, calls = transport(tmp_path)
    markup = keyboard("discard_input:request")
    await service.send_message(context, chat_id=1, text="Подтвердите ввод", reply_markup=markup)
    assert calls[0][1]["reply_markup"] is markup
    assert "<tg-button" not in calls[0][1]["rich_message"]["markdown"]
    await service.edit_message_outcome(context, 1, 101, "Подтвердите ввод", reply_markup=markup)
    assert calls[1][1]["reply_markup"] is markup
    assert "<tg-button" not in calls[1][1]["rich_message"]["markdown"]
