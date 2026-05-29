"""Tests for ModeState.ssh_remote_enabled field and its persistence."""

import os

from session import ModeState, SessionManager
from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)


def test_mode_state_ssh_remote_enabled_default():
    state = ModeState()
    assert state.ssh_remote_enabled is False


def test_mode_state_ssh_remote_enabled_set():
    state = ModeState()
    state.ssh_remote_enabled = True
    assert state.ssh_remote_enabled is True


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


def test_ssh_remote_enabled_persist_and_restore(tmp_path):
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    session = mgr.create(1, "qwen", workdir)
    assert session.modes.ssh_remote_enabled is False

    session.modes.ssh_remote_enabled = True
    mgr._persist_sessions()

    # Restore via new manager (same state DB)
    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.modes.ssh_remote_enabled is True


def test_ssh_remote_enabled_defaults_false_on_restore(tmp_path):
    """Old sessions without ssh_remote_enabled should default to False."""
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    mgr.create(1, "qwen", workdir)
    mgr._persist_sessions()

    # Restore: field was never set, should be False
    mgr2 = SessionManager(cfg)
    sessions2 = mgr2.sessions_for_chat(1)
    restored = list(sessions2.values())[0]
    assert restored.modes.ssh_remote_enabled is False


def test_ssh_remote_enabled_two_sessions_independent(tmp_path):
    cfg, workdir = _build_config(tmp_path)

    mgr = SessionManager(cfg)
    s1 = mgr.create(1, "qwen", workdir)
    s2 = mgr.create(1, "qwen", workdir)

    s1.modes.ssh_remote_enabled = True
    assert s2.modes.ssh_remote_enabled is False

    mgr._persist_sessions()
    mgr2 = SessionManager(cfg)
    sessions = mgr2.sessions_for_chat(1)
    flags = [s.modes.ssh_remote_enabled for s in sessions.values()]
    assert True in flags
    assert False in flags
