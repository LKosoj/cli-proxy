import asyncio

import pytest

from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ToolResult
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager, session_runtime_uid


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig):
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):
        current = self.config
        for part in str(key or "").split("."):
            token = part.strip()
            if not token:
                continue
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
                continue
            if not hasattr(current, token):
                return default
            current = getattr(current, token)
        return current


def _build_config(
    tmp_path,
    *,
    dummy_enabled: bool = True,
    include_backup: bool = False,
    backup_enabled: bool = True,
    whitelist_chat_ids: list[int] | None = None,
    admlist_chat_ids: list[int] | None = None,
    user_workdirs: dict[int, list[str]] | None = None,
    user_modes: dict[int, str | list[str]] | None = None,
) -> AppConfig:
    tools = {
        "dummy": ToolConfig(
            name="dummy",
            mode="headless",
            cmd=["bash", "-lc", "cat"],
            enabled=dummy_enabled,
        )
    }
    if include_backup:
        tools["backup"] = ToolConfig(
            name="backup",
            mode="headless",
            cmd=["bash", "-lc", "cat"],
            enabled=backup_enabled,
        )
    return AppConfig(
        telegram=TelegramConfig(
            token="t",
            whitelist_chat_ids=list(whitelist_chat_ids or [1]),
            admlist_chat_ids=list(admlist_chat_ids or [1]),
            user_workdirs=dict(user_workdirs or {}),
            user_modes=dict(user_modes or {}),
        ),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


@pytest.mark.asyncio
async def test_facade_run_session_input_falls_back_to_prompt_when_no_mode(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=SessionService(SessionManager(cfg), TaskService()),
        task_service=TaskService(),
        git_service=None,
        mode_registry_service=None,
    )
    session = facade.session_service.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    called = {"prompt": 0}

    async def _fake_run_prompt(prompt: str, *args, **kwargs):
        called["prompt"] += 1
        return "PROMPT:" + prompt

    session.run_prompt = _fake_run_prompt  # type: ignore[assignment]

    events: list[str] = []
    facade.subscribe(lambda note: events.append(note.event))

    out = await facade.run_session_input(session_uid, "hi")
    assert out == "PROMPT:hi"
    assert called["prompt"] == 1
    assert "task:started" in events
    assert "task:completed" in events


@pytest.mark.asyncio
async def test_facade_run_session_input_blocks_direct_cli_for_non_admin_without_direct_cli(tmp_path) -> None:
    cfg = _build_config(
        tmp_path,
        whitelist_chat_ids=[1],
        admlist_chat_ids=[999],
        user_workdirs={1: [str(tmp_path)]},
        user_modes={1: ["agent"]},
    )
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=SessionService(SessionManager(cfg), TaskService()),
        task_service=TaskService(),
        git_service=None,
        mode_registry_service=None,
    )
    facade.config = cfg
    session = facade.session_service.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    called = {"prompt": 0}

    async def _fake_run_prompt(prompt: str, *args, **kwargs):
        _ = (prompt, args, kwargs)
        called["prompt"] += 1
        return "PROMPT"

    session.run_prompt = _fake_run_prompt  # type: ignore[assignment]

    notes: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: notes.append((note.event, dict(note.payload))))

    out = await facade.run_session_input(session_uid, "hi")

    assert out == ""
    assert called["prompt"] == 0
    blocked_messages = [payload for event, payload in notes if event == "ui:message"]
    assert blocked_messages
    assert blocked_messages[-1]["session_id"] == session_uid
    assert blocked_messages[-1]["text"] == "Прямой CLI недоступен для вашего пользователя."


