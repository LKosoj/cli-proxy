from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState
from tg.message_processor import MessageProcessor


class _AccessPolicy:
    async def ensure_allowed(self, _chat_id, _context) -> bool:
        return True


class _SessionUI:
    async def handle_pending_message(self, _chat_id, _text, _context, *, message_thread_id=None) -> bool:
        _ = message_thread_id
        return False


class _Metrics:
    def inc(self, _name: str) -> None:
        return None


def _text_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        document=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
        video_note=None,
    )


@pytest.mark.asyncio
async def test_pending_git_commit_cancel_message_is_handled_before_cli_input() -> None:
    git_calls: list[dict[str, object]] = []
    buffer_calls: list[str] = []

    class _Git:
        async def handle_pending_commit_message(self, chat_id, text, _context, *, message_thread_id=None) -> bool:
            git_calls.append(
                {
                    "chat_id": int(chat_id),
                    "text": str(text),
                    "message_thread_id": message_thread_id,
                }
            )
            return str(text).strip() == "-"

    async def _buffer_or_send(*_args, **_kwargs):
        buffer_calls.append("called")

    bot_app = SimpleNamespace(
        access_policy_service=_AccessPolicy(),
        context_by_chat={},
        metrics=_Metrics(),
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
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1001),
        effective_user=SimpleNamespace(id=9001),
        message=_text_message("-"),
    )

    await processor.process_message(update, context=object())

    assert git_calls == [
        {
            "chat_id": 1001,
            "text": "-",
            "message_thread_id": None,
        }
    ]
    assert buffer_calls == []


@pytest.mark.asyncio
async def test_plugin_awaiting_input_does_not_swallow_regular_message() -> None:
    buffer_calls: list[dict] = []
    cancel_calls: list[int] = []
    resolve_calls: list[dict[str, int | None]] = []
    ensure_calls: list[dict[str, int | None]] = []
    session = SimpleNamespace(id="s1")

    async def _legacy_ensure_active_session(*_args, **_kwargs):
        raise AssertionError("legacy ensure_active_session should not be used")

    def _resolve_scope_session(*, reply_chat_id: int, message_thread_id=None, owner_chat_id=None):
        resolve_calls.append(
            {
                "reply_chat_id": int(reply_chat_id),
                "message_thread_id": message_thread_id,
                "owner_chat_id": int(owner_chat_id or reply_chat_id),
            }
        )
        return session

    async def _ensure_scope_session(_chat_id, _context, *, reply_chat_id=None, message_thread_id=None):
        ensure_calls.append(
            {
                "reply_chat_id": int(reply_chat_id if reply_chat_id is not None else _chat_id),
                "message_thread_id": message_thread_id,
                "owner_chat_id": int(_chat_id),
            }
        )
        return session

    async def _buffer_or_send(_session, text, chat_id, _context, user_id=None):
        buffer_calls.append(
            {
                "session": _session,
                "text": text,
                "chat_id": int(chat_id),
                "user_id": user_id,
            }
        )

    ui_state = ChatUiState()

    bot_app = SimpleNamespace(
        access_policy_service=_AccessPolicy(),
        context_by_chat={},
        metrics=_Metrics(),
        session_ui=_SessionUI(),
        ui_state=ui_state,
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: False,
        _plugin_awaiting_input=lambda _chat_id: True,
        manager=SimpleNamespace(
            get_by_uid=lambda _session_uid: None,
            get_single_session_for_chat=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy get_single_session_for_chat should not be used")
            ),
            active=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy active fallback should not be used")
            ),
        ),
        _mode_allows_plugin_ui=lambda _session: True,
        _cancel_plugin_dialogs=lambda chat_id: cancel_calls.append(int(chat_id)),
        resolve_telegram_scope_session=_resolve_scope_session,
        ensure_scope_session=_ensure_scope_session,
        ensure_active_session=_legacy_ensure_active_session,
        _buffer_or_send=_buffer_or_send,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
    )
    processor = MessageProcessor(bot_app)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1001),
        effective_user=SimpleNamespace(id=9001),
        message=_text_message("hello"),
    )

    await processor.process_message(update, context=object())

    assert len(buffer_calls) == 1
    assert buffer_calls[0]["session"] is session
    assert buffer_calls[0]["text"] == "hello"
    assert buffer_calls[0]["chat_id"] == 1001
    assert buffer_calls[0]["user_id"] == 9001
    assert cancel_calls == []
    assert resolve_calls == [
        {
            "reply_chat_id": 1001,
            "message_thread_id": None,
            "owner_chat_id": 1001,
        }
    ]
    assert ensure_calls == [
        {
            "reply_chat_id": 1001,
            "message_thread_id": None,
            "owner_chat_id": 1001,
        }
    ]


