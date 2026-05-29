from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from config import AppConfig
from modes.registry import ModeLoader, ModeRegistry
from modes.sdk import ModePipelineService, ModeRegistryService, SessionControlService, TaskService
from modes.sdk.services.runtime import RunModePipelineFn
from modes.sdk.runtime.tooling.registry import ToolRegistry, get_tool_registry
from session import SessionManager

from app.mode_dependencies import (
    ModeDependencies,
    ModeFoundationServices,
    ModeRunLifecycleService,
    RunArtifactsService,
    RunBoundaryValidationService,
    RunDoctorService,
    RunObservabilityService,
    SkillRuntimeService,
    build_mode_foundation_services,
)
from app.events.bus import SystemEventBus
from app.security import SecurityFacade
from app.services.advanced_orchestrator_service import AdvancedOrchestratorService
from app.services.artifact_intent_service import ArtifactIntentService
from app.services.cli_limits_service import CliLimitsService
from app.services.config_service import ConfigService, FileConfigProvider, RuntimeConfigValidator
from app.services.mode_launch_adapter import ModeLaunchAdapterService, ModeLaunchPolicy
from app.services.notification_queue_service import NotificationQueueService
from app.services.project_registry import ProjectRegistry
from app.services.remote_control_service import RemoteControlService
from app.services.run_operations_service import RunOperationsService
from app.services.scheduled_job_repository import ScheduledJobRecord, ScheduledJobRepository
from app.services.scheduler_service import SchedulerService
from app.services.session_mutation_service import SessionMutationService
from app.services.session_thread_manager import SessionThreadManager
from app.services.session_thread_repository import SessionThreadRepository
from app.services.shared_http_ingress import SharedHttpIngress
from app.services.ssh_service import SSHService
from app.services.state_repository import JsonStateRepository, get_state_repository
from app.services.webhook_delivery_repository import WebhookDeliveryRepository

logger = logging.getLogger(__name__)


@dataclass
class ApplicationContainer:
    """Dependency container for bot/runtime bootstrap."""

    config: AppConfig
    state_repository: JsonStateRepository
    session_manager: SessionManager
    mode_registry: ModeRegistry
    mode_loader: ModeLoader
    mode_registry_service: ModeRegistryService
    mode_pipeline: ModePipelineService
    mode_dependencies: ModeDependencies
    run_artifacts: RunArtifactsService
    run_observability: RunObservabilityService
    run_doctor: RunDoctorService
    run_boundary_validation: RunBoundaryValidationService
    mode_run_lifecycle: ModeRunLifecycleService
    skill_runtime: SkillRuntimeService
    plugin_registry: ToolRegistry
    mode_tasks: TaskService
    session_control: SessionControlService
    session_mutation_service: SessionMutationService
    webhook_delivery_repository: WebhookDeliveryRepository
    scheduled_job_repository: ScheduledJobRepository
    scheduled_jobs: list[ScheduledJobRecord]
    ssh_service: SSHService
    system_event_bus: SystemEventBus
    security: SecurityFacade
    shared_http_ingress: SharedHttpIngress
    scheduler_service: SchedulerService
    mode_launch_adapter_service: ModeLaunchAdapterService
    config_provider: FileConfigProvider
    runtime_config_validator: RuntimeConfigValidator
    config_service: ConfigService
    run_operations_service: RunOperationsService
    remote_control_service: RemoteControlService
    notification_queue_service: NotificationQueueService
    cli_limits_service: CliLimitsService
    project_registry: ProjectRegistry
    session_thread_repository: SessionThreadRepository
    session_thread_manager: SessionThreadManager
    advanced_orchestrator_service: AdvancedOrchestratorService
    artifact_intent_service: ArtifactIntentService


ToolRegistryFactory = Callable[[AppConfig], ToolRegistry]


def _build_security(config: AppConfig, *, system_event_bus: SystemEventBus) -> SecurityFacade:
    try:
        return SecurityFacade.from_app_config(config, system_event_bus=system_event_bus)
    except Exception:
        logger.exception("security init failed during bootstrap")
        raise


def _build_shared_http_ingress(config: AppConfig) -> SharedHttpIngress:
    try:
        return SharedHttpIngress.from_config(config)
    except Exception:
        logger.exception("shared http ingress init failed during bootstrap")
        raise


def _build_scheduler_service(
    config: AppConfig,
    *,
    scheduled_job_repository: ScheduledJobRepository,
    system_event_bus: SystemEventBus,
) -> SchedulerService:
    try:
        return SchedulerService(
            repository=scheduled_job_repository,
            event_bus=system_event_bus,
            scheduler_config=config.scheduler,
        )
    except Exception:
        logger.exception("scheduler service init failed during bootstrap")
        raise


