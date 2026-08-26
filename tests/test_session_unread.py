"""Tests for session unread (manual "mark as unread" flag) state."""

import os
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from miniapp.services.config_service import app_config_to_dict
from session import Session, SessionManager, session_runtime_uid
from sessions.session_state_access import is_session_unread, set_session_unread
from sessions.session_status import build_session_status_text
from tg.callbacks import CallbackHandler
from tg.handlers import BotHandlers


# ---------------------------------------------------------------------------
# 1. is_session_unread / set_session_unread on a bare fake object
# ---------------------------------------------------------------------------

def test_is_session_unread_defaults_false_on_bare_object():
    session = types.SimpleNamespace()
    assert is_session_unread(session) is False


def test_set_session_unread_toggles_flat_attribute():
    session = types.SimpleNamespace()
    set_session_unread(session, True)
    assert session.unread is True
    assert is_session_unread(session) is True

    set_session_unread(session, False)
    assert session.unread is False
    assert is_session_unread(session) is False


# ---------------------------------------------------------------------------
# 2. Persist/restore across SessionManager restarts (same state.db)
# ---------------------------------------------------------------------------

def _build_config(tmp_path):
    workdir = str(tmp_path / "project")
    os.makedirs(workdir, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1]),
        tools={"qwen": ToolConfig(name="qwen", mode="headless", cmd=["echo"])},
        defaults=DefaultsConfig(
            workdir=workdir,
            state_path=str(tmp_path / "state.db"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    ), workdir


def test_unread_persist_and_restore(tmp_path):
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    session = mgr.create(1, "qwen", workdir)
    assert session.unread is False

    session.unread = True
    mgr._persist_sessions()

    # Restore via new manager (same state DB)
    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.unread is True


def test_unread_defaults_false_on_restore(tmp_path):
    """Sessions persisted before the flag existed have no "unread" key at all."""
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    session = mgr.create(1, "qwen", workdir)
    session.unread = True
    mgr._persist_sessions()

    # Emulate a state file written by an older build: strip the key entirely.
    entry = mgr._state_repo.load_sessions_by_chat()["1"]
    sid = list(entry["sessions"].keys())[0]
    assert entry["sessions"][sid].pop("unread", None) is True
    mgr._state_repo.replace_chat_entry(chat_id=1, entry=entry)

    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.unread is False


def test_unread_two_sessions_independent(tmp_path):
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    s1 = mgr.create(1, "qwen", workdir)
    s2 = mgr.create(1, "qwen", workdir)

    s1.unread = True
    assert s2.unread is False

    mgr._persist_sessions()
    mgr2 = SessionManager(cfg)
    sessions = mgr2.sessions_for_chat(1)
    flags = [s.unread for s in sessions.values()]
    assert True in flags
    assert False in flags


# ---------------------------------------------------------------------------
# 4. Callback handler: first click marks unread, second click clears it;
#    доступ к чужой сессии по подделанному callback_data должен отклоняться.
# ---------------------------------------------------------------------------

def _toggle_bot_app(manager, *, admin_chat_ids=()):
    """bot_app для _cb_sess_unread_toggle с НАСТОЯЩИМ BotHandlers._is_session_visible_for_chat
    (честная проверка владения чатом), а не заглушкой, которая ничего не проверяет.

    resolve_telegram_callback_scope намеренно не задан: без него _callback_scope
    возвращает owner_chat_id = переданный в вызов chat_id (реальный чат запроса) —
    этого достаточно, чтобы смоделировать чат, из которого пришёл callback.
    """
    bot_app = types.SimpleNamespace(
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
        manager=manager,
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: int(chat_id) in admin_chat_ids),
        is_session_allowed_for_chat=lambda chat_id, session: int(chat_id) == int(getattr(session, "chat_id", -1)),
    )
    bot_app.handlers = BotHandlers(bot_app)
    bot_app.handlers.build_sessions_active_overview = MagicMock(return_value=("text", MagicMock()))
    edits: list[str] = []

    async def _edit_message(_context, *, chat_id, message_id, text, reply_markup=None, md2=True):
        edits.append(str(text))
        return True

    bot_app._edit_message = _edit_message
    bot_app.edits = edits
    return bot_app