@pytest.mark.asyncio
async def test_facade_run_session_input_notifies_and_uses_fallback_cli_after_restore(tmp_path) -> None:
    initial_cfg = _build_config(tmp_path, dummy_enabled=True, include_backup=True, backup_enabled=True)
    initial_sessions = SessionService(SessionManager(initial_cfg), TaskService())
    initial_session = initial_sessions.create_session(1, "dummy", str(tmp_path))

    restored_cfg = _build_config(tmp_path, dummy_enabled=False, include_backup=True, backup_enabled=True)
    task_service = TaskService()
    sessions = SessionService(SessionManager(restored_cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(restored_cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=None,
    )
    facade.config = restored_cfg

    session = sessions.get_session(1, initial_session.id)
    assert session is not None
    session_uid = session_runtime_uid(session)

    async def _fake_run_prompt(prompt: str, *args, **kwargs):
        _ = args
        _ = kwargs
        return f"PROMPT:{session.tool.name}:{prompt}"

    session.run_prompt = _fake_run_prompt  # type: ignore[assignment]

    notes: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: notes.append((note.event, note.payload)))

    out = await facade.run_session_input(session_uid, "hi")

    assert out == "PROMPT:backup:hi"
    assert session.active_cli == "backup"
    switch_notes = [
        payload
        for event, payload in notes
        if event == "ui:message" and "Переключаю на backup" in str(payload.get("text") or "")
    ]
    assert switch_notes
    assert switch_notes[0].get("session_uid") == session_uid


@pytest.mark.asyncio
async def test_facade_run_session_input_emits_and_clears_assistant_preview(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    cfg.defaults.assistant_preview_enabled = True
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=SessionService(SessionManager(cfg), TaskService()),
        task_service=TaskService(),
        git_service=None,
        mode_registry_service=None,
    )
    facade.config = cfg
    session = facade.session_service.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    async def _fake_run_prompt(prompt: str, *args, **kwargs):
        _ = (prompt, args, kwargs)
        session.last_assistant_text_value = "Первая часть превью"
        await asyncio.sleep(0.45)
        session.last_assistant_text_value = "Финальная часть превью"
        await asyncio.sleep(0.05)
        return "FINAL"

    session.run_prompt = _fake_run_prompt  # type: ignore[assignment]

    notes: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: notes.append((note.event, dict(note.payload))))

    out = await facade.run_session_input(session_uid, "hi")

    assert out == "FINAL"
    preview_notes = [payload for event, payload in notes if event == "ui:assistant_preview"]
    assert preview_notes
    assert preview_notes[-1]["text"] == "⏳ Финальная часть превью"
    clear_notes = [payload for event, payload in notes if event == "ui:assistant_preview_clear"]
    assert clear_notes
    assert clear_notes[-1]["session_uid"] == session_uid


