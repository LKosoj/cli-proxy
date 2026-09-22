from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.ext import MessageHandler, filters

from tg.message_processor import MessageProcessor
from tg.rich_message import RICH_MESSAGE, rich_message_text, rich_message_to_text
from tg.wiring import register_handlers


def _rich_update(blocks: list, *, text: str | None = None) -> Update:
    message = {
        "message_id": 5,
        "date": 1790000000,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 2, "is_bot": False, "first_name": "u"},
        "rich_message": {"blocks": blocks},
    }
    if text is not None:
        message["text"] = text
    return Update.de_json({"update_id": 1, "message": message}, bot=None)


def test_rich_message_flattens_blocks_to_plain_text() -> None:
    blocks = [
        {"type": "heading", "size": 2, "text": "Заголовок"},
        {
            "type": "paragraph",
            "text": [
                "почему ",
                {"type": "bold", "text": "заметки"},
                " обрезаются ",
                {"type": "custom_emoji", "custom_emoji_id": "1", "alternative_text": "🙂"},
                {"type": "url", "text": {"type": "italic", "text": " тут"}, "url": "https://x"},
                " и ",
                {"type": "url", "text": "https://y", "url": "https://y"},
                "?",
            ],
        },
        {"type": "pre", "language": "python", "text": "print(1)"},
        {"type": "list", "items": [{"label": "1.", "blocks": [{"type": "paragraph", "text": "первое"}]}]},
        {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "цитата\nдве строки"}]},
        {"type": "photo", "photo": [], "caption": {"text": "подпись"}},
        {"type": "table", "cells": [[{"text": "a"}, {"text": "b"}]]},
        {"type": "divider"},
        {"type": "mathematical_expression", "expression": "x^2"},
        {"type": "buttons", "buttons": []},
        {"type": "unknown_future_block", "text": "skip"},
        {"type": "pre", "text": ""},
        {"type": "list", "items": [{"label": "2.", "blocks": []}]},
    ]
    assert rich_message_to_text({"blocks": blocks}) == (
        "Заголовок\n\n"
        "почему заметки обрезаются 🙂 тут (https://x) и https://y?\n\n"
        "```python\nprint(1)\n```\n\n"
        "1. первое\n\n"
        "> цитата\n> две строки\n\n"
        "подпись\n\n"
        "a | b\n\n"
        "---\n\n"
        "x^2"
    )


def test_rich_message_text_is_none_for_ordinary_message() -> None:
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 5,
                "date": 1790000000,
                "chat": {"id": 1, "type": "private"},
                "text": "plain",
            },
        },
        bot=None,
    )
    assert rich_message_text(update.message) is None
    assert not RICH_MESSAGE.check_update(update)


def test_rich_message_filter_matches_only_when_text_is_absent() -> None:
    rich_only = _rich_update([{"type": "paragraph", "text": "hi"}])
    assert rich_message_text(rich_only.message) == "hi"
    assert RICH_MESSAGE.check_update(rich_only)
    assert not filters.TEXT.check_update(rich_only)

    with_text = _rich_update([{"type": "paragraph", "text": "hi"}], text="hi")
    assert not RICH_MESSAGE.check_update(with_text)
    assert filters.TEXT.check_update(with_text)


