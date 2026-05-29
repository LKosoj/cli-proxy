from __future__ import annotations

import asyncio
import os

import pytest

from app.events.bus import SystemEventBus
from app.bootstrap import build_application
from app.security import SecurityFacade
from app.security.audit import EventBusAuditService
from app.services.advanced_orchestrator_service import AdvancedOrchestratorService
from app.services.artifact_intent_service import ArtifactIntentService
from app.services.cli_limits_service import CliLimitsService
from app.services.config_service import ConfigService, FileConfigProvider, RuntimeConfigValidator
from app.services.mode_launch_adapter import ModeLaunchAdapterService
from app.services.mode_run_lifecycle_service import ModeRunLifecycleService
from app.services.notification_queue_service import NotificationQueueService
from app.services.project_registry import ProjectRegistry
from app.services.remote_control_service import RemoteControlService
from app.services.run_operations_service import RunOperationsService
from app.services.scheduler_service import SchedulerService
from app.services.session_mutation_service import SessionMutationService
from app.services.session_thread_manager import SessionThreadManager
from app.services.session_thread_repository import SessionThreadRepository
from app.services.shared_http_ingress import SharedHttpIngress
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


class _DummyToolRegistry:
    pass


def _build_config(tmp_path, *, intent: str) -> AppConfig:
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
            run_artifacts_retention_days=7 if intent == "bootstrap" else 30,
            skill_discovery_mode="auto" if intent == "bootstrap" else "suggest",
            skill_install_policy="admin_approve" if intent == "bootstrap" else "manual",
            skill_registry_paths=[".cli-proxy/skills", f".cli-proxy/{intent}-skills"],
            skill_allowlisted_sources=["local:global-registry", "registry:npx-skills"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_build_application_initializes_dependency_container(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="bootstrap")
    registry = _DummyToolRegistry()
    called = {}

    async def _pipeline_fn(*_args, **_kwargs):
        return None

    def _tool_registry_factory(config: AppConfig):
        called["config"] = config
        return registry

    container = build_application(
        cfg,
        tool_registry_factory=_tool_registry_factory,
        run_mode_pipeline_fn=_pipeline_fn,
    )

    assert container.config is cfg
    assert container.plugin_registry is registry
    assert container.session_manager._state_repo is container.state_repository
    assert container.mode_registry_service.registry is container.mode_registry
    assert callable(container.session_control.persist_sessions)
    assert callable(container.session_control.cancel_mode_tasks)
    assert callable(container.session_control.cancel_session_tasks)
    assert called.get("config") is cfg
    assert os.path.exists(container.state_repository.db_path)
    assert container.mode_pipeline.run_mode_pipeline_fn is _pipeline_fn
    assert container.mode_dependencies.session_manager is container.session_manager
    assert container.mode_dependencies.registry is container.mode_registry_service
    assert container.mode_dependencies.pipeline is container.mode_pipeline
    assert container.run_artifacts is container.mode_dependencies.run_artifacts
    assert container.run_observability is container.mode_dependencies.run_observability
    assert container.run_doctor is container.mode_dependencies.run_doctor
    assert container.run_boundary_validation is container.mode_dependencies.run_boundary_validation
    assert isinstance(container.mode_run_lifecycle, ModeRunLifecycleService)
    assert container.mode_run_lifecycle is container.mode_dependencies.mode_run_lifecycle
    assert container.mode_run_lifecycle.artifact_store is container.run_artifacts.artifact_store
    assert container.mode_run_lifecycle.observability is container.run_observability
    assert container.mode_run_lifecycle.boundary_validator is container.run_boundary_validation
    assert container.skill_runtime is container.mode_dependencies.skill_runtime
    assert isinstance(container.session_mutation_service, SessionMutationService)
    assert container.mode_dependencies.session_mutation_service is container.session_mutation_service
    assert container.run_artifacts.retention_window_days() == 7
    assert container.skill_runtime.allows_auto_discovery() is True
    assert container.skill_runtime.install_policy == "admin_approve"
    assert isinstance(container.system_event_bus, SystemEventBus)
    assert isinstance(container.security, SecurityFacade)
    assert isinstance(container.shared_http_ingress, SharedHttpIngress)
    assert isinstance(container.scheduler_service, SchedulerService)
    assert isinstance(container.mode_launch_adapter_service, ModeLaunchAdapterService)
    assert isinstance(container.config_provider, FileConfigProvider)
    assert isinstance(container.runtime_config_validator, RuntimeConfigValidator)
    assert isinstance(container.config_service, ConfigService)
    assert container.config_service.provider is container.config_provider
    assert container.config_service.validator is container.runtime_config_validator
    assert isinstance(container.run_operations_service, RunOperationsService)
    assert isinstance(container.remote_control_service, RemoteControlService)
    assert isinstance(container.notification_queue_service, NotificationQueueService)
    assert isinstance(container.cli_limits_service, CliLimitsService)
    assert isinstance(container.project_registry, ProjectRegistry)
    assert isinstance(container.session_thread_repository, SessionThreadRepository)
    assert isinstance(container.session_thread_manager, SessionThreadManager)
    assert container.session_thread_manager._repository is container.session_thread_repository
    assert container.session_thread_manager._session_manager is container.session_manager
    assert container.session_thread_manager._config is cfg.thread_mode
    assert isinstance(container.advanced_orchestrator_service, AdvancedOrchestratorService)
    assert isinstance(container.artifact_intent_service, ArtifactIntentService)


def test_build_application_creates_security_and_shared_http_ingress_from_config(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="bootstrap_security_http")
    user_workdir = tmp_path / "user_project"
    user_workdir.mkdir()
    cfg.telegram.whitelist_chat_ids = [1, 2]
    cfg.telegram.user_workdirs = {2: [str(user_workdir)]}
    cfg.miniapp = MiniAppConfig(
        enabled=True,
        bind_host="127.0.0.2",
        bind_port=8099,
        max_edit_file_size_kb=2048,
    )

    container = build_application(cfg, tool_registry_factory=lambda _cfg: _DummyToolRegistry())
    events: list[tuple[str, dict]] = []

    async def _capture(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    container.system_event_bus.subscribe(EventBusAuditService.EVENT_NAME, _capture)

    assert container.security.authorize(1, require_admin=True).allowed is True
    user_decision = container.security.authorize(2, scope="files")
    assert user_decision.allowed is True
    assert user_decision.is_user is True
    assert container.security.authorize(3, scope="files").allowed is False
    assert container.shared_http_ingress.host == "127.0.0.2"
    assert container.shared_http_ingress.port == 8099
    assert container.shared_http_ingress.client_max_size == (
        (2048 * 1024 * SharedHttpIngress.MINIAPP_REQUEST_BODY_EXPANSION_FACTOR)
        + SharedHttpIngress.MINIAPP_REQUEST_BODY_OVERHEAD_BYTES
    )

    asyncio.run(
        container.security.emit_audit(
            category="bootstrap",
            action="security.container",
            status="ok",
            user_id=1,
            context={"chat_id": 1},
        )
    )

    assert len(events) == 1
    assert events[0][0] == EventBusAuditService.EVENT_NAME
    assert events[0][1]["action"] == "security.container"


def test_build_application_creates_scheduler_and_mode_launch_from_container_dependencies(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="bootstrap_scheduler_mode_launch")
    runtime = object()
    container = build_application(
        cfg,
        tool_registry_factory=lambda _cfg: _DummyToolRegistry(),
        bot_app_provider=lambda: runtime,
    )

    assert container.scheduler_service._repository is container.scheduled_job_repository
    assert container.scheduler_service._event_bus is container.system_event_bus
    assert container.scheduler_service._config is cfg.scheduler
    assert container.mode_launch_adapter_service.bot_app is runtime


@pytest.mark.asyncio
async def test_build_application_creates_run_operations_with_late_bound_executor(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="bootstrap_run_operations")
    calls = []

    class _Runtime:
        async def _execute_recommended_run_action(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return {"ok": True}

    runtime = _Runtime()
    container = build_application(
        cfg,
        tool_registry_factory=lambda _cfg: _DummyToolRegistry(),
        bot_app_provider=lambda: runtime,
    )

    executor = container.run_operations_service.recommended_action_executor
    assert executor is not None
    result = await executor("session", action="run_validate")

    assert result == {"ok": True}
    assert calls == [{"args": ("session",), "kwargs": {"action": "run_validate"}}]
    assert container.run_operations_service.artifact_store is container.run_doctor.artifact_store
    assert container.run_operations_service.doctor_service is container.run_doctor
    assert container.run_operations_service.observability_service is container.run_observability


def test_application_container_services_have_no_legacy_for_bot_app_bridges() -> None:
    assert not hasattr(SecurityFacade, "for_bot_app")
    assert not hasattr(SharedHttpIngress, "for_bot_app")
    assert not hasattr(SchedulerService, "for_bot_app")
    assert not hasattr(ModeLaunchAdapterService, "for_bot_app")
    assert not hasattr(SessionThreadManager, "for_bot_app")
    assert not hasattr(ConfigService, "for_bot_app")
    assert not hasattr(FileConfigProvider, "for_bot_app")
    assert not hasattr(RuntimeConfigValidator, "for_bot_app")


def test_build_application_exposes_run_artifact_store_api_through_foundation_service(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="bootstrap_run_artifacts")
    container = build_application(cfg, tool_registry_factory=lambda _cfg: _DummyToolRegistry())
    session = container.session_manager.create(chat_id=1, tool_name="dummy", workdir=cfg.defaults.workdir)

    run = container.run_artifacts.start_run(
        session=session,
        mode_id="codebase_mapper",
        phase="operation",
    )
    state = container.run_artifacts.load_state(run)

    assert run.mode_id == "codebase_mapper"
    assert state["phase"] == "operation"
    assert os.path.exists(run.state_path)


@pytest.mark.asyncio
async def test_build_application_wires_session_control_cancellation(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="cancel")
    container = build_application(cfg, tool_registry_factory=lambda _cfg: _DummyToolRegistry())

    started = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        await asyncio.sleep(60)

    container.mode_tasks.create(
        session_id="s1",
        mode_id="manager",
        coro=_long_running(),
        name="long-running",
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)

    cancelled = await container.session_control.cancel_mode(
        session_id="s1",
        mode_id="manager",
        timeout_s=0.1,
    )

    assert cancelled == 1
    assert container.mode_tasks.list(session_id="s1", mode_id="manager") == []


def test_build_application_isolates_state_between_sequential_runs(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="intent_a")
    cfg_b = _build_config(tmp_path, intent="intent_b")

    container_a = build_application(cfg_a, tool_registry_factory=lambda _cfg: _DummyToolRegistry())
    created_a = container_a.session_manager.create(chat_id=1, tool_name="dummy", workdir=cfg_a.defaults.workdir)
    assert created_a.id == "s1"
    # Replaced deprecated get_single_session_for_chat with sessions_for_chat check
    assert list(container_a.session_manager.sessions_for_chat(1).values()) == [created_a]

    container_b = build_application(cfg_b, tool_registry_factory=lambda _cfg: _DummyToolRegistry())
    # Replaced deprecated get_single_session_for_chat with sessions_for_chat check
    assert container_b.session_manager.sessions_for_chat(1) == {}
    assert container_b.session_manager.sessions_for_chat(1) == {}
    assert container_a.run_artifacts is not container_b.run_artifacts
    assert container_a.mode_run_lifecycle is not container_b.mode_run_lifecycle
    assert container_a.skill_runtime is not container_b.skill_runtime
    assert container_a.skill_runtime.registry_path_list() != container_b.skill_runtime.registry_path_list()

    created_b = container_b.session_manager.create(chat_id=1, tool_name="dummy", workdir=cfg_b.defaults.workdir)
    assert created_b.id == "s1"
    assert len(container_a.session_manager.sessions_for_chat(1)) == 1
    assert len(container_b.session_manager.sessions_for_chat(1)) == 1
