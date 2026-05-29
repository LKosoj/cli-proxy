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


def _payload(tmp_path, *, run_metrics_enabled: bool = True) -> dict:
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
            "run_artifacts_enabled": True,
            "run_artifacts_retention_days": 30,
            "run_doctor_enabled": True,
            "run_boundary_validation_enabled": True,
            "run_metrics_enabled": run_metrics_enabled,
            "skill_discovery_mode": "suggest",
            "skill_install_policy": "manual",
            "skill_registry_paths": [".cli-proxy/skills"],
            "skill_allowlisted_sources": [
                "local:global-registry",
                "local:project-registry",
            ],
        },
    }


def _write_config(path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _build_initial_config(path) -> AppConfig:
    return load_config(str(path))


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


def test_runtime_reload_returns_restart_required_for_restart_only_defaults_field(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, run_metrics_enabled=True))
    previous = _build_initial_config(path)
    bot_app = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    _write_config(path, _payload(tmp_path, run_metrics_enabled=False))
    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert "defaults.run_metrics_enabled" in result["restart_required"]
    assert "defaults.run_metrics_enabled" not in result["applied"]
