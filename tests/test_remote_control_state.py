"""Tests for remote_control_enabled and remote_control_host_alias in ModeState."""

import os
from types import SimpleNamespace

from session import ModeState, SessionManager
from sessions.session_state_access import (
    get_remote_control_host_alias,
    is_remote_control_enabled,
    set_remote_control_enabled,
    set_remote_control_host_alias,
)
from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)


# ---------------------------------------------------------------------------
# ModeState dataclass defaults
# ---------------------------------------------------------------------------


def test_remote_control_enabled_default():
    state = ModeState()
    assert state.remote_control_enabled is False


def test_remote_control_host_alias_default():
    state = ModeState()
    assert state.remote_control_host_alias is None


def test_remote_control_fields_set():
    state = ModeState(remote_control_enabled=True, remote_control_host_alias="prod")
    assert state.remote_control_enabled is True
    assert state.remote_control_host_alias == "prod"


# ---------------------------------------------------------------------------
# session_state_access helpers
# ---------------------------------------------------------------------------


def test_accessor_enabled_with_modes():
    session = SimpleNamespace(modes=ModeState())
    assert is_remote_control_enabled(session) is False
    set_remote_control_enabled(session, True)
    assert is_remote_control_enabled(session) is True


def test_accessor_host_alias_with_modes():
    session = SimpleNamespace(modes=ModeState())
    assert get_remote_control_host_alias(session) is None
    set_remote_control_host_alias(session, "staging")
    assert get_remote_control_host_alias(session) == "staging"


def test_accessor_enabled_without_modes():
    session = SimpleNamespace()
    assert is_remote_control_enabled(session) is False
    set_remote_control_enabled(session, True)
    assert is_remote_control_enabled(session) is True


def test_accessor_host_alias_without_modes():
    session = SimpleNamespace()
    assert get_remote_control_host_alias(session) is None
    set_remote_control_host_alias(session, "db")
    assert get_remote_control_host_alias(session) == "db"


def test_accessor_empty_string_alias_normalizes_to_none():
    session = SimpleNamespace(modes=ModeState())
    set_remote_control_host_alias(session, "")
    assert get_remote_control_host_alias(session) is None


def test_accessor_whitespace_alias_normalizes_to_none():
    session = SimpleNamespace(modes=ModeState())
    set_remote_control_host_alias(session, "   ")
    assert get_remote_control_host_alias(session) is None


# ---------------------------------------------------------------------------
# Persistence roundtrip
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


def test_persist_and_restore_remote_control(tmp_path):
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    session = mgr.create(1, "qwen", workdir)
    assert session.modes.remote_control_enabled is False
    assert session.modes.remote_control_host_alias is None

    session.modes.remote_control_enabled = True
    session.modes.remote_control_host_alias = "prod"
    mgr._persist_sessions()

    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.modes.remote_control_enabled is True
    assert restored.modes.remote_control_host_alias == "prod"


def test_old_session_defaults_on_restore(tmp_path):
    """Sessions without the new fields load with defaults."""
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    mgr.create(1, "qwen", workdir)
    mgr._persist_sessions()

    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.modes.remote_control_enabled is False
    assert restored.modes.remote_control_host_alias is None


def test_host_alias_preserved_when_disabled(tmp_path):
    """remote_control_host_alias is NOT cleared when remote_control_enabled = False."""
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    session = mgr.create(1, "qwen", workdir)
    session.modes.remote_control_enabled = True
    session.modes.remote_control_host_alias = "staging"

    # Disable without clearing alias
    session.modes.remote_control_enabled = False
    mgr._persist_sessions()

    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.modes.remote_control_enabled is False
    assert restored.modes.remote_control_host_alias == "staging"


def test_two_sessions_independent_remote_control(tmp_path):
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    s1 = mgr.create(1, "qwen", workdir)
    s2 = mgr.create(1, "qwen", workdir)

    s1.modes.remote_control_enabled = True
    s1.modes.remote_control_host_alias = "prod"

    assert s2.modes.remote_control_enabled is False
    assert s2.modes.remote_control_host_alias is None

    mgr._persist_sessions()
    mgr2 = SessionManager(cfg)
    sessions = mgr2.sessions_for_chat(1)
    values = list(sessions.values())
    aliases = [s.modes.remote_control_host_alias for s in values]
    assert "prod" in aliases
    assert None in aliases


def test_two_sequential_runs_no_state_leak(tmp_path):
    """Two sequential creates with different intent don't leak state."""
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    s1 = mgr.create(1, "qwen", workdir)
    s1.modes.remote_control_enabled = True
    s1.modes.remote_control_host_alias = "alpha"
    mgr._persist_sessions()

    # Second run: create a new session — should have defaults
    mgr2 = SessionManager(cfg)
    s2 = mgr2.create(1, "qwen", workdir)
    assert s2.modes.remote_control_enabled is False
    assert s2.modes.remote_control_host_alias is None
