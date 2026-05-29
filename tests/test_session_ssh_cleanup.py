"""Test that Session.close() triggers SSH connection cleanup."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
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


@pytest.mark.asyncio
async def test_session_close_calls_ssh_close_all(tmp_path):
    cfg = _build_config(tmp_path)
    session = Session(
        id="s1",
        tool=cfg.tools["qwen"],
        workdir=cfg.defaults.workdir,
        idle_timeout_sec=100,
        config=cfg,
    )
    mock_ssh = MagicMock()
    mock_ssh.close_all = AsyncMock()
    session._ssh_service = mock_ssh

    session.close()

    # ensure_future schedules the coroutine; give event loop a tick
    await asyncio.sleep(0.05)
    mock_ssh.close_all.assert_awaited_once_with(workdir=cfg.defaults.workdir)


def test_session_close_without_ssh_does_not_raise(tmp_path):
    cfg = _build_config(tmp_path)
    session = Session(
        id="s1",
        tool=cfg.tools["qwen"],
        workdir=cfg.defaults.workdir,
        idle_timeout_sec=100,
        config=cfg,
    )
    assert session._ssh_service is None
    session.close()  # should not raise
