import types
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from sessions.session_status import build_session_status_text
from tg.handlers import BotHandlers
from tg.callbacks import CallbackHandler
from session import Session, ModeState


@pytest.fixture
def mock_session():
    s = MagicMock(spec=Session)
    s.id = "test-session"
    s.name = "Test Name"
    s.workdir = "/tmp/test"
    s.busy = False
    s.git = None
    s.git_busy = False
    s.git_conflict = False
    s.started_at = 1000
    s.last_output_ts = 1100
    s.last_tick_ts = 1150
    s.tick_seen = 5
    s.queue = []
    s.resume_token = None
    s.tool = types.SimpleNamespace(name="claude")
    s.active_cli = "claude"
    s.modes = ModeState(ssh_remote_enabled=False)
    s.project_root = None
    return s


def test_build_session_status_text_includes_ssh(mock_session):
    with patch("sessions.session_status.time.time", return_value=1200):
        # Case 1: No SSH hosts configured -> No SSH line
        with patch("sessions.session_status.load_ssh_config", return_value={}):
            status = build_session_status_text(mock_session)
            assert "🔗 SSH:" not in status

        # Case 2: SSH hosts exist but disabled
        mock_session.modes.ssh_remote_enabled = False
        with patch("sessions.session_status.load_ssh_config", return_value={"prod": {}}):
            status = build_session_status_text(mock_session)
            assert "🔗 SSH: выкл" in status

        # Case 3: Enabled with hosts
        mock_session.modes.ssh_remote_enabled = True
        with patch("sessions.session_status.load_ssh_config", return_value={"prod": {}, "staging": {}}):
            status = build_session_status_text(mock_session)
            assert "🔗 SSH: вкл (prod, staging)" in status


def test_ssh_remote_button_visibility(mock_session):
    handlers = BotHandlers(MagicMock())

    # No hosts -> No button
    with patch("tg.handlers.ssh_remote_available", return_value=False):
        btn = handlers._ssh_remote_button(mock_session)
        assert btn is None

    # Hosts exist -> Button visible
    with patch("tg.handlers.ssh_remote_available", return_value=True):
        # OFF state
        mock_session.modes.ssh_remote_enabled = False
        btn = handlers._ssh_remote_button(mock_session)
        assert btn is not None
        assert "SSH: выкл" in btn.text
        assert btn.callback_data.startswith("sess_ssh_toggle:")

        # ON state
        mock_session.modes.ssh_remote_enabled = True
        btn = handlers._ssh_remote_button(mock_session)
        assert btn is not None
        assert "SSH: вкл" in btn.text


@pytest.mark.asyncio
async def test_cb_sess_ssh_toggle():
    bot_app = MagicMock()
    bot_app._edit_message = AsyncMock()
    handler = CallbackHandler(bot_app)

    session = MagicMock(spec=Session)
    session.id = "sid123"
    session.chat_id = 123
    session.workdir = "/tmp/proj"
    session.modes = ModeState(ssh_remote_enabled=False)

    class _Manager:
        def __init__(self):
            self.writes = []

        def get_by_uid(self, uid):
            return session if uid == "uid123" else None

        def serialize_chat_entry_for_persist(self, chat_id, session_id):
            return {"session_id": session_id, "chat_id": chat_id}

        def write_chat_entry(self, chat_id, entry):
            self.writes.append((chat_id, entry))
            return True

    manager = _Manager()
    bot_app.manager = manager
    bot_app.handlers.build_sessions_active_overview.return_value = ("text", MagicMock())
    # Контракт резолвера: (reply_chat_id, thread_id, owner_chat_id, session).
    bot_app.resolve_telegram_callback_scope = MagicMock(return_value=(123, None, 123, session))

    query = AsyncMock()
    query.data = "sess_ssh_toggle:uid123"
    context = MagicMock()

    # 1. SSH not available for project
    with patch("tg.callback_actions.session.ssh_remote_available", return_value=False):
        res = await handler._cb_sess_ssh_toggle(data=query.data, chat_id=123, query=query, context=context)
        assert res is True
        query.answer.assert_called_with("SSH не настроен для этого проекта", show_alert=True)
        assert session.modes.ssh_remote_enabled is False

    # 2. SSH available, toggle to ON
    query.answer.reset_mock()
    with patch("tg.callback_actions.session.ssh_remote_available", return_value=True):
        res = await handler._cb_sess_ssh_toggle(data=query.data, chat_id=123, query=query, context=context)
        assert res is True
        assert session.modes.ssh_remote_enabled is True
        query.answer.assert_called_with("Удалённое управление включено")
        assert manager.writes == [(123, {"session_id": "sid123", "chat_id": 123})]

    # 3. Toggle back to OFF
    query.answer.reset_mock()
    with patch("tg.callback_actions.session.ssh_remote_available", return_value=True):
        res = await handler._cb_sess_ssh_toggle(data=query.data, chat_id=123, query=query, context=context)
        assert res is True
        assert session.modes.ssh_remote_enabled is False
        query.answer.assert_called_with("Удалённое управление выключено")


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_cls", [CallbackHandler, BotHandlers])
async def test_persist_session_async_falls_back_on_write_failure(handler_cls):
    bot_app = MagicMock()
    bot_app.mode_session_control.persist = MagicMock()

    class _Manager:
        def serialize_chat_entry_for_persist(self, chat_id, session_id):
            return {"session_id": session_id, "chat_id": chat_id}

        def write_chat_entry(self, chat_id, entry):
            return False

    bot_app.manager = _Manager()
    handler = handler_cls(bot_app)

    await handler._persist_session_async(123, "sid123")

    bot_app.mode_session_control.persist.assert_called_once_with()