def _toggle_query(*, chat_id: int, message_id: int = 1, user_id: int = 1):
    return types.SimpleNamespace(
        message=types.SimpleNamespace(chat_id=chat_id, message_id=message_id),
        from_user=types.SimpleNamespace(id=user_id, language_code="ru"),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_cb_sess_unread_toggle(tmp_path):
    cfg, workdir = _build_config(tmp_path)
    mgr = SessionManager(cfg)
    owner_chat_id = 1
    session = mgr.create(owner_chat_id, "qwen", workdir)
    assert session.unread is False

    bot_app = _toggle_bot_app(mgr)
    handler = CallbackHandler(bot_app)

    data = f"sess_unread_toggle:{session_runtime_uid(session)}"
    query = _toggle_query(chat_id=owner_chat_id)
    context = MagicMock()

    # 1. First click: off -> on
    res = await handler._cb_sess_unread_toggle(data=data, chat_id=owner_chat_id, query=query, context=context)
    assert res is True
    assert session.unread is True
    query.answer.assert_called_with("Сессия отмечена как непрочитанная.")

    # 2. Second click: on -> off
    query.answer.reset_mock()
    res = await handler._cb_sess_unread_toggle(data=data, chat_id=owner_chat_id, query=query, context=context)
    assert res is True
    assert session.unread is False
    query.answer.assert_called_with("Отметка «непрочитано» снята.")


@pytest.mark.asyncio
async def test_cb_sess_unread_toggle_denies_forged_cross_chat_payload(tmp_path):
    """Ключевой негативный тест дыры доступа: сессия принадлежит чату A, а callback
    приходит из чата B с подделанным data вида sess_unread_toggle:chat:<A>:<sid>
    (поле data в MTProto-вызове произвольное и не обязано соответствовать кнопке).
    Флаг чужой сессии не должен измениться, персист не должен вызваться, и
    пользователю должен быть показан отказ, а не обзор чужой сессии."""
    cfg, workdir = _build_config(tmp_path)
    mgr = SessionManager(cfg)
    owner_chat_id = 1
    attacker_chat_id = 2
    session = mgr.create(owner_chat_id, "qwen", workdir)
    assert session.unread is False

    bot_app = _toggle_bot_app(mgr)
    handler = CallbackHandler(bot_app)

    data = f"sess_unread_toggle:chat:{owner_chat_id}:{session.id}"
    query = _toggle_query(chat_id=attacker_chat_id, user_id=999)
    context = MagicMock()

    res = await handler._cb_sess_unread_toggle(data=data, chat_id=attacker_chat_id, query=query, context=context)

    assert res is True
    assert session.unread is False
    query.answer.assert_not_called()
    bot_app.handlers.build_sessions_active_overview.assert_not_called()
    assert bot_app.edits[-1] == "Сессия недоступна."


@pytest.mark.asyncio
async def test_cb_sess_unread_toggle_allows_admin_cross_chat(tmp_path):
    """Админ по-прежнему может переключить флаг сессии другого чата: в
    _is_session_visible_for_chat админ проходит проверку всегда."""
    cfg, workdir = _build_config(tmp_path)
    mgr = SessionManager(cfg)
    owner_chat_id = 1
    admin_chat_id = 999
    session = mgr.create(owner_chat_id, "qwen", workdir)
    assert session.unread is False

    bot_app = _toggle_bot_app(mgr, admin_chat_ids={admin_chat_id})
    handler = CallbackHandler(bot_app)

    data = f"sess_unread_toggle:chat:{owner_chat_id}:{session.id}"
    query = _toggle_query(chat_id=admin_chat_id)
    context = MagicMock()

    res = await handler._cb_sess_unread_toggle(data=data, chat_id=admin_chat_id, query=query, context=context)

    assert res is True
    assert session.unread is True
    query.answer.assert_called_with("Сессия отмечена как непрочитанная.")


# ---------------------------------------------------------------------------
# 6. build_session_status_text: 🔵 marker on first line when unread
# ---------------------------------------------------------------------------

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
    s.unread = False
    s.project_root = None
    return s


def test_build_session_status_text_marks_unread_in_title(mock_session):
    with patch("sessions.session_status.time.time", return_value=1200):
        with patch("sessions.session_status.load_ssh_config", return_value={}):
            status_off = build_session_status_text(mock_session)
            assert not status_off.splitlines()[0].startswith("🔵 ")

            mock_session.unread = True
            status_on = build_session_status_text(mock_session)
            assert status_on.splitlines()[0].startswith("🔵 ")


# ---------------------------------------------------------------------------
# 7. build_sessions_menu: marker present for unread session, absent for the rest
# ---------------------------------------------------------------------------

def _build_bot_config(tmp_path, *, admin_chat_ids=(1,), chat_ids=(1,)):
    workdir = str(tmp_path)
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="t", whitelist_chat_ids=[int(c) for c in chat_ids],
            admlist_chat_ids=[int(c) for c in admin_chat_ids],
            user_workdirs={int(c): [workdir] for c in chat_ids},
        ),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["echo"])},
        defaults=DefaultsConfig(
            workdir=workdir,
            state_path=str(tmp_path / "state.db"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(app_config_to_dict(cfg), f, sort_keys=False)
    return cfg, workdir


def test_build_sessions_menu_marks_unread_session(tmp_path):
    cfg, workdir = _build_bot_config(tmp_path)

    app = BotApp(cfg)
    try:
        read_session = app.manager.create(1, "dummy", workdir)
        unread_session = app.manager.create(1, "dummy", workdir)
        unread_session.unread = True

        keyboard = app.session_ui.build_sessions_menu(1)
        texts = [btn.text for row in keyboard.inline_keyboard for btn in row]

        read_text = next(t for t in texts if read_session.id in t)
        unread_text = next(t for t in texts if unread_session.id in t)

        assert not read_text.startswith("🔵 ")
        assert unread_text.startswith("🔵 ")
    finally:
        app.shutdown_html_process_pool()


# ---------------------------------------------------------------------------
# 8. build_sessions_active_overview: кнопка переключения «непрочитано»
# ---------------------------------------------------------------------------

def _find_unread_button(keyboard):
    for row in keyboard.inline_keyboard:
        for btn in row:
            if str(getattr(btn, "callback_data", "") or "").startswith("sess_unread_toggle:"):
                return btn
    return None


def test_sessions_active_overview_shows_unread_toggle(tmp_path):
    """Кнопка живёт в меню сессии: её наличие, подпись и callback_data - часть контракта.

    Подпись переворачивается по текущему флагу: прочитанной сессии предлагают
    отметить непрочитанной и наоборот.
    """
    cfg, workdir = _build_bot_config(tmp_path)

    app = BotApp(cfg)
    try:
        session = app.manager.create(1, "dummy", workdir)
        expected_data = f"sess_unread_toggle:{session_runtime_uid(session) or session.id}"

        _, keyboard = app.handlers.build_sessions_active_overview(1, session=session, lang="ru")
        btn = _find_unread_button(keyboard)
        assert btn is not None
        assert btn.callback_data == expected_data
        assert btn.text == "🔵 Отметить непрочитанным"

        session.unread = True
        _, keyboard_unread = app.handlers.build_sessions_active_overview(1, session=session, lang="ru")
        btn_unread = _find_unread_button(keyboard_unread)
        assert btn_unread is not None
        assert btn_unread.callback_data == expected_data
        assert btn_unread.text == "⚪ Отметить прочитанным"
    finally:
        app.shutdown_html_process_pool()


def test_sessions_active_overview_shows_unread_toggle_for_non_admin(tmp_path):
    """Отметка «непрочитано» - личная пометка владельца сессии, а не админская
    операция: у не-админа кнопка тоже должна быть."""
    cfg, workdir = _build_bot_config(tmp_path, admin_chat_ids=(1,), chat_ids=(1, 2))

    app = BotApp(cfg)
    try:
        assert app.is_admin(2) is False
        session = app.manager.create(2, "dummy", workdir)

        _, keyboard = app.handlers.build_sessions_active_overview(2, session=session, lang="ru")
        btn = _find_unread_button(keyboard)
        assert btn is not None
        assert btn.text == "🔵 Отметить непрочитанным"
    finally:
        app.shutdown_html_process_pool()
