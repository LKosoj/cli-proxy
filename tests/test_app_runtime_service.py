from __future__ import annotations

import asyncio
from types import SimpleNamespace

import yaml

from app.services.app_runtime_service import AppRuntimeService
from config import AppConfig, load_config


class _DummySession:
    def __init__(self, config: AppConfig) -> None:
        self.id = "s1"
        self.config = config
        self.tool = config.tools["dummy"]
        self.active_cli = "dummy"
        self.cli = SimpleNamespace(active_cli="dummy")

    def set_active_cli(self, cli_name: str) -> None:
        self.active_cli = cli_name
        self.cli.active_cli = cli_name


def _payload(tmp_path, *, webhooks_enabled: bool) -> dict:
    return {
        "telegram": {
            "token": "stable-token",
            "whitelist_chat_ids": [1],
            "admlist_chat_ids": [1],
        },
        "tools": {
            "dummy": {
                "mode": "headless",
                "cmd": ["bash", "-lc", "cat"],
            }
        },
        "defaults": {
            "workdir": str(tmp_path),
            "state_path": str(tmp_path / "state.json"),
            "desktop_state_path": str(tmp_path / "desktop_state.json"),
            "toolhelp_path": str(tmp_path / "toolhelp.json"),
            "log_path": str(tmp_path / "bot.log"),
        },
        "webhooks": {
            "enabled": webhooks_enabled,
            "public_base_url": "https://example.com",
        },
    }


def _write_config(path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _build_bot_app(config: AppConfig):
    session = _DummySession(config)
    manager = SimpleNamespace(config=config, sessions_by_chat={1: {session.id: session}})
    return SimpleNamespace(
        config=config,
        manager=manager,
        git=SimpleNamespace(config=config),
        mcp=SimpleNamespace(config=config),
        session_ui=SimpleNamespace(config=config),
        system_event_bus=None,
        is_admin=lambda chat_id: int(chat_id) == 1,
        is_user=lambda chat_id: int(chat_id) == 1,
        iter_mode_runtimes=lambda: [],
    )


def test_webhooks_enabled_restart_required_classification(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, webhooks_enabled=True))
    previous = load_config(str(path))
    bot_app = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    _write_config(path, _payload(tmp_path, webhooks_enabled=False))
    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert "webhooks.enabled" in result["restart_required"]
    assert "webhooks.enabled" not in result["applied"]
    assert result["warnings"] == ["Some changes require process restart."]
    assert bot_app.config.webhooks.enabled is True


def test_miniapp_max_edit_file_size_restart_required_classification(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    initial = _payload(tmp_path, webhooks_enabled=True)
    initial["miniapp"] = {
        "enabled": True,
        "base_path": "/cli-proxy",
        "max_edit_file_size_kb": 5120,
    }
    _write_config(path, initial)
    previous = load_config(str(path))
    bot_app = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, webhooks_enabled=True)
    updated["miniapp"] = {
        "enabled": True,
        "base_path": "/cli-proxy",
        "max_edit_file_size_kb": 1024,
    }
    _write_config(path, updated)
    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert "miniapp.max_edit_file_size_kb" in result["restart_required"]
    assert "miniapp.max_edit_file_size_kb" not in result["applied"]
    assert result["warnings"] == ["Some changes require process restart."]
    assert bot_app.config.miniapp.max_edit_file_size_kb == 5120
