"""Tests for SSH integration in BotApp, bootstrap, and Session."""

import os
from unittest.mock import MagicMock

from app.bootstrap import build_application
from app.services.ssh_service import SSHService
from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)
from session import Session


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
    )


def test_bootstrap_creates_ssh_service(tmp_path):
    cfg = _build_config(tmp_path)
    container = build_application(cfg)
    assert isinstance(container.ssh_service, SSHService)


def test_bootstrap_passes_ssh_to_mode_dependencies(tmp_path):
    cfg = _build_config(tmp_path)
    container = build_application(cfg)
    assert container.mode_dependencies.ssh is container.ssh_service


def test_session_has_ssh_service_field(tmp_path):
    cfg = _build_config(tmp_path)
    session = Session(
        id="s1",
        tool=cfg.tools["qwen"],
        workdir=cfg.defaults.workdir,
        idle_timeout_sec=100,
        config=cfg,
    )
    assert session._ssh_service is None
    mock_ssh = MagicMock(spec=SSHService)
    session._ssh_service = mock_ssh
    assert session._ssh_service is mock_ssh
