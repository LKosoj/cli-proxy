from __future__ import annotations

import asyncio
import pathlib

import yaml

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


def _build_config(tmp_path: pathlib.Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(enabled=False),
    )


def test_bot_startup_smoke_builds_local_runtime(tmp_path) -> None:
    app = BotApp(_build_config(tmp_path, intent="bot_smoke"))
    try:
        assert app.mode_run_artifacts.is_enabled() is True
        assert app.mode_skill_runtime is not None
        assert app.miniapp_server._started is False
        assert app.shared_http_ingress is not None
    finally:
        app.shutdown_html_process_pool()