def _build_mode_launch_adapter_service(
    *,
    mode_registry_service: ModeRegistryService,
    bot_app_provider: Callable[[], object | None] | None,
) -> ModeLaunchAdapterService:
    try:
        return ModeLaunchAdapterService(
            bot_app_provider=bot_app_provider,
            policy=ModeLaunchPolicy.for_mode_registry(mode_registry_service),
        )
    except Exception:
        logger.exception("mode launch adapter init failed during bootstrap")
        raise


def _build_config_provider(config: AppConfig) -> FileConfigProvider:
    try:
        config_path = str(getattr(config, "path", "") or "config.yaml")
        return FileConfigProvider(config_path)
    except Exception:
        logger.exception("config provider init failed during bootstrap")
        raise


def _build_runtime_config_validator() -> RuntimeConfigValidator:
    try:
        return RuntimeConfigValidator()
    except Exception:
        logger.exception("runtime config validator init failed during bootstrap")
        raise


def _build_config_service(
    *,
    provider: FileConfigProvider,
    validator: RuntimeConfigValidator,
) -> ConfigService:
    try:
        return ConfigService(
            provider,
            validator=validator,
        )
    except Exception:
        logger.exception("config service init failed during bootstrap")
        raise


def _build_run_operations_service(
    foundation_services: ModeFoundationServices,
    *,
    bot_app_provider: Callable[[], object | None] | None,
) -> RunOperationsService:
    try:
        recommended_action_executor = None
        if bot_app_provider is not None:
            async def _recommended_action_executor(*args, **kwargs):
                bot_app = bot_app_provider()
                executor = getattr(bot_app, "_execute_recommended_run_action", None)
                if not callable(executor):
                    raise RuntimeError("BotApp recommended action executor is not available")
                return await executor(*args, **kwargs)

            recommended_action_executor = _recommended_action_executor

        return RunOperationsService(
            enabled=bool(
                foundation_services.run_artifacts.is_enabled()
                and foundation_services.run_doctor.is_enabled()
            ),
            artifact_store=foundation_services.run_doctor.artifact_store,
            doctor_service=foundation_services.run_doctor,
            observability_service=foundation_services.run_observability,
            recommended_action_executor=recommended_action_executor,
        )
    except Exception:
        logger.exception("run operations service init failed during bootstrap")
        raise


def _build_remote_control_service() -> RemoteControlService:
    try:
        return RemoteControlService()
    except Exception:
        logger.exception("remote control service init failed during bootstrap")
        raise


def _build_notification_queue_service() -> NotificationQueueService:
    try:
        return NotificationQueueService(
            logger=logging.getLogger("app.services.notification_queue_service")
        )
    except Exception:
        logger.exception("notification queue service init failed during bootstrap")
        raise


def _build_cli_limits_service(config: AppConfig) -> CliLimitsService:
    try:
        return CliLimitsService(
            gemini_oauth_client_secret=config.defaults.gemini_oauth_client_secret,
        )
    except Exception:
        logger.exception("cli limits service init failed during bootstrap")
        raise


def _build_project_registry(config: AppConfig) -> ProjectRegistry:
    try:
        return ProjectRegistry(config.defaults.state_path)
    except Exception:
        logger.exception("project registry init failed during bootstrap")
        raise


def _build_session_thread_repository(config: AppConfig) -> SessionThreadRepository:
    try:
        return SessionThreadRepository(config.defaults.state_path)
    except Exception:
        logger.exception("session thread repository init failed during bootstrap")
        raise


def _build_session_thread_manager(
    config: AppConfig,
    *,
    repository: SessionThreadRepository,
    session_manager: SessionManager,
) -> SessionThreadManager:
    try:
        return SessionThreadManager(
            repository=repository,
            session_manager=session_manager,
            thread_mode_config=config.thread_mode,
        )
    except Exception:
        logger.exception("session thread manager init failed during bootstrap")
        raise


def _build_advanced_orchestrator_service() -> AdvancedOrchestratorService:
    try:
        return AdvancedOrchestratorService()
    except Exception:
        logger.exception("advanced orchestrator service init failed during bootstrap")
        raise


def _build_artifact_intent_service() -> ArtifactIntentService:
    try:
        return ArtifactIntentService()
    except Exception:
        logger.exception("artifact intent service init failed during bootstrap")
        raise


