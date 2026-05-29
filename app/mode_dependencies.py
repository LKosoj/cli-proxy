from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_doctor_service import RunDoctorService
from app.services.mode_run_lifecycle_service import ModeRunLifecycleService
from app.services.run_observability_service import RunObservabilityService
from app.services.skill_runtime_service import SkillRuntimeService as SkillRuntimeSelectorService
from app.services.task_bearing_cli_hook_service import register_task_bearing_cli_foundation_services
from modes.sdk.services.dialogs import DialogService
from modes.sdk.services.messaging import MessagingService
from modes.sdk.services.mode_registry import ModeRegistryService
from modes.sdk.services.runtime import AgentRuntimeService, DictStateService, DirsFlowService, ModePipelineService
from modes.sdk.services.session_control import SessionControlService
from modes.sdk.services.tasks import TaskService
from modes.sdk.services.tooling import ModeToolingService

if TYPE_CHECKING:
    from app.services.session_mutation_service import SessionMutationService
    from app.services.ssh_service import SSHService
    from session import SessionManager


MessagingFactoryFn = Callable[[Any], MessagingService]
RuntimeByCapabilityFn = Callable[[str], Any]


def _normalize_string_list(value: Any, *, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return fallback
    normalized = tuple(str(item).strip() for item in value if str(item).strip())
    return normalized or fallback


@dataclass(frozen=True)
class RunArtifactsService:
    enabled: bool
    retention_days: int
    artifact_store: RunArtifactStore

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    def retention_window_days(self) -> int:
        return int(self.retention_days)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.artifact_store, name)


@dataclass(frozen=True)
class SkillRuntimeService:
    discovery_mode: Literal["off", "suggest", "auto"]
    install_policy: Literal["manual", "admin_approve", "allowlisted_auto"]
    registry_paths: tuple[str, ...]
    allowlisted_sources: frozenset[str]
    selector_service: SkillRuntimeSelectorService

    def is_selection_enabled(self) -> bool:
        return self.discovery_mode != "off"

    def allows_auto_discovery(self) -> bool:
        return self.discovery_mode == "auto"

    def allows_source(self, source: str) -> bool:
        return str(source or "").strip() in self.allowlisted_sources

    def registry_path_list(self) -> list[str]:
        return list(self.registry_paths)

    @property
    def registry_service(self) -> Any:
        return self.selector_service.registry_service

    @property
    def policy_service(self) -> Any:
        return self.selector_service.policy_service

    def promote_to_global(self, *args: Any, **kwargs: Any) -> Any:
        return self.selector_service.promote_to_global(*args, **kwargs)

    def promote_run_skills(self, *args: Any, **kwargs: Any) -> Any:
        return self.selector_service.promote_run_skills(*args, **kwargs)

    def clear_cache(self) -> None:
        self.selector_service.clear_cache()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.selector_service, name)


@dataclass(frozen=True)
class ModeFoundationServices:
    run_artifacts: RunArtifactsService
    run_observability: RunObservabilityService
    run_doctor: RunDoctorService
    run_boundary_validation: RunBoundaryValidationService
    mode_run_lifecycle: ModeRunLifecycleService
    skill_runtime: SkillRuntimeService


def build_mode_foundation_services(config: Any) -> ModeFoundationServices:
    defaults = getattr(config, "defaults", None)
    artifact_store = RunArtifactStore(config)
    skill_runtime_selector = SkillRuntimeSelectorService(config)
    boundary_validation = RunBoundaryValidationService(
        enabled=bool(getattr(defaults, "run_boundary_validation_enabled", True)),
    )
    discovery_mode_raw = str(getattr(defaults, "skill_discovery_mode", "suggest") or "suggest").strip().lower()
    install_policy_raw = str(getattr(defaults, "skill_install_policy", "manual") or "manual").strip().lower()
    discovery_mode: Literal["off", "suggest", "auto"] = (
        discovery_mode_raw if discovery_mode_raw in {"off", "suggest", "auto"} else "suggest"
    )
    install_policy: Literal["manual", "admin_approve", "allowlisted_auto"] = (
        install_policy_raw
        if install_policy_raw in {"manual", "admin_approve", "allowlisted_auto"}
        else "manual"
    )
    registry_paths = _normalize_string_list(
        getattr(defaults, "skill_registry_paths", None),
        fallback=(".cli-proxy/skills",),
    )
    allowlisted_sources = frozenset(
        _normalize_string_list(
            getattr(defaults, "skill_allowlisted_sources", None),
            fallback=(
                "local:global-registry",
                "local:project-registry",
                "path:absolute",
                "registry:npx-skills",
                "ref:owner-repo-skill",
            ),
        )
    )
    observability = RunObservabilityService(
        enabled=bool(getattr(defaults, "run_metrics_enabled", True)),
        artifact_store=artifact_store,
    )
    register_task_bearing_cli_foundation_services(
        config,
        artifact_store=artifact_store,
        observability=observability,
        skill_runtime=skill_runtime_selector,
    )
    return ModeFoundationServices(
        run_artifacts=RunArtifactsService(
            enabled=bool(getattr(defaults, "run_artifacts_enabled", True)),
            retention_days=int(getattr(defaults, "run_artifacts_retention_days", 30) or 30),
            artifact_store=artifact_store,
        ),
        run_observability=observability,
        run_doctor=RunDoctorService(
            enabled=bool(getattr(defaults, "run_doctor_enabled", True)),
            artifact_store=artifact_store,
            boundary_validator=boundary_validation,
        ),
        run_boundary_validation=boundary_validation,
        mode_run_lifecycle=ModeRunLifecycleService(
            artifact_store=artifact_store,
            observability=observability,
            boundary_validator=boundary_validation,
        ),
        skill_runtime=SkillRuntimeService(
            discovery_mode=discovery_mode,
            install_policy=install_policy,
            registry_paths=registry_paths,
            allowlisted_sources=allowlisted_sources,
            selector_service=skill_runtime_selector,
        ),
    )