@pytest.mark.asyncio
async def test_facade_run_session_input_routes_mode_via_handle_input_when_mode_active(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    registry = ModeRegistry()
    called = {"handle_input": 0, "run_pipeline": 0}

    session = None
    session_uid = ""

    class EchoMode(BaseMode):
        mode_id = "echo"

        async def handle_input(self, message, ctx):
            called["handle_input"] += 1
            assert message.text == "hi"
            assert str(message.chat_id) == session_uid
            assert ctx.get("mode_id") == "echo"
            return ToolResult.ok("MODE:" + str(message.text))

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok()

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            called["run_pipeline"] += 1
            raise AssertionError("run_pipeline must not be called directly in Desktop input path")

    registry.register(EchoMode())
    mode_registry_service = ModeRegistryService(registry)
    mode_registry_service.initialize_plugins(config=cfg, services={})

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    sessions = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    session = sessions.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "echo"

    async def _no_prompt(*_a, **_k):
        raise AssertionError("session.run_prompt should not be called when mode is active")

    session.run_prompt = _no_prompt  # type: ignore[assignment]

    events: list[str] = []
    facade.subscribe(lambda note: events.append(note.event))

    out = await facade.run_session_input(session_uid, "hi")
    assert out == "MODE:hi"
    assert called["handle_input"] == 1
    assert called["run_pipeline"] == 0
    assert "task:started" in events
    assert "task:completed" in events


@pytest.mark.asyncio
async def test_facade_run_session_input_routes_manager_via_handle_input(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    registry = ModeRegistry()
    called = {"handle_input": 0, "run_pipeline": 0}

    session = None
    session_uid = ""

    class ManagerLikeMode(BaseMode):
        mode_id = "manager"

        async def handle_input(self, message, ctx):
            called["handle_input"] += 1
            assert message.text == "hi"
            assert str(message.chat_id) == session_uid
            assert ctx.get("mode_id") == "manager"
            pending = self.require_service("manager_pending")
            pending.set("k", {"v": 1})
            assert pending.get("k") == {"v": 1}
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok()

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            called["run_pipeline"] += 1
            raise AssertionError("manager run_pipeline must not be called directly in Desktop input path")

    registry.register(ManagerLikeMode())
    mode_registry_service = ModeRegistryService(registry)
    mode_registry_service.initialize_plugins(config=cfg, services={})

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    sessions = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    session = sessions.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "manager"

    async def _no_prompt(*_a, **_k):
        raise AssertionError("session.run_prompt should not be called when mode is active")

    session.run_prompt = _no_prompt  # type: ignore[assignment]

    out = await facade.run_session_input(session_uid, "hi")
    assert out == ""
    assert called["handle_input"] == 1
    assert called["run_pipeline"] == 0


@pytest.mark.asyncio
async def test_facade_run_session_input_offers_orchestrator_transition_when_enabled(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    registry = ModeRegistry()

    class AnalystMode(BaseMode):
        mode_id = "analyst"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok()

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            return "ANALYST:" + str(user_text)

    class ManagerMode(BaseMode):
        mode_id = "manager"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok()

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            return "MANAGER:" + str(user_text)

    registry.register(AnalystMode())
    registry.register(ManagerMode())
    mode_registry_service = ModeRegistryService(registry)
    mode_registry_service.initialize_plugins(config=cfg, services={})

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    sessions = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    session = sessions.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "analyst"
    session.orchestrator.enabled = True
    session.orchestrator.last_mode_output = "FULL PREV MODE RESULT"

    events = []
    facade.subscribe(lambda note: events.append(note))

    out = await facade.run_session_input(session_uid, "Нужно декомпозировать план работ")
    assert out == ""
    assert any(note.event == "ui:mode_menu" for note in events)
    assert isinstance(session.orchestrator.pending_input, dict)
    assert session.orchestrator.pending_input.get("text") == "FULL PREV MODE RESULT"


@pytest.mark.asyncio
async def test_facade_bridges_v2_reaction_events_to_ui(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    registry = ModeRegistry()

    class EventMode(BaseMode):
        mode_id = "event_mode"

        async def handle_input(self, message, ctx):
            _ = message
            _ = ctx
            return ToolResult.ok(
                "",
                data={
                    "v2_event": {
                        "step_id": "step-1",
                        "message": "failed",
                        "payload": {
                            "retry_count": 0,
                            "needs_input": True,
                            "reroute": True,
                            "target_mode": "analyst",
                        },
                    }
                },
            )

        async def handle_callback(self, callback, ctx):
            _ = callback
            _ = ctx
            return ToolResult.ok()

    registry.register(EventMode())
    mode_registry_service = ModeRegistryService(registry)
    mode_registry_service.initialize_plugins(config=cfg, services={})

    task_service = TaskService()
    sessions = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    session = sessions.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "event_mode"

    notes = []
    facade.subscribe(lambda note: notes.append((note.event, note.payload)))

    out = await facade.run_session_input(session_uid, "run")
    assert out == ""
    v2_events = [p for ev, p in notes if ev == "ui:v2_event"]
    assert any(str(p.get("event_type")) == "retry" for p in v2_events)
    assert any(str(p.get("event_type")) == "reroute" for p in v2_events)
    assert any(str(p.get("event_type")) == "needs_input" for p in v2_events)


@pytest.mark.asyncio
async def test_facade_bridges_validation_not_run_status_to_ui(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    registry = ModeRegistry()

    class ValidationMode(BaseMode):
        mode_id = "validation_mode"

        async def handle_input(self, message, ctx):
            _ = message
            _ = ctx
            return ToolResult.ok(
                "",
                data={
                    "validation_report": {
                        "status": "not_run",
                        "steps": [{"tool": "pytest", "status": "not_run"}],
                    }
                },
            )

        async def handle_callback(self, callback, ctx):
            _ = callback
            _ = ctx
            return ToolResult.ok()

    registry.register(ValidationMode())
    mode_registry_service = ModeRegistryService(registry)
    mode_registry_service.initialize_plugins(config=cfg, services={})

    task_service = TaskService()
    sessions = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    session = sessions.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "validation_mode"

    notes = []
    facade.subscribe(lambda note: notes.append((note.event, note.payload)))

    out = await facade.run_session_input(session_uid, "run")
    assert out == ""
    validation_events = [p for ev, p in notes if ev == "ui:validation_status"]
    assert validation_events
    assert validation_events[0].get("status") == "not_run"