def build_application(
    config: AppConfig,
    *,
    tool_registry_factory: ToolRegistryFactory = get_tool_registry,
    run_mode_pipeline_fn: RunModePipelineFn | None = None,
    bot_app_provider: Callable[[], object | None] | None = None,
) -> ApplicationContainer:
    """
    Build application dependency container in a deterministic order:
    1) DB/repository
    2) Session manager
    3) Mode registry + loader
    4) Plugin tool registry
    5) Session control wiring
    """

    state_repository = get_state_repository(config.defaults.state_path)
    webhook_delivery_repository = WebhookDeliveryRepository(config.defaults.state_path)
    scheduled_job_repository = ScheduledJobRepository(config.defaults.state_path)
    scheduled_jobs = scheduled_job_repository.list_jobs(enabled_only=True)
    system_event_bus = SystemEventBus()
    security = _build_security(config, system_event_bus=system_event_bus)
    shared_http_ingress = _build_shared_http_ingress(config)
    config_provider = _build_config_provider(config)
    runtime_config_validator = _build_runtime_config_validator()
    config_service = _build_config_service(
        provider=config_provider,
        validator=runtime_config_validator,
    )
    remote_control_service = _build_remote_control_service()
    notification_queue_service = _build_notification_queue_service()
    cli_limits_service = _build_cli_limits_service(config)
    project_registry = _build_project_registry(config)
    session_thread_repository = _build_session_thread_repository(config)
    advanced_orchestrator_service = _build_advanced_orchestrator_service()
    artifact_intent_service = _build_artifact_intent_service()
    scheduler_service = _build_scheduler_service(
        config,
        scheduled_job_repository=scheduled_job_repository,
        system_event_bus=system_event_bus,
    )
    session_manager = SessionManager(config)
    session_mutation_service = SessionMutationService(session_manager)
    session_thread_manager = _build_session_thread_manager(
        config,
        repository=session_thread_repository,
        session_manager=session_manager,
    )

    mode_registry = ModeRegistry()
    mode_loader = ModeLoader()
    try:
        mode_loader.load_into(mode_registry)
    except Exception:
        logger.exception("mode loader init failed during bootstrap")
    mode_registry_service = ModeRegistryService(mode_registry)
    mode_launch_adapter_service = _build_mode_launch_adapter_service(
        mode_registry_service=mode_registry_service,
        bot_app_provider=bot_app_provider,
    )
    mode_pipeline = ModePipelineService(run_mode_pipeline_fn=run_mode_pipeline_fn)
    foundation_services = build_mode_foundation_services(config)
    run_operations_service = _build_run_operations_service(
        foundation_services,
        bot_app_provider=bot_app_provider,
    )
    ssh_service = SSHService()
    mode_dependencies = ModeDependencies(
        session_manager=session_manager,
        registry=mode_registry_service,
        pipeline=mode_pipeline,
        run_artifacts=foundation_services.run_artifacts,
        run_observability=foundation_services.run_observability,
        run_doctor=foundation_services.run_doctor,
        run_boundary_validation=foundation_services.run_boundary_validation,
        mode_run_lifecycle=foundation_services.mode_run_lifecycle,
        skill_runtime=foundation_services.skill_runtime,
        session_mutation_service=session_mutation_service,
        ssh=ssh_service,
    )

    mode_tasks = TaskService()

    async def _cancel_mode_tasks(session_id: str, mode_id: str, timeout_s: float = 0.2) -> int:
        return await mode_tasks.cancel_all(
            session_uid=str(session_id),
            mode_id=str(mode_id),
            timeout_s=float(timeout_s),
        )

    async def _cancel_session_tasks(session_id: str, timeout_s: float = 0.2) -> int:
        return await mode_tasks.cancel_session(
            session_uid=str(session_id),
            timeout_s=float(timeout_s),
        )

    session_control = SessionControlService(
        persist_sessions=session_manager._persist_sessions,
        cancel_mode_tasks=_cancel_mode_tasks,
        cancel_session_tasks=_cancel_session_tasks,
    )

    plugin_registry = tool_registry_factory(config)

    return ApplicationContainer(
        config=config,
        state_repository=state_repository,
        session_manager=session_manager,
        mode_registry=mode_registry,
        mode_loader=mode_loader,
        mode_registry_service=mode_registry_service,
        mode_pipeline=mode_pipeline,
        mode_dependencies=mode_dependencies,
        run_artifacts=foundation_services.run_artifacts,
        run_observability=foundation_services.run_observability,
        run_doctor=foundation_services.run_doctor,
        run_boundary_validation=foundation_services.run_boundary_validation,
        mode_run_lifecycle=foundation_services.mode_run_lifecycle,
        skill_runtime=foundation_services.skill_runtime,
        plugin_registry=plugin_registry,
        mode_tasks=mode_tasks,
        session_control=session_control,
        session_mutation_service=session_mutation_service,
        webhook_delivery_repository=webhook_delivery_repository,
        scheduled_job_repository=scheduled_job_repository,
        scheduled_jobs=scheduled_jobs,
        ssh_service=ssh_service,
        system_event_bus=system_event_bus,
        security=security,
        shared_http_ingress=shared_http_ingress,
        scheduler_service=scheduler_service,
        mode_launch_adapter_service=mode_launch_adapter_service,
        config_provider=config_provider,
        runtime_config_validator=runtime_config_validator,
        config_service=config_service,
        run_operations_service=run_operations_service,
        remote_control_service=remote_control_service,
        notification_queue_service=notification_queue_service,
        cli_limits_service=cli_limits_service,
        project_registry=project_registry,
        session_thread_repository=session_thread_repository,
        session_thread_manager=session_thread_manager,
        advanced_orchestrator_service=advanced_orchestrator_service,
        artifact_intent_service=artifact_intent_service,
    )