@pytest.mark.asyncio
async def test_button_only_pending_question_does_not_block_active_session_input() -> None:
    sent_messages: list[str] = []
    buffer_calls: list[dict] = []
    ensure_calls: list[dict[str, int | None]] = []
    session = SimpleNamespace(id="s1")

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        assert int(chat_id) == 1001
        sent_messages.append(str(text))

    async def _legacy_ensure_active_session(*_args, **_kwargs):
        raise AssertionError("legacy ensure_active_session should not be used")

    async def _ensure_scope_session(_chat_id, _context, *, reply_chat_id=None, message_thread_id=None):
        ensure_calls.append(
            {
                "reply_chat_id": int(reply_chat_id if reply_chat_id is not None else _chat_id),
                "message_thread_id": message_thread_id,
                "owner_chat_id": int(_chat_id),
            }
        )
        return session

    async def _buffer_or_send(_session, text, chat_id, _context, user_id=None):
        buffer_calls.append(
            {
                "session": _session,
                "text": text,
                "chat_id": int(chat_id),
                "user_id": user_id,
            }
        )

    ui_state = ChatUiState()
    ui_state.active_ask_question_by_chat[TelegramUiKey.from_parts(1001)] = "q1"
    ui_state.pending_questions["q1"] = {
        "chat_id": 1001,
        "session_id": "s7",
        "allow_custom": False,
        "options": ["Продолжить остановленный план", "Начать новый план", "Отмена"],
    }

    bot_app = SimpleNamespace(
        access_policy_service=_AccessPolicy(),
        context_by_chat={},
        metrics=_Metrics(),
        session_ui=_SessionUI(),
        ui_state=ui_state,
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: False,
        _send_message=_send_message,
        _plugin_awaiting_input=lambda _chat_id: False,
        manager=SimpleNamespace(
            get_by_uid=lambda _session_uid: None,
            get_single_session_for_chat=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy get_single_session_for_chat should not be used")
            ),
            active=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy active fallback should not be used")
            ),
        ),
        _mode_allows_plugin_ui=lambda _session: True,
        _cancel_plugin_dialogs=lambda _chat_id: None,
        ensure_scope_session=_ensure_scope_session,
        ensure_active_session=_legacy_ensure_active_session,
        _buffer_or_send=_buffer_or_send,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
    )
    processor = MessageProcessor(bot_app)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1001),
        effective_user=SimpleNamespace(id=9001),
        message=_text_message("продолжить"),
    )

    await processor.process_message(update, context=object())

    assert sent_messages == []
    assert len(buffer_calls) == 1
    assert buffer_calls[0]["session"] is session
    assert buffer_calls[0]["text"] == "продолжить"
    assert ensure_calls == [
        {
            "reply_chat_id": 1001,
            "message_thread_id": None,
            "owner_chat_id": 1001,
        }
    ]


