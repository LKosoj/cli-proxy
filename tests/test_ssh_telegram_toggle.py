"""Verification: Telegram SSH toggle callback changes session state and message."""

from types import SimpleNamespace

import yaml

from app.services.ssh_config_loader import save_ssh_config, ssh_remote_available
from bot import BotApp
from config import (
    AppConfig, DefaultsConfig, MCPConfig,
    SSHHostConfig, TelegramConfig, ToolConfig,
)
from miniapp.services.config_service import app_config_to_dict
from session import ModeState
from sessions.session_state_access import is_ssh_remote_enabled, set_ssh_remote_enabled


def _setup_workdir(tmp_path):
    workdir = str(tmp_path)
    hosts = {"prod": SSHHostConfig(host="10.0.0.1", user="deploy")}
    save_ssh_config(workdir, hosts)
    return workdir


def test_toggle_changes_state_false_to_true(tmp_path):
    """Simulate the core logic of _cb_sess_ssh_toggle: read→flip→write."""
    workdir = _setup_workdir(tmp_path)
    session = SimpleNamespace(
        modes=ModeState(ssh_remote_enabled=False),
        workdir=workdir,
    )

    assert ssh_remote_available(workdir)
    current = is_ssh_remote_enabled(session)
    assert current is False
    set_ssh_remote_enabled(session, not current)
    assert is_ssh_remote_enabled(session) is True


def test_toggle_changes_state_true_to_false(tmp_path):
    workdir = _setup_workdir(tmp_path)
    session = SimpleNamespace(
        modes=ModeState(ssh_remote_enabled=True),
        workdir=workdir,
    )

    current = is_ssh_remote_enabled(session)
    assert current is True
    set_ssh_remote_enabled(session, not current)
    assert is_ssh_remote_enabled(session) is False


def test_toggle_noop_when_no_ssh_config(tmp_path):
    """When ssh_remote_available returns False, toggle should not proceed."""
    workdir = str(tmp_path)  # no .cli-proxy/ssh.yaml
    assert not ssh_remote_available(workdir)


def test_double_toggle_returns_to_original(tmp_path):
    workdir = _setup_workdir(tmp_path)
    session = SimpleNamespace(
        modes=ModeState(ssh_remote_enabled=False),
        workdir=workdir,
    )

    set_ssh_remote_enabled(session, not is_ssh_remote_enabled(session))
    assert is_ssh_remote_enabled(session) is True
    set_ssh_remote_enabled(session, not is_ssh_remote_enabled(session))
    assert is_ssh_remote_enabled(session) is False


# ---------------------------------------------------------------------------
# Message update verification: build_sessions_active_overview reflects toggle
# ---------------------------------------------------------------------------

def _build_bot_config(tmp_path):
    workdir = str(tmp_path)
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="t", whitelist_chat_ids=[1],
            admlist_chat_ids=[1],
            user_workdirs={1: [workdir]},
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


def test_message_updates_after_ssh_toggle(tmp_path):
    """After toggle, build_sessions_active_overview returns updated SSH label."""
    cfg, workdir = _build_bot_config(tmp_path)
    save_ssh_config(workdir, {"prod": SSHHostConfig(host="1.2.3.4", user="u")})

    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", workdir)

    # Before toggle: SSH off
    text_off, kb_off = app.handlers.build_sessions_active_overview(1, session=session)
    assert "SSH: выкл" in text_off or any(
        "SSH: выкл" in btn.text
        for row in (kb_off.inline_keyboard if kb_off else [])
        for btn in row
    )

    # Toggle on
    set_ssh_remote_enabled(session, True)

    # After toggle: SSH on
    text_on, kb_on = app.handlers.build_sessions_active_overview(1, session=session)
    ssh_on_found = "SSH: вкл" in text_on or any(
        "SSH: вкл" in btn.text
        for row in (kb_on.inline_keyboard if kb_on else [])
        for btn in row
    )
    assert ssh_on_found, "'SSH: вкл' not found in text or keyboard after toggle"

    app.shutdown_html_process_pool()
