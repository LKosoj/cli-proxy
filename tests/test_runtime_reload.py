from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import yaml

from app.events.bus import SystemEventBus
from app.services.memory_event_store import MemoryEventStore
from app.services.app_runtime_service import AppRuntimeService
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend
from config import AppConfig, load_config


class _DummyRuntime:
    def __init__(self) -> None:
        self.config = None
        self.calls: list[AppConfig] = []

    def set_config(self, config: AppConfig) -> None:
        self.config = config
        self.calls.append(config)


class _DummySession:
    def __init__(self, config: AppConfig, active_cli: str = "dummy") -> None:
        self.id = "s1"
        self.config = config
        self.tool = config.tools[active_cli]
        self.active_cli = active_cli
        self.cli = SimpleNamespace(active_cli=active_cli)
        self.active_cli_updates: list[str] = []

    def set_active_cli(self, cli_name: str) -> None:
        self.active_cli = cli_name
        self.cli.active_cli = cli_name
        self.active_cli_updates.append(cli_name)
        # Update session.tool like real Session.set_active_cli does
        if self.config and cli_name in (self.config.tools or {}):
            self.tool = self.config.tools[cli_name]


class _DummyManager:
    def __init__(self, config: AppConfig, session: _DummySession) -> None:
        self.config = config
        self.sessions_by_chat = {1: {session.id: session}}


def _payload(tmp_path, *, token: str, extra_tool: str | None = None) -> dict:
    tools = {
        "dummy": {
            "mode": "headless",
            "cmd": ["bash", "-lc", "cat"],
        }
    }
    if extra_tool:
        tools[extra_tool] = {
            "mode": "headless",
            "cmd": [extra_tool],
        }
    return {
        "telegram": {
            "token": token,
            "whitelist_chat_ids": [1],
            "admlist_chat_ids": [1],
        },
        "tools": tools,
        "defaults": {
            "workdir": str(tmp_path),
            "state_path": str(tmp_path / "state.json"),
            "desktop_state_path": str(tmp_path / "desktop_state.json"),
            "toolhelp_path": str(tmp_path / "toolhelp.json"),
            "log_path": str(tmp_path / "bot.log"),
            "cli_json_stream_archive_enabled": False,
            "assistant_preview_enabled": False,
            "pending_input_confirmation_enabled": True,
            "run_artifacts_enabled": True,
            "run_artifacts_retention_days": 30,
            "run_doctor_enabled": True,
            "run_boundary_validation_enabled": True,
            "run_metrics_enabled": True,
            "skill_discovery_mode": "suggest",
            "skill_install_policy": "manual",
            "skill_registry_paths": [".cli-proxy/skills"],
            "skill_allowlisted_sources": [
                "local:global-registry",
                "local:project-registry",
            ],
        },
        "thread_mode": {
            "enabled": True,
            "mode": "group",
            "topics_chat_id": -1001234567890,
        },
        "webhooks": {
            "enabled": True,
            "public_base_url": "https://example.com",
        },
        "scheduler": {
            "enabled": True,
            "timezone": "Europe/Moscow",
        },
    }


def _with_tmux_tool(payload: dict, *, backend: str = "tmux") -> dict:
    payload["tools"]["dummy"].update(
        {
            "interactive_cmd": ["bash", "-lc", "cat"],
            "execution_backends": ["headless", "tmux"],
            "default_execution_backend": backend,
        }
    )
    return payload


def _write_config(path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _build_initial_config(path) -> AppConfig:
    return load_config(str(path))


def _build_bot_app(config: AppConfig):
    session = _DummySession(config)
    manager = _DummyManager(config, session)
    runtime = _DummyRuntime()
    bus = SystemEventBus()
    return (
        SimpleNamespace(
            config=config,
            manager=manager,
            git=SimpleNamespace(config=config),
            mcp=SimpleNamespace(config=config),
            session_ui=SimpleNamespace(config=config),
            system_event_bus=bus,
            mode_input_router=SimpleNamespace(lint_evolution_hook=None),
            is_admin=lambda chat_id: int(chat_id) == 1,
            is_user=lambda chat_id: int(chat_id) in {1},
            iter_mode_runtimes=lambda: [runtime],
        ),
        session,
        runtime,
        bus,
    )


def test_runtime_reload_applies_typed_validated_config_and_publishes_event(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="old-token"))
    previous = _build_initial_config(path)
    bot_app, session, runtime, bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(AppRuntimeService.EVENT_RELOADED, _capture)

    _write_config(path, _payload(tmp_path, token="fresh-token", extra_tool="backup"))
    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert bot_app.config.telegram.token == "old-token"
    assert bot_app.config.thread_mode.mode == "group"
    assert bot_app.manager.config is bot_app.config
    assert bot_app.git.config is bot_app.config
    assert bot_app.mcp.config is bot_app.config
    assert bot_app.session_ui.config is bot_app.config
    assert session.config is bot_app.config
    assert session.tool is bot_app.config.tools["dummy"]
    assert runtime.calls == [bot_app.config]
    assert len(events) == 1
    event_name, payload = events[0]
    assert event_name == AppRuntimeService.EVENT_RELOADED
    assert payload["path"] == str(path)
    assert payload["status"] == "success_with_warnings"
    assert payload["restart_required"] == ["telegram.token"]
    assert payload["warnings"] == ["Some changes require process restart."]
    assert payload["applied"] == ["tools.*"]