@dataclass(frozen=True)
class ModeDependencies:
    """Typed mode-level dependencies shared across plugins."""

    session_manager: SessionManager
    registry: ModeRegistryService
    pipeline: ModePipelineService
    run_artifacts: Optional[RunArtifactsService] = None
    run_observability: Optional[RunObservabilityService] = None
    run_doctor: Optional[RunDoctorService] = None
    run_boundary_validation: Optional[RunBoundaryValidationService] = None
    mode_run_lifecycle: Optional[ModeRunLifecycleService] = None
    skill_runtime: Optional[SkillRuntimeService] = None
    tasks: Optional[TaskService] = None
    dialogs: Optional[DialogService] = None
    session_control: Optional[SessionControlService] = None
    messaging_factory: Optional[MessagingFactoryFn] = None
    session_mutation_service: Optional[SessionMutationService] = None
    agent_runtime: Optional[AgentRuntimeService] = None
    dirs_flow: Optional[DirsFlowService] = None
    manager_pending: Optional[DictStateService] = None
    agent_pending: Optional[DictStateService] = None
    runtime_by_capability: Optional[RuntimeByCapabilityFn] = None
    tooling: Optional[ModeToolingService] = None
    ssh: Optional[SSHService] = None

    def with_overrides(
        self,
        *,
        run_artifacts: Optional[RunArtifactsService] = None,
        run_observability: Optional[RunObservabilityService] = None,
        run_doctor: Optional[RunDoctorService] = None,
        run_boundary_validation: Optional[RunBoundaryValidationService] = None,
        mode_run_lifecycle: Optional[ModeRunLifecycleService] = None,
        skill_runtime: Optional[SkillRuntimeService] = None,
        tasks: Optional[TaskService] = None,
        dialogs: Optional[DialogService] = None,
        session_control: Optional[SessionControlService] = None,
        messaging_factory: Optional[MessagingFactoryFn] = None,
        session_mutation_service: Optional[SessionMutationService] = None,
        agent_runtime: Optional[AgentRuntimeService] = None,
        dirs_flow: Optional[DirsFlowService] = None,
        manager_pending: Optional[DictStateService] = None,
        agent_pending: Optional[DictStateService] = None,
        runtime_by_capability: Optional[RuntimeByCapabilityFn] = None,
        tooling: Optional[ModeToolingService] = None,
        ssh: Optional[SSHService] = None,
    ) -> ModeDependencies:
        return replace(
            self,
            run_artifacts=run_artifacts if run_artifacts is not None else self.run_artifacts,
            run_observability=run_observability if run_observability is not None else self.run_observability,
            run_doctor=run_doctor if run_doctor is not None else self.run_doctor,
            run_boundary_validation=(
                run_boundary_validation if run_boundary_validation is not None else self.run_boundary_validation
            ),
            mode_run_lifecycle=(
                mode_run_lifecycle if mode_run_lifecycle is not None else self.mode_run_lifecycle
            ),
            skill_runtime=skill_runtime if skill_runtime is not None else self.skill_runtime,
            tasks=tasks if tasks is not None else self.tasks,
            dialogs=dialogs if dialogs is not None else self.dialogs,
            session_control=session_control if session_control is not None else self.session_control,
            messaging_factory=messaging_factory if messaging_factory is not None else self.messaging_factory,
            session_mutation_service=(
                session_mutation_service
                if session_mutation_service is not None
                else self.session_mutation_service
            ),
            agent_runtime=agent_runtime if agent_runtime is not None else self.agent_runtime,
            dirs_flow=dirs_flow if dirs_flow is not None else self.dirs_flow,
            manager_pending=manager_pending if manager_pending is not None else self.manager_pending,
            agent_pending=agent_pending if agent_pending is not None else self.agent_pending,
            runtime_by_capability=runtime_by_capability if runtime_by_capability is not None else self.runtime_by_capability,
            tooling=tooling if tooling is not None else self.tooling,
            ssh=ssh if ssh is not None else self.ssh,
        )
