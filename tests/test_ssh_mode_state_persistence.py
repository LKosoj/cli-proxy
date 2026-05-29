"""Verification: ssh_remote_enabled flag survives bot restart (new SessionManager)."""

import os

from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)
from session import SessionManager
from sessions.session_state_access import is_ssh_remote_enabled, set_ssh_remote_enabled


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


def test_ssh_flag_survives_bot_restart(tmp_path):
    """Simulate bot restart: create mgr1, set flag, persist, create mgr2, verify."""
    cfg, workdir = _build_config(tmp_path)

    # Bot instance 1: create session, enable SSH, persist
    mgr1 = SessionManager(cfg)
    session1 = mgr1.create(1, "qwen", workdir)
    assert is_ssh_remote_enabled(session1) is False
    set_ssh_remote_enabled(session1, True)
    mgr1._persist_sessions()

    # Bot instance 2 (simulates restart): new SessionManager, same state DB
    mgr2 = SessionManager(cfg)
    sessions = mgr2.sessions_for_chat(1)
    assert len(sessions) >= 1
    restored = list(sessions.values())[0]
    assert is_ssh_remote_enabled(restored) is True


def test_ssh_flag_false_survives_restart(tmp_path):
    """Verify False is also correctly restored (not just True)."""
    cfg, workdir = _build_config(tmp_path)

    mgr1 = SessionManager(cfg)
    session1 = mgr1.create(1, "qwen", workdir)
    set_ssh_remote_enabled(session1, True)
    mgr1._persist_sessions()

    # Flip to False
    set_ssh_remote_enabled(session1, False)
    mgr1._persist_sessions()

    mgr2 = SessionManager(cfg)
    sessions = mgr2.sessions_for_chat(1)
    restored = list(sessions.values())[0]
    assert is_ssh_remote_enabled(restored) is False


def test_ssh_flag_default_on_old_data(tmp_path):
    """Sessions persisted before SSH feature default to False after restart."""
    cfg, workdir = _build_config(tmp_path)

    mgr1 = SessionManager(cfg)
    mgr1.create(1, "qwen", workdir)
    # Never set ssh_remote_enabled — simulates old session data
    mgr1._persist_sessions()

    mgr2 = SessionManager(cfg)
    sessions = mgr2.sessions_for_chat(1)
    restored = list(sessions.values())[0]
    assert is_ssh_remote_enabled(restored) is False