def test_register_handlers_routes_rich_messages_and_unsupported_ones(monkeypatch) -> None:
    class _App:
        def __init__(self) -> None:
            self.bot_data: dict[str, object] = {}
            self.handlers: list[tuple[object, int]] = []

        def add_handler(self, handler, group: int = 0) -> None:
            self.handlers.append((handler, int(group)))

    async def _on_message(*_args, **_kwargs):
        return None

    async def _on_unsupported(*_args, **_kwargs):
        return None

    bot_app = SimpleNamespace(
        access_policy_service=SimpleNamespace(is_allowed=lambda _chat_id: True),
        metrics=SimpleNamespace(inc=lambda _name: None),
        on_pre_command=_on_message,
        on_callback=_on_message,
        on_unknown_command=_on_message,
        on_photo=_on_message,
        on_document=_on_message,
        on_message=_on_message,
        on_unsupported_message=_on_unsupported,
    )
    monkeypatch.setattr("tg.wiring.build_command_registry", lambda _bot_app: [])
    monkeypatch.setattr("tg.wiring.install_plugin_handlers", lambda **_kwargs: None)
    app = _App()

    register_handlers(app=app, bot_app=bot_app, config=object())

    group0 = [handler for handler, group in app.handlers if group == 0 and isinstance(handler, MessageHandler)]
    rich = _rich_update([{"type": "paragraph", "text": "hi"}])
    matched = [handler for handler in group0 if handler.check_update(rich)]
    assert matched and matched[0].callback is _on_message

    voice = Update.de_json(
        {
            "update_id": 2,
            "message": {
                "message_id": 6,
                "date": 1790000000,
                "chat": {"id": 1, "type": "private"},
                "voice": {"file_id": "f", "file_unique_id": "u", "duration": 1},
            },
        },
        bot=None,
    )
    matched = [handler for handler in group0 if handler.check_update(voice)]
    assert matched and matched[0].callback is _on_unsupported
    # Последний обработчик группы 0 — заглушка; правки и служебные события она не ловит.
    assert group0[-1].callback is _on_unsupported
    edited = Update.de_json(
        {
            "update_id": 3,
            "edited_message": {
                "message_id": 6,
                "date": 1790000000,
                "chat": {"id": 1, "type": "private"},
                "voice": {"file_id": "f", "file_unique_id": "u", "duration": 1},
            },
        },
        bot=None,
    )
    pinned = Update.de_json(
        {
            "update_id": 4,
            "message": {
                "message_id": 8,
                "date": 1790000000,
                "chat": {"id": 1, "type": "private"},
                "pinned_message": {"message_id": 1, "date": 1790000000, "chat": {"id": 1, "type": "private"}, "text": "x"},
            },
        },
        bot=None,
    )
    assert not [handler for handler in group0 if handler.check_update(edited)]
    assert not [handler for handler in group0 if handler.check_update(pinned)]


@pytest.mark.asyncio
async def test_process_message_uses_rich_text_when_text_is_absent() -> None:
    buffered: list[str] = []

    async def _buffer_or_send(_session, text, *_args, **_kwargs):
        buffered.append(text)

    class _Session:
        id = "s1"

    class _AccessPolicy:
        async def ensure_allowed(self, _chat_id, _context) -> bool:
            return True

    class _Git:
        async def handle_pending_commit_message(self, *_args, **_kwargs) -> bool:
            return False

    class _SessionUI:
        async def handle_pending_message(self, *_args, **_kwargs) -> bool:
            return False

    from app.services.telegram_ui_scope import TelegramUiKey
    from app.services.ui_state_models import ChatUiState

    bot_app = SimpleNamespace(
        config=SimpleNamespace(
            telegram=SimpleNamespace(user_languages={}),
            defaults=SimpleNamespace(default_language="ru"),
        ),
        access_policy_service=_AccessPolicy(),
        metrics=SimpleNamespace(inc=lambda _name: None),
        git=_Git(),
        session_ui=_SessionUI(),
        ui_state=ChatUiState(),
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: False,
        _plugin_awaiting_input=lambda _chat_id: False,
        manager=SimpleNamespace(get_by_uid=lambda _session_uid: None),
        _mode_allows_plugin_ui=lambda _session: True,
        _cancel_plugin_dialogs=lambda _chat_id: None,
        _buffer_or_send=_buffer_or_send,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
    )
    processor = MessageProcessor(bot_app)

    async def _resolve_session(*_args, **_kwargs):
        return _Session()

    processor._resolve_session = _resolve_session
    update = _rich_update([{"type": "paragraph", "text": ["почему ", {"type": "bold", "text": "заметки"}, "?"]}])

    await processor.process_message(update, context=object())

    assert buffered == ["почему заметки?"]

    unknown_commands: list[object] = []

    async def _on_unknown_command(update, _context):
        unknown_commands.append(update)

    bot_app.on_unknown_command = _on_unknown_command
    await processor.process_message(_rich_update([{"type": "photo", "photo": []}]), context=object())
    await processor.process_message(
        _rich_update([{"type": "paragraph", "text": [{"type": "bot_command", "text": "/new"}, " x"]}]),
        context=object(),
    )

    assert buffered == ["почему заметки?"]
    assert len(unknown_commands) == 1


def test_log_unsupported_lists_content_and_unknown_fields(caplog) -> None:
    processor = MessageProcessor(SimpleNamespace())
    update = Update.de_json(
        {
            "update_id": 3,
            "message": {
                "message_id": 7,
                "date": 1790000000,
                "chat": {"id": 1, "type": "private"},
                "voice": {"file_id": "f", "file_unique_id": "u", "duration": 1},
                "brand_new_field": {"x": 1},
            },
        },
        bot=None,
    )
    with caplog.at_level(logging.INFO, logger="tg.message_processor"):
        processor.log_unsupported(update)
    expected = "inbound unsupported message chat_id=1 thread_id=None message_id=7 content=['voice'] api_kwargs=['brand_new_field']"
    assert any(expected in rec.getMessage() for rec in caplog.records)