def test_runtime_reload_closes_idle_tmux_when_tool_backend_changes_to_headless(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _with_tmux_tool(_payload(tmp_path, token="stable-token"), backend="tmux"))
    previous = _build_initial_config(path)
    bot_app, session, _runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)
    closed: list[tuple[str, str, bool]] = []

    async def _close(_backend, close_session):
        closed.append((close_session.tool.name, close_session.cli.active_cli, close_session.config is previous))

    monkeypatch.setattr(TmuxExecutionBackend, "close", _close)
    _write_config(path, _with_tmux_tool(_payload(tmp_path, token="stable-token"), backend="headless"))

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert "session.s1.tmux_closed" in result["applied"]
    assert closed == [("dummy", "dummy", True)]
    assert session.tool is bot_app.config.tools["dummy"]
    assert session.tool.default_execution_backend == "headless"


def test_runtime_reload_closes_removed_active_tmux_tool_before_fallback_switch(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    initial = _with_tmux_tool(_payload(tmp_path, token="stable-token", extra_tool="backup"), backend="tmux")
    _write_config(path, initial)
    previous = _build_initial_config(path)
    bot_app, session, _runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)
    closed: list[tuple[str, str, bool]] = []

    async def _close(_backend, close_session):
        closed.append((close_session.tool.name, close_session.cli.active_cli, close_session.config is previous))

    monkeypatch.setattr(TmuxExecutionBackend, "close", _close)
    updated = _payload(tmp_path, token="stable-token", extra_tool="backup")
    updated["tools"].pop("dummy")
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert "session.s1.tmux_closed" in result["applied"]
    assert "session.s1.active_cli->backup" in result["applied"]
    assert closed == [("dummy", "dummy", True)]
    assert session.cli.active_cli == "backup"
    assert session.tool is bot_app.config.tools["backup"]


def test_runtime_reload_warns_instead_of_closing_busy_tmux_session(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _with_tmux_tool(_payload(tmp_path, token="stable-token"), backend="tmux"))
    previous = _build_initial_config(path)
    bot_app, session, _runtime, _bus = _build_bot_app(previous)
    session.busy = True
    service = AppRuntimeService(bot_app)
    closed: list[str] = []

    async def _close(_backend, close_session):
        closed.append(close_session.id)

    monkeypatch.setattr(TmuxExecutionBackend, "close", _close)
    _write_config(path, _with_tmux_tool(_payload(tmp_path, token="stable-token"), backend="headless"))

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert "session.s1.tmux_backend" in result["restart_required"]
    assert result["warnings"] == ["Some changes require process restart."]
    assert closed == []


def test_runtime_reload_closes_idle_tmux_even_when_tick_is_recent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _with_tmux_tool(_payload(tmp_path, token="stable-token"), backend="tmux"))
    previous = _build_initial_config(path)
    bot_app, session, _runtime, _bus = _build_bot_app(previous)
    session.is_active_by_tick = lambda: True
    service = AppRuntimeService(bot_app)
    closed: list[str] = []

    async def _close(_backend, close_session):
        closed.append(close_session.id)

    monkeypatch.setattr(TmuxExecutionBackend, "close", _close)
    _write_config(path, _with_tmux_tool(_payload(tmp_path, token="stable-token"), backend="headless"))

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert "session.s1.tmux_closed" in result["applied"]
    assert "session.s1.tmux_backend" not in result["restart_required"]
    assert closed == ["s1"]


def test_runtime_reload_keeps_previous_config_and_logs_error_on_invalid_validation(tmp_path, caplog) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="stable-token"))
    previous = _build_initial_config(path)
    bot_app, session, runtime, bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(AppRuntimeService.EVENT_RELOAD_FAILED, _capture)
    invalid = _payload(tmp_path, token="broken-token")
    invalid["thread_mode"]["topics_chat_id"] = None
    _write_config(path, invalid)

    caplog.set_level(logging.ERROR, logger="miniapp")
    result = asyncio.run(service.reload_runtime_config())

    assert result == {
        "status": "error",
        "applied": [],
        "restart_required": [],
        "warnings": ["Config validation failed. Previous config kept."],
    }
    assert bot_app.config is previous
    assert bot_app.manager.config is previous
    assert bot_app.git.config is previous
    assert bot_app.mcp.config is previous
    assert bot_app.session_ui.config is previous
    assert session.config is previous
    assert runtime.calls == []
    assert "validated config load failed path=" in caplog.text
    assert "topics_chat_id is required when mode='group'" in caplog.text
    assert events == [
        (
            AppRuntimeService.EVENT_RELOAD_FAILED,
            {
                "path": str(path),
                "status": "error",
                "applied": [],
                "restart_required": [],
                "warnings": ["Config validation failed. Previous config kept."],
            },
        )
    ]