@pytest.mark.asyncio
async def test_custom_pending_question_supports_cancel_message() -> None:
    sent_messages: list[str] = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        assert int(chat_id) == 1001
        sent_messages.append(str(text))

    bot_app = SimpleNamespace(
        access_policy_service=_AccessPolicy(),
        context_by_chat={},
        metrics=_Metrics(),
        session_ui=_SessionUI(),
        ui_state=ChatUiState(),
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: True,
        _pop_pending_custom_input_status=lambda _chat_id, *, message_thread_id=None: "cancelled",
        _send_message=_send_message,
        _plugin_awaiting_input=lambda _chat_id: False,
        manager=SimpleNamespace(get_by_uid=lambda _session_uid: None),
        _mode_allows_plugin_ui=lambda _session: True,
        _cancel_plugin_dialogs=lambda _chat_id: None,
        _buffer_or_send=lambda *_args, **_kwargs: None,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
    )
    processor = MessageProcessor(bot_app)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1001),
        effective_user=SimpleNamespace(id=9001),
        message=_text_message("отмена"),
    )

    await processor.process_message(update, context=object())

    assert sent_messages == ["Ок, ввод своего варианта отменен."]


@pytest.mark.asyncio
async def test_plugin_awaiting_input_prefers_route_session_uid_over_legacy_fallbacks() -> None:
    buffer_calls: list[dict] = []
    cancel_calls: list[int] = []
    uid_calls: list[str] = []
    session = SimpleNamespace(id="s-uid")

    async def _authorize(_update, _context):
        return SimpleNamespace(
            reply_chat_id=1001,
            owner_chat_id=1001,
            message_thread_id=77,
            session_uid="thread:1001:77",
            session=None,
            reply_kwargs=lambda: {"chat_id": 1001, "message_thread_id": 77},
        )

    async def _legacy_ensure_active_session(*_args, **_kwargs):
        raise AssertionError("legacy ensure_active_session should not be used")

    async def _ensure_scope_session(*_args, **_kwargs):
        raise AssertionError("ensure_scope_session should not be used when session_uid is present")

    def _resolve_scope_session(*_args, **_kwargs):
        raise AssertionError("resolver should not be used when session_uid is present")

    def _get_by_uid(session_uid: str):
        uid_calls.append(str(session_uid))
        if str(session_uid) == "thread:1001:77":
            return session
        return None

    async def _buffer_or_send(_session, text, chat_id, _context, user_id=None):
        buffer_calls.append(
            {
                "session": _session,
                "text": text,
                "chat_id": int(chat_id),
                "user_id": user_id,
            }
        )

    ui_state = ChatUiState()

    bot_app = SimpleNamespace(
        ensure_telegram_inbound_authorized=_authorize,
        access_policy_service=_AccessPolicy(),
        context_by_chat={},
        metrics=_Metrics(),
        session_ui=_SessionUI(),
        ui_state=ui_state,
        _resolve_pending_custom_answer=lambda *_args, **_kwargs: False,
        _plugin_awaiting_input=lambda _chat_id: True,
        manager=SimpleNamespace(
            get_by_uid=_get_by_uid,
            get_single_session_for_chat=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy get_single_session_for_chat should not be used")
            ),
            active=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy active fallback should not be used")
            ),
        ),
        _mode_allows_plugin_ui=lambda _session: True,
        _cancel_plugin_dialogs=lambda chat_id: cancel_calls.append(int(chat_id)),
        ensure_scope_session=_ensure_scope_session,
        ensure_active_session=_legacy_ensure_active_session,
        resolve_telegram_scope_session=_resolve_scope_session,
        _buffer_or_send=_buffer_or_send,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
    )
    processor = MessageProcessor(bot_app)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1001),
        effective_user=SimpleNamespace(id=9001),
        message=_text_message("hello by uid"),
    )

    await processor.process_message(update, context=object())

    assert len(buffer_calls) == 1
    assert buffer_calls[0]["session"] is session
    assert buffer_calls[0]["text"] == "hello by uid"
    assert buffer_calls[0]["chat_id"] == 1001
    assert buffer_calls[0]["user_id"] == 9001
    assert uid_calls == ["thread:1001:77", "thread:1001:77"]
    assert cancel_calls == []
