from __future__ import annotations

import pathlib

import yaml

from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


def build_config(tmp_path: pathlib.Path, *, intent: str, miniapp_enabled: bool = False) -> AppConfig:
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
        miniapp=MiniAppConfig(enabled=miniapp_enabled),
    )


def write_config(cfg: AppConfig, path: pathlib.Path) -> pathlib.Path:
    payload = {
        "telegram": {
            "token": cfg.telegram.token,
            "whitelist_chat_ids": list(cfg.telegram.whitelist_chat_ids),
            "admlist_chat_ids": list(cfg.telegram.admlist_chat_ids),
        },
        "tools": {
            "dummy": {"mode": "headless", "cmd": ["bash", "-lc", "cat"], "enabled": True},
        },
        "defaults": {
            "workdir": cfg.defaults.workdir,
            "state_path": cfg.defaults.state_path,
            "toolhelp_path": cfg.defaults.toolhelp_path,
            "log_path": cfg.defaults.log_path,
        },
        "mcp": {"enabled": False},
        "mcp_clients": [],
        "presets": [],
        "miniapp": {"enabled": bool(getattr(cfg.miniapp, "enabled", False))},
    }
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return path