def test_runtime_reload_reports_restart_required_defaults_keys(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="stable-token"))
    previous = _build_initial_config(path)
    bot_app, _session, runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, token="stable-token")
    updated["defaults"]["run_artifacts_enabled"] = False
    updated["defaults"]["run_artifacts_retention_days"] = 14
    updated["defaults"]["memory_events_enabled"] = True
    updated["defaults"]["memory_native_cli_hooks_enabled"] = True
    updated["defaults"]["memory_outcomes_enabled"] = True
    updated["defaults"]["memory_dreaming_enabled"] = True
    updated["defaults"]["memory_events_retention_days"] = 14
    updated["defaults"]["memory_events_max_payload_chars"] = 4096
    updated["defaults"]["memory_events_redaction_enabled"] = False
    updated["defaults"]["memory_dreaming_batch_size"] = 5
    updated["defaults"]["run_doctor_enabled"] = False
    updated["defaults"]["run_boundary_validation_enabled"] = False
    updated["defaults"]["run_metrics_enabled"] = False
    updated["defaults"]["skill_discovery_mode"] = "auto"
    updated["defaults"]["skill_install_policy"] = "admin_approve"
    updated["defaults"]["skill_registry_paths"] = [".cli-proxy/skills", ".cli-proxy/project-skills"]
    updated["defaults"]["skill_allowlisted_sources"] = [
        "local:global-registry",
        "registry:npx-skills",
    ]
    updated["defaults"]["gemini_oauth_client_secret"] = "new-gemini-secret"
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success_with_warnings"
    assert result["applied"] == []
    assert "defaults.run_artifacts_enabled" in result["restart_required"]
    assert "defaults.run_artifacts_retention_days" in result["restart_required"]
    assert "defaults.memory_events_enabled" in result["restart_required"]
    assert "defaults.memory_native_cli_hooks_enabled" in result["restart_required"]
    assert "defaults.memory_outcomes_enabled" in result["restart_required"]
    assert "defaults.memory_dreaming_enabled" in result["restart_required"]
    assert "defaults.memory_events_retention_days" in result["restart_required"]
    assert "defaults.memory_events_max_payload_chars" in result["restart_required"]
    assert "defaults.memory_events_redaction_enabled" in result["restart_required"]
    assert "defaults.memory_dreaming_batch_size" in result["restart_required"]
    assert "defaults.run_doctor_enabled" in result["restart_required"]
    assert "defaults.run_boundary_validation_enabled" in result["restart_required"]
    assert "defaults.run_metrics_enabled" in result["restart_required"]
    assert "defaults.skill_discovery_mode" in result["restart_required"]
    assert "defaults.skill_install_policy" in result["restart_required"]
    assert "defaults.skill_registry_paths" in result["restart_required"]
    assert "defaults.skill_allowlisted_sources" in result["restart_required"]
    assert "defaults.gemini_oauth_client_secret" in result["restart_required"]
    assert bot_app.config.defaults.skill_discovery_mode == "suggest"
    assert bot_app.config.defaults.skill_install_policy == "manual"
    assert bot_app.config.defaults.skill_registry_paths == [".cli-proxy/skills"]
    assert bot_app.config.defaults.gemini_oauth_client_secret is None
    assert runtime.calls == [bot_app.config]


def test_runtime_reload_ignores_cached_memory_store_on_config(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="stable-token"))
    previous = _build_initial_config(path)
    setattr(previous, "_shared_memory_event_store", MemoryEventStore.from_config(previous))
    bot_app, _session, runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, token="stable-token")
    updated["defaults"]["assistant_preview_enabled"] = True
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert bot_app.config.defaults.assistant_preview_enabled is True
    assert not hasattr(bot_app.config, "_shared_memory_event_store")
    assert runtime.calls == [bot_app.config]


def test_runtime_reload_applies_reloadable_assistant_preview_flag(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="stable-token"))
    previous = _build_initial_config(path)
    bot_app, _session, runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, token="stable-token")
    updated["defaults"]["assistant_preview_enabled"] = True
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert result["applied"] == ["defaults.assistant_preview_enabled"]
    assert result["restart_required"] == []
    assert bot_app.config.defaults.assistant_preview_enabled is True
    assert runtime.calls == [bot_app.config]


