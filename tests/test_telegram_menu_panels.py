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

    context = SimpleNamespace(bot=SimpleNamespace(_post=post, send_message=AsyncMock(), delete_message=AsyncMock()))
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
async def test_menu_replaces_message_after_restart_and_separates_topics(tmp_path):
    service, context, calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, message_thread_id=11, text="Первый экран", reply_markup=keyboard())
    restarted = TelegramTransportService(service.bot_app)

    async def delete_previous(**kwargs):
        assert len(calls) == 1
        assert kwargs == {"chat_id": 1, "message_id": first.message_id}

    context.bot.delete_message.side_effect = delete_previous
    replacement = await restarted.send_menu(
        context, chat_id=1, message_thread_id=11, text="Второй экран", reply_markup=keyboard("sess_list"),
    )
    await restarted.send_menu(context, chat_id=1, message_thread_id=12, text="Другая тема", reply_markup=keyboard())
    assert [endpoint for endpoint, _ in calls] == ["sendRichMessage"] * 3
    context.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=first.message_id)
    assert replacement.message_id != first.message_id
    assert calls[2][1]["message_thread_id"] == 12
    assert restarted.menu_panel(TelegramUiKey(1, 11))["callbacks"] == ["sess_list"]


@pytest.mark.asyncio
async def test_deleted_menu_is_replaced_and_close_reopens_new_panel(tmp_path):
    service, context, calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    context.bot.delete_message.side_effect = BadRequest("Message to delete not found")
    replacement = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    assert replacement.message_id != first.message_id
    service.remember_menu_edit(1, replacement.message_id, None)
    reopened = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    assert reopened.message_id != replacement.message_id
    assert len(calls) == 3
    assert all(endpoint == "sendRichMessage" for endpoint, _ in calls)


@pytest.mark.asyncio
async def test_old_menu_is_closed_when_telegram_refuses_deletion(tmp_path):
    service, context, calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    context.bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    replacement = await service.send_menu(context, chat_id=1, text="Новое меню", reply_markup=keyboard("sess_list"))
    assert [endpoint for endpoint, _ in calls] == ["sendRichMessage", "editMessageText", "sendRichMessage"]
    assert calls[1][1]["message_id"] == first.message_id
    assert "<tg-button" not in calls[1][1]["rich_message"]["markdown"]
    assert "Меню закрыто" in calls[1][1]["rich_message"]["markdown"]
    assert service.menu_panel(TelegramUiKey(1, None))["message_id"] == replacement.message_id


@pytest.mark.asyncio
async def test_navigation_edits_current_menu_without_replacing_it(tmp_path):
    service, context, calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    markup = keyboard("sess_list")
    await service.edit_message_outcome(context, 1, first.message_id, "Список сессий", reply_markup=markup)
    service.remember_menu_edit(1, first.message_id, markup)
    context.bot.delete_message.assert_not_awaited()
    assert [endpoint for endpoint, _ in calls] == ["sendRichMessage", "editMessageText"]
    assert service.menu_panel(TelegramUiKey(1, None))["message_id"] == first.message_id
    assert service.menu_panel(TelegramUiKey(1, None))["callbacks"] == ["sess_list"]


@pytest.mark.asyncio
async def test_closed_menu_stays_inactive_when_new_message_fails(tmp_path):
    service, context, _calls = transport(tmp_path)
    first = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    service.bot_app._send_message = AsyncMock(return_value=None)
    assert await service.send_menu(context, chat_id=1, text="Новое меню", reply_markup=keyboard()) is None
    context.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=first.message_id)
    assert service.menu_panel(TelegramUiKey(1, None))["callbacks"] == []


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
    assert edit.call_args.kwargs["reply_markup"] is None
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


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["session", "common", "git"])
@pytest.mark.parametrize("keep_buttons", [False, True])
async def test_callback_result_closes_rich_menu_unless_keyboard_is_explicit(tmp_path, owner, keep_buttons):
    from bot import BotApp
    from app.services.git_ops_service import GitOps
    from tg.callbacks import CallbackHandler

    service, context, calls = transport(tmp_path)
    app = service.bot_app
    app.transport_service = service

    async def edit(*args, **kwargs):
        return await BotApp._edit_message(app, *args, **kwargs)

    app._edit_message = edit
    message = await service.send_menu(context, chat_id=1, text="Меню", reply_markup=keyboard())
    data = {"session": "sess_status:s1", "common": "file_pick:0", "git": "git_status"}[owner]
    query = SimpleNamespace(data=data, message=SimpleNamespace(chat_id=1, message_id=message.message_id))
    markup = keyboard("sess_list") if keep_buttons else None
    if owner == "common":
        await CallbackHandler._edit_msg(SimpleNamespace(bot_app=app), context, query, "Результат", reply_markup=markup)
    else:
        cls = SessionUI if owner == "session" else GitOps
        await cls._edit_msg(SimpleNamespace(_edit_message=edit), context, query, "Результат", reply_markup=markup)
    assert calls[-1][0] == "editMessageText"
    markdown = calls[-1][1]["rich_message"]["markdown"]
    assert ("<tg-button" in markdown) is keep_buttons
    assert service.menu_panel(TelegramUiKey(1, None))["callbacks"] == (["sess_list"] if keep_buttons else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("queue", [[], ["Запрос"]])
async def test_queue_result_closes_menu(tmp_path, queue):
    session = SimpleNamespace(id="s1", queue=queue)
    manager = SimpleNamespace(get=lambda *_args: session)
    config = SimpleNamespace(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1]),
        defaults=DefaultsConfig(workdir=str(tmp_path), state_path=str(tmp_path / "state.db")),
    )
    edit = AsyncMock(return_value=True)
    ui = SessionUI(config, manager, AsyncMock(), edit, str, str)
    query = SimpleNamespace(data="sess_queue:s1", message=SimpleNamespace(chat_id=1, message_id=10))
    await ui.handle_callback(query, 1, object())
    assert edit.call_args.kwargs["reply_markup"] is None
