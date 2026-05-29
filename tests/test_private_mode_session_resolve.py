"""Test that thread_mode=private resolves existing sessions even with thread_id=None.

Reproduces the bug: in private chat with thread_mode.mode='private',
thread_id is always None, which previously caused unknown_thread=True
for all messages — blocking commands for regular users even when they
had an existing session.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock


def _build_bot_app(*, thread_mode_enabled=True, thread_mode_mode="private",
                   topics_chat_id=None, sessions=None):
    """Build a minimal bot_app mock for resolve_telegram_inbound_route."""
    from bot import BotApp

    config = MagicMock()
    config.thread_mode.enabled = thread_mode_enabled
    config.thread_mode.mode = thread_mode_mode
    config.thread_mode.topics_chat_id = topics_chat_id

    bot_app = MagicMock(spec=BotApp)
    bot_app.config = config
    bot_app.session_thread_manager = None
    bot_app._extract_message_thread_id = BotApp._extract_message_thread_id
    bot_app._extract_direct_messages_topic_id = BotApp._extract_direct_messages_topic_id
    bot_app.resolve_telegram_inbound_route = BotApp.resolve_telegram_inbound_route.__get__(bot_app, BotApp)

    def resolve_session(*, reply_chat_id, message_thread_id=None, owner_chat_id=None):
        if sessions:
            for s in sessions.values():
                if int(getattr(s, "chat_id", 0)) == int(reply_chat_id):
                    return s
        return None

    bot_app.resolve_telegram_scope_session = resolve_session
    bot_app._route_has_any_sessions = lambda route: bool(sessions)
    return bot_app


def _make_update(chat_id=100, thread_id=None, direct_messages_topic_id=None):
    update = MagicMock()
    update.effective_chat.id = chat_id
    msg = MagicMock()
    msg.message_thread_id = thread_id
    if direct_messages_topic_id is None:
        msg.direct_messages_topic = None
    else:
        msg.direct_messages_topic.topic_id = direct_messages_topic_id
    msg.direct_message_topic = None
    update.effective_message = msg
    update.message = msg
    update.callback_query = None
    return update


def test_private_mode_no_session_gives_unknown_thread():
    """Without session, thread_id=None still gives unknown_thread=True."""
    bot_app = _build_bot_app(sessions=None)
    update = _make_update(chat_id=100)
    route = bot_app.resolve_telegram_inbound_route(update)
    assert route.unknown_thread is True
    assert route.session is None


def test_private_mode_with_session_resolves_normally():
    """With existing session for chat_id, thread_id=None should NOT be unknown_thread."""
    session = SimpleNamespace(
        id="s1",
        chat_id=100,
        conversation_scope=SimpleNamespace(session_uid="chat:100:s1"),
    )
    bot_app = _build_bot_app(sessions={"s1": session})
    update = _make_update(chat_id=100)
    route = bot_app.resolve_telegram_inbound_route(update)
    assert route.unknown_thread is False
    assert route.session is session
    assert route.session_uid == "chat:100:s1"


def test_private_mode_route_preserves_direct_messages_topic_id():
    session = SimpleNamespace(
        id="s1",
        chat_id=100,
        conversation_scope=SimpleNamespace(session_uid="chat:100:s1"),
    )
    bot_app = _build_bot_app(sessions={"s1": session})
    update = _make_update(chat_id=100, direct_messages_topic_id=1234567890123)
    route = bot_app.resolve_telegram_inbound_route(update)
    assert route.direct_messages_topic_id == 1234567890123
    assert route.reply_kwargs()["direct_messages_topic_id"] == 1234567890123


def test_private_mode_route_reads_direct_topic_id_from_api_kwargs():
    session = SimpleNamespace(
        id="s1",
        chat_id=100,
        conversation_scope=SimpleNamespace(session_uid="chat:100:s1"),
    )
    bot_app = _build_bot_app(sessions={"s1": session})
    update = _make_update(chat_id=100)
    update.effective_message.api_kwargs = {"direct_messages_topic_id": 777}
    route = bot_app.resolve_telegram_inbound_route(update)
    assert route.direct_messages_topic_id == 777
    assert route.reply_kwargs()["direct_messages_topic_id"] == 777


def test_private_mode_different_chat_no_match():
    """Session for different chat_id should not match."""
    session = SimpleNamespace(
        id="s1",
        chat_id=200,
        conversation_scope=SimpleNamespace(session_uid="chat:200:s1"),
    )
    bot_app = _build_bot_app(sessions={"s1": session})
    update = _make_update(chat_id=100)
    route = bot_app.resolve_telegram_inbound_route(update)
    assert route.unknown_thread is True
    assert route.session is None