def test_runtime_reload_applies_reloadable_pending_input_confirmation_flag(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="stable-token"))
    previous = _build_initial_config(path)
    bot_app, _session, runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, token="stable-token")
    updated["defaults"]["pending_input_confirmation_enabled"] = False
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert result["applied"] == ["defaults.pending_input_confirmation_enabled"]
    assert result["restart_required"] == []
    assert bot_app.config.defaults.pending_input_confirmation_enabled is False
    assert runtime.calls == [bot_app.config]


def test_runtime_reload_applies_reloadable_default_secret_fields(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    initial_payload = _payload(tmp_path, token="stable-token")
    initial_payload["defaults"]["openai_api_key"] = "old-openai"
    initial_payload["defaults"]["github_token"] = "old-github"
    _write_config(path, initial_payload)
    previous = _build_initial_config(path)
    bot_app, _session, runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, token="stable-token")
    updated["defaults"]["openai_api_key"] = "new-openai"
    updated["defaults"]["github_token"] = "new-github"
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert "defaults.openai_api_key" in result["applied"]
    assert "defaults.github_token" in result["applied"]
    assert result["restart_required"] == []
    assert bot_app.config.defaults.openai_api_key == "new-openai"
    assert bot_app.config.defaults.github_token == "new-github"
    assert runtime.calls == [bot_app.config]


def test_runtime_reload_updates_lint_evolution_hook(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="stable-token"))
    previous = _build_initial_config(path)
    bot_app, _session, runtime, _bus = _build_bot_app(previous)
    service = AppRuntimeService(bot_app)

    updated = _payload(tmp_path, token="stable-token")
    updated["lint_evolution"] = {"enabled": True}
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    assert result["status"] == "success"
    assert result["applied"] == ["lint_evolution"]
    assert result["restart_required"] == []
    assert bot_app.config.lint_evolution.enabled is True
    assert callable(bot_app.mode_input_router.lint_evolution_hook)
    assert runtime.calls == [bot_app.config]


def test_runtime_reload_uses_current_file_contents_on_sequential_calls(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, _payload(tmp_path, token="token-a"))
    initial = _build_initial_config(path)
    bot_app, _session, runtime, bus = _build_bot_app(initial)
    service = AppRuntimeService(bot_app)
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(AppRuntimeService.EVENT_RELOADED, _capture)

    _write_config(path, _payload(tmp_path, token="token-b"))
    first = asyncio.run(service.reload_runtime_config())

    _write_config(path, _payload(tmp_path, token="token-c", extra_tool="aux"))
    second = asyncio.run(service.reload_runtime_config())

    assert first["status"] == "success_with_warnings"
    assert second["status"] == "success_with_warnings"
    assert runtime.calls[0].telegram.token == "token-a"
    assert runtime.calls[1].telegram.token == "token-a"
    assert bot_app.config.telegram.token == "token-a"
    assert "aux" in bot_app.config.tools
    assert [payload["status"] for _event, payload in events] == ["success_with_warnings", "success_with_warnings"]
    assert [runtime_cfg.telegram.token for runtime_cfg in runtime.calls] == ["token-a", "token-a"]


def test_runtime_reload_switches_active_cli_when_removed(tmp_path) -> None:
    """Test that hot reload switches to available CLI when active CLI is removed."""
    path = tmp_path / "config.yaml"
    # Start with two tools, session using the first one
    _write_config(path, _payload(tmp_path, token="stable-token", extra_tool="backup"))
    initial = _build_initial_config(path)
    session = _DummySession(initial, active_cli="dummy")
    bot_app, session, runtime, bus = _build_bot_app(initial)
    service = AppRuntimeService(bot_app)
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    bus.subscribe(AppRuntimeService.EVENT_RELOADED, _capture)

    # Remove the active CLI ("dummy"), leaving only "backup"
    updated = _payload(tmp_path, token="stable-token", extra_tool="backup")
    del updated["tools"]["dummy"]
    _write_config(path, updated)

    result = asyncio.run(service.reload_runtime_config())

    # Session should be switched to the remaining tool ("backup")
    assert session.active_cli == "backup"
    assert session.cli.active_cli == "backup"
    assert session.tool is bot_app.config.tools["backup"]
    assert "dummy" not in bot_app.config.tools
    assert "backup" in bot_app.config.tools

    # Result should indicate success with the session update applied
    assert result["status"] == "success"
    assert "session.s1.active_cli->backup" in result["applied"]
    assert "tools.*" in result["applied"]
    assert result["restart_required"] == []

    # Event should be published
    assert len(events) == 1
    assert events[0][0] == AppRuntimeService.EVENT_RELOADED
    assert events[0][1]["status"] == "success"
