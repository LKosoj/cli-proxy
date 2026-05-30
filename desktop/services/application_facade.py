from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from app.events import DesktopCommandEvent
from app.events.bus import SystemEventBus
from app.mode_dependencies import ModeDependencies, build_mode_foundation_services
from app.security import SecurityFacade
from app.services.access_policy_service import AccessPolicyService
from app.services.actor_identity import desktop_actor_id
from app.services.admin_config_service import (
    AdminConfigService,
    AdminConfigServiceError,
    AdminConfigSessionNotFoundError,
    AdminConfigSessionRequiredError,
)
from app.services.assistant_preview_service import (
    assistant_preview_enabled,
    build_assistant_preview_text,
    watch_session_assistant_preview,
)
from app.services.cli_limits_service import CliLimitsService
from app.services.config_service import AppRuntimeParams, ConfigService
from app.services.input_dispatch_service import InputDispatchService
from app.services.lint_evolution_runtime import make_session_hook as make_lint_evolution_hook
from app.services.mode_launch_adapter import ModeLaunchAdapterService
from app.services.menu_visibility_policy import (
    build_mode_menu_visibility,
    build_session_overview_visibility,
    call_mode_build_menu,
)
from app.services.run_artifact_store import is_terminal_status
from app.services.run_utils import clean_text as clean_run_listing_text, summarize_run_skill_log
from app.services.run_operations_policy import RunOperationsPolicy
from app.services.run_operations_service import RunOperationsService
from app.services.run_recovery_executor import build_recovery_dest, build_recovery_prompt
from app.services.scheduler_presentation_service import SchedulerPresentationService
from app.services.session_interrupt_service import SessionInterruptService
from app.services.session_mutation_service import SessionMutationService
from app.services.session_run_service import ModeScopedPreRunResetService
from app.services.task_bearing_cli_hook_service import get_task_bearing_cli_hook_service
from app.services.ui_state_models import ChatUiState
from app.services.advanced_orchestrator_service import (
    AdvancedOrchestratorService,
    DIRECT_CLI_MODE_ID,
)
from app.services.project_registry import ProjectOwnershipError, ProjectRegistry
from app.services.scheduler_service import (
    SchedulerNotFoundError,
    SchedulerOwnershipError,
    SchedulerService,
    SchedulerValidationError,
)
from app.services.scheduled_job_repository import ScheduledJobRepository
from app.services.session_files_service import FilesServiceError, SessionFilesService
from app.services.session_service import SessionService
from app.services.ssh_service import SSHService
from app.services.remote_control_service import (
    build_remote_control_audit_extra,
    build_remote_file_audit_extra,
)
from app.services.sandbox_service import AgentSandboxService
from app.services.path_normalization import normalize_optional_state_path
from app.services.task_service import TaskService
from agent import (
    approve_pending_command,
    configure_pending_commands_store,
    deny_pending_command,
    execute_shell_command,
    has_pending_command_waiter,
    set_approval_callback,
)
from desktop.services.theme_service import ThemeService
from desktop.services.desktop_identity_provider import DesktopIdentityProvider
from desktop.services.desktop_admin_facade import DesktopAdminFacade
from session import (
    consume_session_cli_switch_notice_text,
    session_scoped_key,
    session_runtime_uid,
    switch_session_active_cli_if_needed,
)
from sessions.queue_item import normalize_queue_item
from modes.registry import ModeLoader
from modes.sdk.services.mode_registry import ModeRegistryService
from modes.sdk.services.dialogs import DialogService
from modes.sdk.services.messaging import MessagingService
from modes.sdk.services.mode_callbacks import ModeCallbackRouterService
from modes.sdk.services.input_routing import ModeInputRoutingService
from modes.sdk.services.callback_data import build_session_overview_callback_data
from modes.sdk.services.tooling import ModeToolingService
from modes.sdk.services.runtime import (
    AgentRuntimeService,
    DirsFlowService,
    DictStateService,
    ModePipelineService,
)
from modes.sdk.services.session_control import SessionControlService
from modes.sdk.runtime.openai_client import chat_completion
from modes.sdk import CallbackModel, decode_mode_dirs
from modes.sdk.runtime.events import EventSeverity, EventType, OrchestratorEvent
from modes.sdk.runtime.reactions import ReactionAction, ReactionEngine, ReactionRule
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_pending_input,
    is_orchestrator_enabled,
    reset_session_runtime_state,
    set_orchestrator_enabled,
    set_orchestrator_last_mode_id,
    set_orchestrator_last_mode_output,
    set_orchestrator_pending_input,
)
from utils.paths import cli_proxy_artifact_path


@dataclass
class AppNotification:
    event: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedAttachments:
    image_paths: List[str] = field(default_factory=list)
    meta: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DesktopModeLaunchPolicy:
    actor_chat_id: Optional[int]
    is_mode_allowed: bool
    reason: str = ""


class DesktopRuntimePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_uid: Optional[str] = None

    @model_validator(mode="after")
    def _require_session_uid(self) -> "DesktopRuntimePayload":
        if str(self.session_uid or "").strip():
            return self
        raise ValueError("desktop runtime payload is invalid: session_uid is required")


class ApplicationFacade:
    """Координатор инициализации и шина уведомлений/прогресса."""

    def __init__(
        self,
        *,
        config_service: ConfigService,
        session_service: SessionService,
        task_service: TaskService,
        git_service: Optional[Any] = None,
        theme_service: Optional[ThemeService] = None,
        mode_registry_service: Optional[ModeRegistryService] = None,
        ui_state_service: Optional[Any] = None,  # Avoid circular import if any
        logger: Optional[logging.Logger] = None,
        advanced_orchestrator_service: Optional[AdvancedOrchestratorService] = None,
    ):
        self.config_service = config_service
        self.session_service = session_service
        self._session_mutations = SessionMutationService(getattr(session_service, "_manager", None))
        self.task_service = task_service
        self.git_service = git_service
        self.theme_service = theme_service or ThemeService(logger=logger)
        self.mode_registry_service = mode_registry_service
        self.ui_state_service = ui_state_service
        self.ssh_service = SSHService()
        from app.services.remote_control_service import RemoteControlService
        self.remote_control_service = RemoteControlService()
        if self.git_service is not None:
            if hasattr(self.git_service, "_ssh_service"):
                self.git_service._ssh_service = self.ssh_service
            if hasattr(self.git_service, "_remote_control_service"):
                self.git_service._remote_control_service = self.remote_control_service
        self.logger = logger or logging.getLogger(__name__)
        self.advanced_orchestrator_service = advanced_orchestrator_service or AdvancedOrchestratorService()
        self.cli_limits_service = CliLimitsService()
        self.orchestrator_chat_completion = chat_completion
        self._subscribers: List[Callable[[AppNotification], Any]] = []
        self.runtime_params: Optional[AppRuntimeParams] = None
        self.config: Optional[Any] = None
        self._mode_runtime_registry: Dict[str, Any] = {}
        self._modes_initialized: bool = False
        self._mode_dialogs: Optional[DialogService] = None
        self._mode_callback_router: Optional[ModeCallbackRouterService] = None
        self._dirs_mode_token_by_chat: Dict[int, str] = {}
        self._mode_task_ids: Dict[tuple[str, str], List[str]] = {}
        self._mode_task_names: Dict[str, str] = {}
        self._manager_resume_pending: Dict[str, Any] = {}
        self._desktop_mode_dependencies_instance: Optional[ModeDependencies] = None
        self._desktop_run_operations_service: Optional[RunOperationsService] = None
        self._desktop_run_operations_policy = RunOperationsPolicy()
        self._desktop_interrupt_service: Optional[SessionInterruptService] = None
        self._desktop_bot_app_instance: Optional[Any] = None
        self._desktop_sandbox_service: Optional[AgentSandboxService] = None
        self._desktop_sandbox_workdir: str = ""
        self._desktop_state_path: str = ""
        self._desktop_project_registry: Optional[ProjectRegistry] = None
        self._desktop_system_event_bus: Optional[SystemEventBus] = None
        self._desktop_scheduled_job_repository: Optional[ScheduledJobRepository] = None
        self._desktop_scheduler_service: Optional[SchedulerService] = None
        self._desktop_scheduler_started_instance: Optional[SchedulerService] = None
        self._desktop_mode_launch_adapter: Optional[ModeLaunchAdapterService] = None
        self._desktop_identity_provider: Optional[DesktopIdentityProvider] = None
        self._desktop_message_id: int = 0
        self._shutdown_in_progress: bool = False
        self._queue_kick_tasks: Dict[str, asyncio.Task] = {}
        self._reaction_engine = ReactionEngine(logger=self.logger)
        self._reaction_engine.register_action("reroute", self._handle_reaction_reroute)
        self._reaction_rules: List[ReactionRule] = [
            ReactionRule(
                rule_id="desktop.v2.retry_on_failed_step",
                event_types=[EventType.STEP_FAILED],
                min_severity=EventSeverity.ERROR,
                actions=[ReactionAction(action_type="retry_step", params={"max_retries": 3})],
            ),
            ReactionRule(
                rule_id="desktop.v2.needs_input_on_failed_step",
                event_types=[EventType.STEP_FAILED],
                min_severity=EventSeverity.ERROR,
                payload_equals={"needs_input": True},
                actions=[ReactionAction(action_type="ask_user", params={"question": "Нужно уточнение для продолжения."})],
            ),
            ReactionRule(
                rule_id="desktop.v2.reroute_on_failed_step",
                event_types=[EventType.STEP_FAILED],
                min_severity=EventSeverity.ERROR,
                payload_equals={"reroute": True},
                actions=[ReactionAction(action_type="reroute")],
            ),
        ]
        self._mode_pre_run_reset = ModeScopedPreRunResetService(logger=self.logger)
        self.started = False

    async def _handle_reaction_reroute(
        self,
        event: OrchestratorEvent,
        _action: ReactionAction,
        _ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "action": "reroute",
            "status": "queued",
            "step_id": str(event.step_id or ""),
            "target_mode": str((event.payload or {}).get("target_mode") or ""),
        }

    async def describe_active_cli_limits(self) -> str:
        sessions = list(self.session_service.list_desktop_sessions() or [])
        config = getattr(self, "config", None)
        tools = getattr(config, "tools", None)
        available_clis = None
        if isinstance(tools, dict):
            available_clis = [
                name
                for name, tool in tools.items()
                if str(name or "").strip().lower() in self.cli_limits_service.SUPPORTED_CLI_NAMES
                and bool(getattr(tool, "enabled", True))
            ]
        return await self.cli_limits_service.describe_for_sessions(sessions, available_clis=available_clis)

    async def _bridge_v2_event(
        self,
        *,
        session_uid: str,
        step_id: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        event = OrchestratorEvent(
            event_type=EventType.STEP_FAILED,
            severity=EventSeverity.ERROR,
            session_id=str(session_uid or ""),
            step_id=str(step_id or ""),
            message=str(message or ""),
            payload=dict(payload or {}),
        )
        results = await self._reaction_engine.execute(
            event,
            self._reaction_rules,
            ctx={"session_uid": str(session_uid or ""), "step_id": str(step_id or "")},
        )
        for item in results:
            action = str(item.get("action") or "")
            status = str(item.get("status") or "")
            if action == "retry_step":
                self.notify("ui:v2_event", session_uid=str(session_uid or ""), event_type="retry", status=status, raw=item)
            elif action == "reroute":
                self.notify("ui:v2_event", session_uid=str(session_uid or ""), event_type="reroute", status=status, raw=item)
            elif action == "ask_user":
                self.notify("ui:v2_event", session_uid=str(session_uid or ""), event_type="needs_input", status=status, raw=item)
        return results

    async def _bridge_mode_result_v2(self, *, session_uid: str, result: Any, fallback_step_id: str) -> None:
        if result is None:
            return
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            return

        validation_report = data.get("validation_report")
        if isinstance(validation_report, dict):
            report_status = str(validation_report.get("status") or "").strip().lower()
            if report_status == "not_run":
                self.notify(
                    "ui:validation_status",
                    session_uid=str(session_uid or ""),
                    status="not_run",
                    report=validation_report,
                )
            else:
                for step in list(validation_report.get("steps") or []):
                    if isinstance(step, dict) and str(step.get("status") or "").strip().lower() == "not_run":
                        self.notify(
                            "ui:validation_status",
                            session_uid=str(session_uid or ""),
                            status="not_run",
                            report=validation_report,
                        )
                        break

        v2_payload = data.get("v2_event")
        if isinstance(v2_payload, dict):
            step_id = str(v2_payload.get("step_id") or fallback_step_id or "")
            message = str(v2_payload.get("message") or getattr(result, "error", "") or "")
            payload = v2_payload.get("payload") if isinstance(v2_payload.get("payload"), dict) else {}
            await self._bridge_v2_event(
                session_uid=str(session_uid or ""),
                step_id=step_id,
                message=message,
                payload=payload,
            )

    def subscribe(self, callback: Callable[[AppNotification], Any]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _unsubscribe

    def list_modes(self) -> List[str]:
        """Возвращает список доступных режимов."""
        if self.mode_registry_service and self.mode_registry_service.registry:
            items = self.mode_registry_service.list_modes()
            return [mode_id for mode_id, _label in items]
        loader = ModeLoader()
        discovered = loader.discover()
        return [name for name, _ in discovered]

    @staticmethod
    def _normalize_session_mode_args(session_uid: Any, mode_id: Any) -> tuple[str, Optional[str]]:
        return str(session_uid or "").strip(), mode_id

    def set_session_mode(self, session_uid: Any, mode_id: Any) -> bool:
        """
        Устанавливает активный mode для сессии.
        mode_id=None или пустая строка выключает режим.
        """
        session_uid, mode_id = self._normalize_session_mode_args(session_uid, mode_id)
        mid = str(mode_id or "").strip()
        if not mid:
            ok = self.session_service.clear_mode_by_uid(session_uid)
        else:
            ok = self.session_service.set_mode_by_uid(session_uid, mid)

        if ok:
            self.notify("ui:mode_changed", session_uid=session_uid, mode_id=mid or None)
        return ok

    async def set_session_mode_via_callback(self, session_uid: Any, mode_id: Any) -> bool:
        """
        Переключает режим через тот же callback-маршрут, что и в Telegram:
        ma:<mode>:enable / disable.
        """
        session_uid, mode_id = self._normalize_session_mode_args(session_uid, mode_id)
        self._ensure_modes_ready()
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return False

        target = str(mode_id or "").strip()
        current = str(get_active_mode(session, "") or "").strip()
        if target == current:
            return True

        if not target:
            if not current:
                return True
            dispatched = await self.handle_mode_callback(session_uid, data=f"ma:{current}:disable")
            if not dispatched:
                return False
            changed = not str(get_active_mode(session, "") or "").strip()
            if changed:
                self.notify("ui:mode_changed", session_uid=session_uid, mode_id=None)
            return bool(changed)

        dispatched = await self.handle_mode_callback(session_uid, data=f"ma:{target}:enable")
        if not dispatched:
            return False
        changed = str(get_active_mode(session, "") or "").strip() == target
        if changed:
            self.notify("ui:mode_changed", session_uid=session_uid, mode_id=target)
        return bool(changed)

    def rename_session(self, session_uid: str, new_name: str) -> bool:
        session = self.session_service.get_session_by_uid(session_uid)
        if session:
            session.name = str(new_name).strip()
            try:
                self.session_service._manager._persist_sessions()
            except Exception:
                self.logger.exception("desktop rename_session failed to persist session_uid=%s", session_uid)
            self.notify("ui:session_updated", session_uid=session_uid)
            return True
        return False

    def reset_session(self, session_uid: str) -> bool:
        session = self.session_service.get_session_by_uid(session_uid)
        if session:
            access_policy = getattr(self._desktop_bot_app(), "access_policy_service", None)
            default_mode_id = None
            if access_policy is not None and hasattr(access_policy, "default_mode_id_for_chat"):
                default_mode_id = access_policy.default_mode_id_for_chat(getattr(session, "chat_id", None))
            reset_session_runtime_state(session, default_mode_id=default_mode_id)
            try:
                self.session_service._manager._persist_sessions()
            except Exception:
                self.logger.exception("desktop reset_session failed to persist session_uid=%s", session_uid)
            self.notify("ui:session_updated", session_uid=session_uid)
            self.notify("ui:message", session_id=session_uid, role="agent", text="Сессия сброшена.")
            return True
        return False

    async def update_session_setting(self, session_uid: str, key: str, value: Any) -> bool:
        """Update a specific session setting and persist changes."""
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return False

        from sessions.session_state_access import (
            set_active_mode,
            set_orchestrator_enabled,
            set_ssh_remote_enabled,
        )

        # REQ-7: Logs and Config are always local
        if key in ("logs_remote_mode", "config_remote_mode") and bool(value):
            self.logger.warning("Rejected %s=True: logs and config are always local", key)
            return False

        changed = False
        if key == "name":
            session.name = str(value).strip() or None
            changed = True
        elif key == "active_cli":
            idle_check = self.remote_control_service.validate_idle(session)
            if not idle_check.ok:
                return False
            if self.session_service.set_active_cli_by_uid(session_uid, str(value)):
                changed = True
        elif key == "active_mode":
            set_active_mode(session, value)
            changed = True
        elif key == "ssh_remote_enabled":
            idle_check = self.remote_control_service.validate_idle(session)
            if not idle_check.ok:
                return False
            set_ssh_remote_enabled(session, bool(value))
            changed = True
            # Normalize dependent toggles
            rc_svc = getattr(self, "remote_control_service", None)
            if rc_svc is not None:
                from app.services.ssh_config_loader import load_ssh_config
                workdir = str(getattr(session, "workdir", "") or "").strip()
                hosts = load_ssh_config(workdir) if workdir else {}
                rc_svc.normalize_setting_change(session, "ssh_remote_enabled", bool(value), hosts, workdir)
        elif key == "remote_control_enabled":
            idle_check = self.remote_control_service.validate_idle(session)
            if not idle_check.ok:
                return False
            rc_svc = getattr(self, "remote_control_service", None)
            if rc_svc is not None:
                from app.services.ssh_config_loader import load_ssh_config
                workdir = str(getattr(session, "workdir", "") or "").strip()
                hosts = load_ssh_config(workdir) if workdir else {}
                rc_svc.normalize_setting_change(session, key, value, hosts, workdir)
            changed = True
        elif key == "remote_control_host_alias":
            idle_check = self.remote_control_service.validate_idle(session)
            if not idle_check.ok:
                return False
            rc_svc = getattr(self, "remote_control_service", None)
            if rc_svc is not None:
                from app.services.ssh_config_loader import load_ssh_config
                workdir = str(getattr(session, "workdir", "") or "").strip()
                hosts = load_ssh_config(workdir) if workdir else {}
                rc_svc.normalize_setting_change(session, key, value, hosts, workdir)
            changed = True
        elif key == "orchestrator_enabled":
            set_orchestrator_enabled(session, bool(value))
            changed = True

        if changed:
            try:
                self.session_service._manager._persist_sessions()
            except Exception:
                self.logger.exception("desktop update_session_setting failed to persist session_uid=%s", session_uid)
            self.notify("ui:session_updated", session_uid=session_uid)
            self.notify("ui:session_settings_changed", session_uid=session_uid, key=key, value=value)
            return True
        return False

    def get_remote_control_settings(self, session_uid: str) -> Optional[Dict[str, Any]]:
        """Return remote control settings for a session (Desktop parity with MiniApp GET)."""
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return None

        from app.services.ssh_config_loader import load_ssh_config
        from sessions.session_state_access import (
            get_remote_control_host_alias,
            is_remote_control_enabled,
        )

        workdir = str(getattr(session, "workdir", "") or "").strip()
        all_hosts = load_ssh_config(workdir) if workdir else {}

        rc_enabled = is_remote_control_enabled(session)
        rc_alias = get_remote_control_host_alias(session)

        # Desktop has no per-user ACL filtering — all hosts visible
        rc_hosts = {}
        for alias, cfg in all_hosts.items():
            rc_hosts[alias] = {
                "host": cfg.host,
                "user": cfg.user,
                "remote_project_root": cfg.remote_project_root,
                "description": cfg.description,
            }

        effective = self.remote_control_service.compute_effective_state(session, all_hosts)

        return {
            "remote_control_enabled": rc_enabled,
            "remote_control_host_alias": rc_alias,
            "remote_control_hosts": rc_hosts,
            "available": {
                "remote_control_hosts": rc_hosts,
            },
            "effective": {
                "execution_target": effective.execution_target.value,
                "host_alias": effective.host_alias,
                "remote_project_root": effective.remote_project_root,
                "git_available": effective.git_available,
            },
        }

    async def update_remote_control(
        self,
        session_uid: str,
        *,
        enabled: Optional[bool] = None,
        host_alias: Optional[str] = ...,  # type: ignore[assignment]
    ) -> Dict[str, Any]:
        """Validate, normalize, and optionally preflight a remote control settings change.

        Returns a dict with ``ok``, ``changed``, and optional ``preflight``.
        """
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return {"ok": False, "error": "session not found", "changed": []}

        from app.services.remote_control_service import TransitionRequest
        from app.services.ssh_config_loader import load_ssh_config
        from sessions.session_state_access import (
            get_remote_control_host_alias,
            is_remote_control_enabled,
        )

        workdir = str(getattr(session, "workdir", "") or "").strip()
        hosts = load_ssh_config(workdir) if workdir else {}
        changed: list[str] = []
        rc_svc = self.remote_control_service
        before_enabled = is_remote_control_enabled(session)
        before_alias = get_remote_control_host_alias(session)
        idle_check = rc_svc.validate_idle(session)
        if not idle_check.ok:
            return {"ok": False, "error": idle_check.error, "changed": changed}

        # Apply alias change first
        if host_alias is not ...:
            rc_svc.normalize_setting_change(session, "remote_control_host_alias", host_alias, hosts, workdir)
            changed.append("remote_control_host_alias")

        # Then enabled change with validation + preflight
        if enabled is not None:
            alias = get_remote_control_host_alias(session)
            tr = TransitionRequest(enable=enabled, host_alias=alias)
            vr = rc_svc.validate_transition(session, tr, hosts)
            if not vr.ok:
                return {"ok": False, "error": vr.error, "changed": changed}

            if enabled and alias and alias in hosts:
                pf = await rc_svc.run_preflight(self.ssh_service, workdir, alias, hosts[alias])
                if not pf.ok:
                    result = {
                        "ok": False,
                        "changed": changed,
                        "preflight": {
                            "ok": False,
                            "host_alias": pf.host_alias,
                            "remote_project_root": pf.remote_project_root,
                            "checked_at": pf.checked_at,
                            "error": pf.error,
                        },
                    }
                    self._log_remote_control_audit(
                        session=session,
                        action="remote_control_preflight_failed",
                        host_alias=alias,
                        host_cfg=hosts.get(alias),
                        result="error",
                        reason=str(pf.error or ""),
                    )
                    return result

            rc_svc.normalize_setting_change(session, "remote_control_enabled", enabled, hosts, workdir)
            changed.append("remote_control_enabled")

        if changed:
            after_enabled = is_remote_control_enabled(session)
            after_alias = get_remote_control_host_alias(session)
            after_host_cfg = hosts.get(after_alias) if after_alias else None
            self._persist_and_notify(session_uid)
            if before_alias != after_alias:
                self._log_remote_control_audit(
                    session=session,
                    action="remote_control_host_changed",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                    reason=f"{before_alias or ''}->{after_alias or ''}",
                )
            if not before_enabled and after_enabled:
                self._log_remote_control_audit(
                    session=session,
                    action="remote_control_enabled",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                )
            if before_enabled and not after_enabled:
                self._log_remote_control_audit(
                    session=session,
                    action="remote_control_disabled",
                    host_alias=after_alias,
                    host_cfg=after_host_cfg,
                    result="ok",
                )
        return {"ok": True, "changed": changed}

    def _persist_and_notify(self, session_uid: str) -> None:
        try:
            self.session_service._manager._persist_sessions()
        except Exception:
            self.logger.exception("desktop persist failed session_uid=%s", session_uid)
        self.notify("ui:session_updated", session_uid=session_uid)

    def _log_remote_control_audit(
        self,
        *,
        session: Any,
        action: str,
        host_alias: Optional[str],
        host_cfg: Any,
        result: str,
        reason: str = "",
    ) -> None:
        self.logger.info(
            action,
            extra=build_remote_control_audit_extra(
                session=session,
                actor=desktop_actor_id(),
                surface="desktop",
                action=action,
                host_alias=host_alias,
                host_cfg=host_cfg,
                result=result,
                reason=reason,
            ),
        )

    async def recheck_remote_control(self, session_uid: str) -> Dict[str, Any]:
        """Re-run preflight for the current remote control host."""
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return {"ok": False, "error": "session not found"}

        from app.services.ssh_config_loader import load_ssh_config
        from sessions.session_state_access import get_remote_control_host_alias

        alias = get_remote_control_host_alias(session)
        if not alias:
            return {"ok": False, "error": "no remote_control_host_alias configured"}

        workdir = str(getattr(session, "workdir", "") or "").strip()
        hosts = load_ssh_config(workdir) if workdir else {}
        host_cfg = hosts.get(alias)
        if host_cfg is None:
            return {"ok": False, "error": f"host '{alias}' not found"}

        rc_svc = self.remote_control_service
        rc_svc.invalidate_preflight(workdir, alias)
        pf = await rc_svc.run_preflight(self.ssh_service, workdir, alias, host_cfg)
        if not pf.ok:
            self._log_remote_control_audit(
                session=session,
                action="remote_control_preflight_failed",
                host_alias=alias,
                host_cfg=host_cfg,
                result="error",
                reason=str(pf.error or ""),
            )
        return {
            "ok": True,
            "preflight": {
                "ok": pf.ok,
                "host_alias": pf.host_alias,
                "remote_project_root": pf.remote_project_root,
                "checked_at": pf.checked_at,
                "error": pf.error,
            },
        }

    # External API: вызывается desktop-редактором (parity-контракт, покрыт tests/test_desktop_files_editor_parity.py)
    def force_save_file(
        self, session_uid: str, user_id: int, path: str, content: str,
    ) -> Dict[str, Any]:
        """Force-save a file (skip revision check). Logs audit event."""
        bot_app = getattr(self, "_bot_app", None)
        if bot_app is None:
            return {"ok": False, "error": "bot_app not available"}
        svc = SessionFilesService(app=bot_app)
        try:
            result = svc.write(user_id, session_uid, path, content, None, force=True)
        except FilesServiceError as exc:
            self.logger.warning(
                "desktop force_save_file rejected session_uid=%s path=%s error=%s",
                session_uid,
                path,
                exc,
            )
            return {"ok": False, "error": str(exc)}
        if result.get("forced"):
            ctx = svc.execution_context(session_uid)
            self.logger.info(
                "remote_file_force_saved",
                extra=build_remote_file_audit_extra(
                    actor=user_id,
                    session_uid=session_uid,
                    surface="desktop",
                    action="remote_file_force_saved",
                    path=path,
                    result="ok",
                    provider=str(ctx.get("execution_target") or "local"),
                    host=str(ctx.get("host_alias") or ""),
                    remote_project_root=str(ctx.get("remote_project_root") or ""),
                    old_revision=result.get("old_revision"),
                    new_revision=result.get("revision"),
                ),
            )
        return result

    async def _resolve_files_result(self, result: Any) -> Dict[str, Any]:
        if inspect.isawaitable(result):
            result = await result
        return dict(result or {})

    def _desktop_files_service(self) -> SessionFilesService:
        return SessionFilesService(app=self._desktop_bot_app())

    @staticmethod
    def _desktop_files_actor_id() -> int:
        return 0

    async def files_execution_context(self, session_uid: str) -> Dict[str, Any]:
        svc = self._desktop_files_service()
        return await self._resolve_files_result(
            svc.execution_context(str(session_uid), user_id=self._desktop_files_actor_id())
        )

    async def files_tree(self, session_uid: str, path: str = ".") -> Dict[str, Any]:
        svc = self._desktop_files_service()
        return await self._resolve_files_result(
            svc.tree(self._desktop_files_actor_id(), str(session_uid), str(path or "."))
        )

    async def files_read(self, session_uid: str, path: str) -> Dict[str, Any]:
        svc = self._desktop_files_service()
        return await self._resolve_files_result(
            svc.read(self._desktop_files_actor_id(), str(session_uid), str(path or ""))
        )

    async def files_write(
        self,
        session_uid: str,
        path: str,
        content: str,
        expected_revision: Optional[str] = None,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        svc = self._desktop_files_service()
        ctx = svc.execution_context(str(session_uid), user_id=self._desktop_files_actor_id())
        result = svc.write(
            self._desktop_files_actor_id(),
            str(session_uid),
            str(path or ""),
            str(content or ""),
            expected_revision,
            force=force,
        )
        payload = await self._resolve_files_result(result)
        if force and payload.get("forced"):
            self.logger.info(
                "remote_file_force_saved",
                extra=build_remote_file_audit_extra(
                    actor=desktop_actor_id(),
                    session_uid=str(session_uid),
                    surface="desktop",
                    action="remote_file_force_saved",
                    path=str(path or ""),
                    result="ok",
                    provider=str(ctx.get("execution_target") or "local"),
                    host=str(ctx.get("host_alias") or ""),
                    remote_project_root=str(ctx.get("remote_project_root") or ""),
                    old_revision=payload.get("old_revision"),
                    new_revision=payload.get("revision"),
                ),
            )
        return payload

    async def files_create(self, session_uid: str, path: str, kind: str) -> Dict[str, Any]:
        svc = self._desktop_files_service()
        return await self._resolve_files_result(
            svc.create(
                self._desktop_files_actor_id(),
                str(session_uid),
                str(path or ""),
                str(kind or ""),
            )
        )

    async def files_delete(self, session_uid: str, path: str) -> Dict[str, Any]:
        svc = self._desktop_files_service()
        return await self._resolve_files_result(
            svc.delete(self._desktop_files_actor_id(), str(session_uid), str(path or ""))
        )

    async def test_ssh_connection(self, workdir: str, alias: str) -> Any:
        """Verify SSH connectivity to a host."""
        return await self.ssh_service.test_connection(workdir, alias)

    async def generate_ssh_key(self, workdir: str, alias: str) -> Any:
        """Generate a new SSH key pair for a host."""
        return await self.ssh_service.generate_key(workdir, alias)

    def set_active_cli(self, session_uid: str, cli_name: str) -> bool:
        session = self.session_service.get_session_by_uid(session_uid)
        if session is None:
            return False
        # Capture previous CLI info before switching.
        previous_cli = str(getattr(getattr(session, "cli", None), "active_cli", "") or "").strip()
        previous_token = (getattr(getattr(session, "cli", None), "resume_tokens", None) or {}).get(previous_cli)
        if not self.session_service.set_active_cli_by_uid(session_uid, str(cli_name)):
            return False
        try:
            self.session_service._manager._persist_sessions()
        except Exception:
            self.logger.exception("desktop set_active_cli failed to persist session_uid=%s", session_uid)
        self.notify("ui:session_updated", session_uid=session_uid)
        # Offer session transfer if source had a session.
        transfer_available = bool(
            previous_cli
            and previous_cli != cli_name
            and previous_token
            and str(previous_token).strip()
        )
        if transfer_available:
            self.notify(
                "ui:session_transfer_offer",
                session_uid=session_uid,
                source_cli=previous_cli,
                target_cli=cli_name,
            )
        return True

    def confirm_session_transfer(self, session_uid: str, source_cli: str) -> bool:
        """Called by Desktop UI when user confirms session transfer.

        Reads the source CLI session and writes it into the currently active (target) CLI,
        then sets the new resume_token so the target CLI continues the conversation.
        """
        session = self.session_service.get_session_by_uid(session_uid)
        if session is None:
            return False
        try:
            from app.services.session_transfer.service import extract_session, write_target_session

            target_cli = str(getattr(getattr(session, "cli", None), "active_cli", "") or "").strip()
            source_token = (getattr(getattr(session, "cli", None), "resume_tokens", None) or {}).get(source_cli)
            workspace = getattr(session, "workdir", "") or ""
            if not (source_token and workspace and target_cli):
                return False

            canonical = extract_session(source_cli, str(source_token), workspace)
            if not canonical or not canonical.messages:
                return False

            new_token = write_target_session(canonical, target_cli, workspace)
            if not new_token:
                return False

            session.resume_token = new_token
            try:
                self.session_service._manager._persist_sessions()
            except Exception:
                self.logger.exception(
                    "desktop confirm_session_transfer: persist failed session_uid=%s", session_uid,
                )
            self.notify("ui:session_updated", session_uid=session_uid)
            return True
        except Exception:
            self.logger.exception("desktop confirm_session_transfer failed session_uid=%s", session_uid)
        return False

    def get_metrics_snapshot(self) -> str:
        # If BotApp has metrics, use it.
        bot_app = self._desktop_bot_app()
        if hasattr(bot_app, "metrics"):
            return str(bot_app.metrics.snapshot())

        # Fallback: manually collect some metrics
        sessions = self.session_service._manager.sessions_for_chat(1)
        total_tokens = sum(getattr(s, "tokens_used", 0) or 0 for s in sessions.values())
        return f"Total sessions: {len(sessions)}\nTotal tokens used: {total_tokens}"

    def get_session_mode(self, session_uid: Any) -> Optional[str]:
        session_uid = str(session_uid or "").strip()
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return None
        mode_id = get_active_mode(session, None)
        mode_id = str(mode_id).strip() if mode_id is not None else None
        return mode_id or None

    def get_admin_status_payload(self, session_uid: str) -> Optional[Dict[str, Any]]:
        self._ensure_modes_ready()
        session = self.session_service.get_session_by_uid(session_uid)
        if not session or not self.mode_registry_service:
            return None
        try:
            plugin = self.mode_registry_service.get("admin")
        except Exception:
            self.logger.exception(
                "desktop get_admin_status_payload failed to resolve plugin session_uid=%s",
                session_uid,
            )
            return None
        if plugin is None:
            return None
        builder = getattr(plugin, "build_status_payload", None)
        if not callable(builder):
            return None
        try:
            payload = builder(
                bot_app=self._desktop_bot_app(),
                session=session,
                chat_id=self._desktop_admin_actor_chat_id(session),
            )
        except Exception:
            self.logger.exception(
                "desktop get_admin_status_payload failed session_uid=%s",
                session_uid,
            )
            return None
        if isinstance(payload, dict):
            return dict(payload)
        return None

    async def run_admin_session_action(
        self,
        session_uid: str,
        *,
        action: str,
        user_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        self._ensure_modes_ready()
        session = self.session_service.get_session_by_uid(session_uid)
        if not session or not self.mode_registry_service:
            return False
        try:
            plugin = self.mode_registry_service.get("admin")
        except Exception:
            self.logger.exception(
                "desktop run_admin_session_action failed to resolve plugin session_uid=%s action=%s",
                session_uid,
                str(action),
            )
            return False
        if plugin is None or not hasattr(plugin, "handle_callback"):
            return False
        actor_chat_id = self._desktop_admin_actor_chat_id(session)
        try:
            result = await plugin.handle_callback(
                CallbackModel(
                    action=str(action or "").strip(),
                    chat_id=actor_chat_id,
                    user_id=int(user_id) if isinstance(user_id, int) else actor_chat_id,
                    payload=dict(payload or {}),
                    raw={},
                ),
                {
                    "bot_app": self._desktop_bot_app(),
                    "session": session,
                    "chat_id": actor_chat_id,
                    "context": None,
                    "query": None,
                    "mode_id": "admin",
                },
            )
        except Exception:
            self.logger.exception(
                "desktop run_admin_session_action failed session_uid=%s action=%s",
                session_uid,
                str(action),
            )
            return False
        self.notify("ui:session_updated", session_uid=session_uid)
        return bool(getattr(result, "success", True))

    @staticmethod
    def _desktop_admin_actor_chat_id(session: Any) -> int:
        try:
            value = int(getattr(session, "chat_id", 0) or 0)
        except Exception:
            return 0
        return value if value > 0 else 0

    def _admin_config_service(self) -> Any:
        service = getattr(self, "admin_config_service", None)
        if service is not None:
            return service
        return AdminConfigService(self)

    @staticmethod
    def _desktop_admin_config_error(exc: AdminConfigServiceError) -> str:
        if isinstance(exc, (AdminConfigSessionNotFoundError, AdminConfigSessionRequiredError)):
            return "session_not_found"
        text = str(exc)
        if text == "session workdir is not set":
            return "session_workdir_empty"
        if text.startswith("invalid YAML: "):
            return "invalid_yaml: " + text[len("invalid YAML: "):]
        if text == "admin config must be a YAML mapping":
            return "config_must_be_mapping"
        if text == "admin config is missing `admin` mapping":
            return "admin config is missing admin mapping"
        if text == "each server must be an object":
            return "server must be a mapping"
        if text.startswith("target must be local|ssh: "):
            return "invalid target for " + text[len("target must be local|ssh: "):]
        if text.startswith("timeout_sec must be numeric: "):
            return "timeout_sec must be numeric for " + text[len("timeout_sec must be numeric: "):]
        if text.startswith("timeout_sec must be > 0: "):
            return "timeout_sec must be > 0 for " + text[len("timeout_sec must be > 0: "):]
        return text

    def get_admin_config_yaml(self, session_uid: str) -> Optional[Dict[str, Any]]:
        try:
            result = ApplicationFacade._admin_config_service(self).get_yaml(session_uid)
        except (AdminConfigSessionNotFoundError, AdminConfigSessionRequiredError):
            return None
        except AdminConfigServiceError as exc:
            if str(exc) == "session workdir is not set":
                return None
            self.logger.exception(
                "desktop get_admin_config_yaml failed session_uid=%s",
                session_uid,
            )
            return None
        return {
            "config_path": str(result.get("config_path") or ""),
            "yaml": str(result.get("yaml") or ""),
        }

    def save_admin_config_yaml(
        self,
        session_uid: str,
        *,
        yaml_text: str,
    ) -> Dict[str, Any]:
        return DesktopAdminFacade.save_admin_config_yaml(self, session_uid, yaml_text=yaml_text)

    def get_admin_hosts(self, session_uid: str) -> List[Dict[str, Any]]:
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return []
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return []
        from app.services.ssh_config_loader import load_ssh_config
        hosts = load_ssh_config(workdir)
        items: List[Dict[str, Any]] = [{
            "alias": "local",
            "target": "local",
            "host": "",
            "user": "",
            "port": 0,
        }]
        for alias, cfg in hosts.items():
            items.append({
                "alias": str(alias),
                "target": "ssh",
                "host": str(cfg.host or ""),
                "user": str(cfg.user or ""),
                "port": int(cfg.port or 22),
            })
        return items

    def get_admin_monitor_servers(self, session_uid: str) -> Dict[str, Any]:
        try:
            result = ApplicationFacade._admin_config_service(self).get_monitor_servers(session_uid)
        except AdminConfigServiceError as exc:
            return {"ok": False, "error": ApplicationFacade._desktop_admin_config_error(exc)}
        return {
            "ok": True,
            "servers": list(result.get("servers") or []),
            "interval_sec": result.get("interval_sec"),
            "enabled": bool(result.get("enabled")),
        }

    def save_admin_monitor_servers(
        self,
        session_uid: str,
        *,
        servers: List[Dict[str, Any]],
        enabled: Optional[bool] = None,
        interval_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        return DesktopAdminFacade.save_admin_monitor_servers(
            self, session_uid, servers=servers, enabled=enabled, interval_sec=interval_sec
        )

    def get_admin_actions_ssh(self, session_uid: str) -> Dict[str, Any]:
        try:
            result = ApplicationFacade._admin_config_service(self).get_ssh_actions(session_uid)
        except AdminConfigServiceError as exc:
            return {"ok": False, "error": ApplicationFacade._desktop_admin_config_error(exc)}
        return {"ok": True, "actions": list(result.get("actions") or [])}

    def save_admin_actions_ssh(
        self,
        session_uid: str,
        *,
        actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return DesktopAdminFacade.save_admin_actions_ssh(self, session_uid, actions=actions)

    # ---------- admin chat ----------

    def _resolve_admin_chat_service(self) -> Any:
        self._ensure_modes_ready()
        if not self.mode_registry_service:
            return None
        try:
            plugin = self.mode_registry_service.get("admin")
        except Exception:
            self.logger.exception("desktop _resolve_admin_chat_service: plugin lookup failed")
            return None
        service = getattr(plugin, "_chat_service", None) if plugin is not None else None
        return service

    def _require_session_workdir(self, session_uid: str) -> tuple[Optional[Any], Optional[str]]:
        session = self.session_service.get_session_by_uid(session_uid)
        if session is None:
            return None, None
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            return session, None
        return session, workdir

    def get_admin_chat_messages(self, session_uid: str) -> Dict[str, Any]:
        service = self._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        _, workdir = self._require_session_workdir(session_uid)
        if not workdir:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            messages = service.list_messages(workdir)
        except Exception as exc:
            self.logger.exception(
                "desktop get_admin_chat_messages failed session_uid=%s", session_uid
            )
            return {"ok": False, "error": f"list_failed:{exc}"}
        return {"ok": True, "messages": messages}

    def get_admin_chat_pending(self, session_uid: str) -> Dict[str, Any]:
        service = self._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        _, workdir = self._require_session_workdir(session_uid)
        if not workdir:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            items = service.list_pending(workdir)
        except Exception as exc:
            self.logger.exception(
                "desktop get_admin_chat_pending failed session_uid=%s", session_uid
            )
            return {"ok": False, "error": f"list_failed:{exc}"}
        return {"ok": True, "items": items}

    def get_admin_chat_memory_md(self, session_uid: str) -> Dict[str, Any]:
        service = self._resolve_admin_chat_service()
        if service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        _, workdir = self._require_session_workdir(session_uid)
        if not workdir:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            text = service.get_memory_md(workdir)
        except Exception as exc:
            self.logger.exception(
                "desktop get_admin_chat_memory_md failed session_uid=%s", session_uid
            )
            return {"ok": False, "error": f"read_failed:{exc}"}
        return {"ok": True, "text": text}

    def save_admin_chat_memory_md(
        self, session_uid: str, *, text: str
    ) -> Dict[str, Any]:
        return DesktopAdminFacade.save_admin_chat_memory_md(self, session_uid, text=text)

    def reject_admin_chat_pending(
        self, session_uid: str, *, approval_id: str
    ) -> Dict[str, Any]:
        return DesktopAdminFacade.reject_admin_chat_pending(self, session_uid, approval_id=approval_id)

    async def post_admin_chat_message(
        self, session_uid: str, *, text: str
    ) -> Dict[str, Any]:
        return await DesktopAdminFacade.post_admin_chat_message(self, session_uid, text=text)

    async def approve_admin_chat_pending(
        self, session_uid: str, *, approval_id: str
    ) -> Dict[str, Any]:
        return await DesktopAdminFacade.approve_admin_chat_pending(self, session_uid, approval_id=approval_id)

    def list_admin_runs(
        self,
        session_uid: str,
        *,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        return DesktopAdminFacade.list_admin_runs(self, session_uid, limit=limit)

    def get_admin_run_detail(
        self,
        session_uid: str,
        *,
        run_id: str,
        events_limit: int = 50,
    ) -> Optional[Dict[str, Any]]:
        return DesktopAdminFacade.get_admin_run_detail(self, session_uid, run_id=run_id, events_limit=events_limit)

    def list_scheduler_projects(self) -> List[Dict[str, Any]]:
        provider = self._desktop_identity_provider_service()
        return [
            {
                "slug": str(item.slug),
                "name": str(item.name),
                "path": str(item.path),
                "enabled": bool(item.enabled),
                "owner_id": str(item.owner_id),
            }
            for item in provider.list_owned_projects()
        ]

    def resolve_scheduler_project_slug(self, session_uid: str) -> Optional[str]:
        provider = self._desktop_identity_provider_service()
        return provider.resolve_project_slug(str(session_uid or ""))

    def list_scheduler_notification_targets(self, *, project_slug: str) -> List[Dict[str, Any]]:
        provider = self._desktop_identity_provider_service()
        return [
            {
                "session_id": str(item.session_id),
                "session_uid": str(item.session_uid),
                "label": str(item.label),
                "workdir": str(item.workdir),
                "project_slug": str(item.project_slug),
            }
            for item in provider.list_notification_targets(project_slug)
        ]

    def list_scheduler_jobs(self, *, project_slug: str) -> List[Dict[str, Any]]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        service = self._desktop_scheduler_service_instance()
        presentation = self._desktop_scheduler_presentation_service()
        jobs = []
        for job in service.list_jobs(owner_id=provider.owner_id):
            if presentation.project_slug_for_job(job) != str(project.slug):
                continue
            jobs.append(presentation.serialize_job(job, project_slug=str(project.slug)))
        return jobs

    def get_scheduler_job(self, *, project_slug: str, job_id: str) -> Dict[str, Any]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        presentation = self._desktop_scheduler_presentation_service()
        current = self._require_desktop_scheduler_project_job(
            provider=provider,
            project_slug=str(project.slug),
            job_id=str(job_id or ""),
        )
        return presentation.serialize_job(current, project_slug=str(project.slug))

    async def publish_mode_launch_request(
        self,
        *,
        project_slug: str,
        session_uid: str,
        mode_id: str,
        prompt: str = "",
        dry_run: bool = False,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        cfg = self._require_desktop_config()
        previous_state_path = str(self._desktop_state_path or "").strip()
        try:
            current_state_path = (
                normalize_optional_state_path(getattr(getattr(cfg, "defaults", None), "state_path", None)) or ""
            )
        except TypeError:
            current_state_path = ""
        preserved_event_bus = self._desktop_system_event_bus
        provider = self._desktop_identity_provider_service()
        if (
            preserved_event_bus is not None
            and self._desktop_system_event_bus is None
            and previous_state_path in {"", current_state_path}
        ):
            self._desktop_system_event_bus = preserved_event_bus
        project = provider.require_owned_project(project_slug)
        target_mode = str(mode_id or "").strip()
        if not target_mode:
            raise ValueError("mode_id is required")

        session = provider.resolve_session(session_uid)
        if session is None:
            raise ProjectOwnershipError(f"desktop session is not found: {session_uid}")
        resolved_project_slug = provider.resolve_project_slug(session_uid)
        if str(resolved_project_slug or "") != str(project.slug):
            raise ProjectOwnershipError(
                f"desktop session is outside owned project: {project.slug}"
            )

        resolved_session_uid = str(
            getattr(getattr(session, "conversation_scope", None), "session_uid", "") or ""
        ).strip() or f"desktop:{str(getattr(session, 'id', '') or '').strip()}"
        resolved_prompt = str(prompt or "").strip()
        resolved_correlation_id = str(correlation_id or "").strip() or secrets.token_hex(8)
        launch_policy = self._resolve_desktop_mode_launch_policy(session=session, mode_id=target_mode)
        await self._ensure_desktop_event_runtime_started()
        actor_payload = {
            "kind": "desktop",
            "actor_id": str(provider.owner_id),
            "owner_id": str(provider.owner_id),
        }
        if launch_policy.actor_chat_id is not None:
            actor_payload["chat_id"] = int(launch_policy.actor_chat_id)
        await self._desktop_system_event_bus_instance().publish(
            DesktopCommandEvent(
                session_uid=resolved_session_uid,
                project_slug=str(project.slug),
                command=target_mode,
                correlation_id=resolved_correlation_id,
                payload={
                    "mode_id": target_mode,
                    "prompt": resolved_prompt,
                    "text": resolved_prompt,
                    "dry_run": bool(dry_run),
                    "actor": actor_payload,
                    "launch_policy": {
                        "actor_chat_id": launch_policy.actor_chat_id,
                        "is_mode_allowed": bool(launch_policy.is_mode_allowed),
                        "reason": str(launch_policy.reason or ""),
                    },
                },
            )
        )
        return {
            "ok": True,
            "queued": True,
            "mode_id": target_mode,
            "project_slug": str(project.slug),
            "session_uid": resolved_session_uid,
            "correlation_id": resolved_correlation_id,
        }

    def _resolve_desktop_mode_launch_policy(
        self,
        *,
        session: Any,
        mode_id: str,
    ) -> DesktopModeLaunchPolicy:
        provider = self._desktop_identity_provider_service()
        actor_chat_id = provider.resolve_mode_launch_actor_chat_id(session)
        if actor_chat_id is None:
            return DesktopModeLaunchPolicy(
                actor_chat_id=None,
                is_mode_allowed=False,
                reason="actor_unresolved",
            )
        access_policy = getattr(self._desktop_bot_app(), "access_policy_service", None)
        is_mode_allowed = bool(
            access_policy.is_mode_allowed_for_chat(actor_chat_id, str(mode_id or ""))
        ) if access_policy is not None and hasattr(access_policy, "is_mode_allowed_for_chat") else False
        return DesktopModeLaunchPolicy(
            actor_chat_id=int(actor_chat_id),
            is_mode_allowed=bool(is_mode_allowed),
            reason="" if is_mode_allowed else "mode_not_allowed",
        )

    def _is_desktop_direct_cli_allowed(self, *, session: Any) -> bool:
        try:
            provider = self._desktop_identity_provider_service()
        except RuntimeError:
            return True
        actor_chat_id = provider.resolve_mode_launch_actor_chat_id(session)
        if actor_chat_id is None:
            return True
        access_policy = getattr(self._desktop_bot_app(), "access_policy_service", None)
        if access_policy is None or not hasattr(access_policy, "is_mode_allowed_for_chat"):
            return True
        return bool(access_policy.is_mode_allowed_for_chat(actor_chat_id, DIRECT_CLI_MODE_ID))

    def _notify_desktop_direct_cli_denied(self, *, session_uid: str) -> None:
        self.notify(
            "ui:message",
            session_id=str(session_uid or ""),
            role="agent",
            text=AccessPolicyService.DIRECT_CLI_DENIED_TEXT,
            md2=True,
        )

    def create_scheduler_job(
        self,
        *,
        project_slug: str,
        cron: str,
        target_mode: str,
        notification_target_session_uid: str,
        enabled: bool = True,
        job_name: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        target = provider.require_notification_target(project.slug, notification_target_session_uid)
        service = self._desktop_scheduler_service_instance()
        created = service.create_job(
            owner_id=provider.owner_id,
            cron=str(cron or ""),
            target_mode=str(target_mode or ""),
            notification_target_telegram_session_uid=target.session_uid,
            enabled=bool(enabled),
            job_name=str(job_name or "").strip() or None,
            payload=self._desktop_scheduler_presentation_service().payload_for_project(
                project_slug=str(project.slug),
                payload=payload,
            ),
        )
        return self._desktop_scheduler_presentation_service().serialize_job(
            created,
            project_slug=str(project.slug),
        )

    def update_scheduler_job(
        self,
        *,
        project_slug: str,
        job_id: str,
        cron: Optional[str] = None,
        target_mode: Optional[str] = None,
        notification_target_session_uid: Optional[str] = None,
        enabled: Optional[bool] = None,
        job_name: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        service = self._desktop_scheduler_service_instance()
        presentation = self._desktop_scheduler_presentation_service()
        current = self._require_desktop_scheduler_project_job(
            provider=provider,
            project_slug=str(project.slug),
            job_id=str(job_id or ""),
        )
        resolved_target = None
        if notification_target_session_uid is not None:
            resolved_target = provider.require_notification_target(
                project.slug,
                notification_target_session_uid,
            ).session_uid
        updated = service.update_job(
            owner_id=provider.owner_id,
            job_id=str(job_id or ""),
            cron=cron,
            target_mode=target_mode,
            notification_target_telegram_session_uid=resolved_target,
            enabled=enabled,
            job_name=job_name,
            payload=presentation.payload_for_project(
                project_slug=str(project.slug),
                payload=current.payload if payload is None else payload,
            ),
        )
        return presentation.serialize_job(updated, project_slug=str(project.slug))

    def delete_scheduler_job(self, *, project_slug: str, job_id: str) -> bool:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        if not str(job_id or "").strip():
            return False
        presentation = self._desktop_scheduler_presentation_service()
        try:
            presentation.require_project_job(
                str(project.slug),
                str(job_id or ""),
                owner_id=provider.owner_id,
            )
        except SchedulerNotFoundError:
            return False
        except SchedulerOwnershipError as exc:
            raise ProjectOwnershipError(
                f"scheduler job is outside owned project: {project.slug}"
            ) from exc
        service = self._desktop_scheduler_service_instance()
        return bool(service.delete_job(owner_id=provider.owner_id, job_id=str(job_id or "")))

    def pause_scheduler_job(self, *, project_slug: str, job_id: str) -> Dict[str, Any]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        service = self._desktop_scheduler_service_instance()
        self._require_desktop_scheduler_project_job(
            provider=provider,
            project_slug=str(project.slug),
            job_id=str(job_id or ""),
        )
        paused = service.pause_job(owner_id=provider.owner_id, job_id=str(job_id or ""))
        return self._desktop_scheduler_presentation_service().serialize_job(
            paused,
            project_slug=str(project.slug),
        )

    def resume_scheduler_job(self, *, project_slug: str, job_id: str) -> Dict[str, Any]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        service = self._desktop_scheduler_service_instance()
        self._require_desktop_scheduler_project_job(
            provider=provider,
            project_slug=str(project.slug),
            job_id=str(job_id or ""),
        )
        resumed = service.resume_job(owner_id=provider.owner_id, job_id=str(job_id or ""))
        return self._desktop_scheduler_presentation_service().serialize_job(
            resumed,
            project_slug=str(project.slug),
        )

    async def run_scheduler_job_now(self, *, project_slug: str, job_id: str) -> Dict[str, Any]:
        provider = self._desktop_identity_provider_service()
        project = provider.require_owned_project(project_slug)
        service = self._desktop_scheduler_service_instance()
        self._require_desktop_scheduler_project_job(
            provider=provider,
            project_slug=str(project.slug),
            job_id=str(job_id or ""),
        )
        event = await service.run_now(owner_id=provider.owner_id, job_id=str(job_id or ""))
        return {
            "job_id": str(event.job_id),
            "job_name": str(event.job_name),
            "status": str(event.status),
            "scheduled_for": float(event.scheduled_for or 0.0),
            "cron": str(event.cron),
            "target_mode": str(event.target_mode),
            "owner_id": str(event.owner_id),
            "notification_target": dict(event.notification_target or {}),
            "payload": dict(event.payload or {}),
        }

    def set_theme(self, theme_name: str) -> bool:
        """Меняет тему приложения и уведомляет подписчиков."""
        if self.theme_service.set_theme(theme_name):
            self.notify("ui:theme_changed", theme=theme_name)
            if self.ui_state_service:
                asyncio.create_task(self.ui_state_service.save(theme=theme_name))
            return True
        return False

    def notify(self, event: str, **payload: Any) -> None:
        normalized = dict(payload)
        session_token = str(normalized.get("session_uid") or normalized.get("session_id") or "").strip()
        if session_token:
            normalized.setdefault("session_uid", session_token)
            normalized.setdefault("session_id", session_token)
        note = AppNotification(event=str(event), payload=normalized)
        for callback in list(self._subscribers):
            try:
                callback(note)
            except Exception:
                self.logger.exception("notification callback failed event=%s", event)

    def list_active_tasks(
        self,
        *,
        session_uid: Optional[str] = None,
    ) -> list[Any]:
        """Desktop-friendly snapshot of active background tasks."""
        return list(self.task_service.list_active(session_id=session_uid))

    def list_runs(
        self,
        session_uid: str,
        *,
        mode_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        session_token = str(session_uid or "").strip()
        if not session_token:
            return []
        session = self.session_service.get_session_by_uid(session_token)
        if session is None:
            return []
        store = self._desktop_run_operations().artifact_store
        handles = store.list_runs(session=session, mode_id=mode_id, limit=limit)
        policy_user_id = self._desktop_run_policy_user_id(session)
        policy_is_admin = self._desktop_admin_actions_allowed(session_token)
        return [
            self._serialize_run_listing(
                store=store,
                run=handle,
                session=session,
                run_policy_user_id=policy_user_id,
                run_policy_is_admin=policy_is_admin,
            )
            for handle in handles
        ]

    async def doctor_run(
        self,
        session_uid: str,
        *,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._execute_desktop_run_operation(
            "doctor",
            session_uid=session_uid,
            mode_id=mode_id,
            run_id=run_id,
        )

    async def recover_run(
        self,
        session_uid: str,
        *,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._execute_desktop_run_operation(
            "recover",
            session_uid=session_uid,
            mode_id=mode_id,
            run_id=run_id,
        )

    async def resume_run(
        self,
        session_uid: str,
        *,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._execute_desktop_run_operation(
            "resume",
            session_uid=session_uid,
            mode_id=mode_id,
            run_id=run_id,
        )

    async def apply_recommendation_run(
        self,
        session_uid: str,
        *,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._execute_desktop_run_operation(
            "apply_recommendation",
            session_uid=session_uid,
            mode_id=mode_id,
            run_id=run_id,
        )

    async def promote_run_skills(
        self,
        session_uid: str,
        *,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_token = str(session_uid or "").strip()
        session = self.session_service.get_session_by_uid(session_token)
        if session is None:
            payload = {
                "status": "not_found",
                "message": "Сессия не найдена.",
                "mode_id": str(mode_id or "").strip() or None,
                "run_id": str(run_id or "").strip() or None,
                "promoted_skill_ids": [],
                "skipped_skill_ids": [],
                "results": [],
            }
            self.notify("ui:runs_updated", session_uid=session_token, operation="promote_run_skills", status="not_found")
            return payload
        decision = self._desktop_run_policy_decision(
            operation="promote_run_skills",
            session=session,
            session_uid=session_token,
        )
        if not bool(getattr(decision, "allowed", False)):
            reason = str(getattr(decision, "reason", "") or "policy_denied")
            payload = {
                "operation": "promote_run_skills",
                "status": "denied",
                "message": f"Run-операция запрещена policy: {reason}.",
                "mode_id": str(mode_id or "").strip() or None,
                "run_id": str(run_id or "").strip() or None,
                "blocked_by": [reason],
                "promoted_skill_ids": [],
                "skipped_skill_ids": [],
                "results": [],
                "policy": self._desktop_run_policy_payload(decision),
            }
            self.notify("ui:runs_updated", session_uid=session_token, operation="promote_run_skills", status="denied")
            self.notify(
                "ui:message",
                session_uid=session_token,
                role="agent",
                text=str(payload["message"]),
                md2=True,
            )
            return payload
        skill_runtime = self._desktop_mode_dependencies().skill_runtime
        execution_context, execution_dest = self._desktop_recovery_execution_vector(
            session=session,
            context=None,
            dest=None,
        )
        result = skill_runtime.promote_run_skills(
            session=session,
            run_artifact_store=self._desktop_run_operations().artifact_store,
            mode_id=mode_id,
            run_id=run_id,
            is_admin=True,
            context=execution_context,
            dest=execution_dest,
        )
        payload = result.to_dict()
        self.notify(
            "ui:runs_updated",
            session_uid=session_token,
            operation="promote_run_skills",
            status=str(payload.get("status") or ""),
            mode_id=str(payload.get("mode_id") or ""),
            run_id=str(payload.get("run_id") or "") or None,
        )
        message = str(payload.get("message") or "").strip()
        if message:
            self.notify(
                "ui:message",
                session_uid=session_token,
                role="agent",
                text=message,
                md2=True,
            )
        return payload

    def list_pending_skill_installs(
        self,
        session_uid: str,
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        session_token = str(session_uid or "").strip()
        if not session_token:
            return []
        session = self.session_service.get_session_by_uid(session_token)
        if session is None or not self._desktop_admin_actions_allowed(session_token):
            return []
        skill_runtime = self._desktop_mode_dependencies().skill_runtime
        records = list(skill_runtime.list_pending_installs(session=session) or [])
        window = records[-max(0, int(limit or 0)) or 50:]
        serialized = [
            {
                "approval_id": str(item.approval_id),
                "skill_id": str(item.skill_id),
                "mode_id": str(item.mode_id),
                "phase": str(item.phase),
                "source": str(item.source),
                "acquisition_source": str(item.acquisition_source),
                "ref": str(item.ref),
                "created_at": float(item.created_at or 0.0),
                "requester": dict(item.requester),
            }
            for item in reversed(window)
        ]
        return serialized

    async def approve_pending_skill_install(
        self,
        session_uid: str,
        *,
        approval_id: str,
    ) -> Dict[str, Any]:
        return await self._execute_desktop_skill_install_action(
            "approve",
            session_uid=session_uid,
            approval_id=approval_id,
        )

    async def reject_pending_skill_install(
        self,
        session_uid: str,
        *,
        approval_id: str,
    ) -> Dict[str, Any]:
        return await self._execute_desktop_skill_install_action(
            "reject",
            session_uid=session_uid,
            approval_id=approval_id,
        )

    async def _execute_desktop_skill_install_action(
        self,
        operation: str,
        *,
        session_uid: str,
        approval_id: str,
    ) -> Dict[str, Any]:
        session_token = str(session_uid or "").strip()
        approval_token = str(approval_id or "").strip()
        session = self.session_service.get_session_by_uid(session_token)
        if session is None:
            payload = {
                "status": "not_found",
                "approval_id": approval_token,
                "skill_id": "",
                "message": "Сессия не найдена.",
                "manifest_path": None,
            }
            self.notify(
                "ui:session_updated",
                session_uid=session_token,
                operation=f"{operation}_pending_skill_install",
                status="not_found",
            )
            return payload
        if not self._desktop_admin_actions_allowed(session_token):
            payload = {
                "status": "denied",
                "approval_id": approval_token,
                "skill_id": "",
                "message": "Административные skill-действия доступны только при активном admin mode.",
                "manifest_path": None,
            }
            self.notify(
                "ui:session_updated",
                session_uid=session_token,
                operation=f"{operation}_pending_skill_install",
                status="denied",
                approval_id=approval_token,
            )
            self.notify(
                "ui:message",
                session_uid=session_token,
                role="agent",
                text=str(payload["message"]),
                md2=True,
            )
            return payload
        skill_runtime = self._desktop_mode_dependencies().skill_runtime
        method = (
            skill_runtime.approve_pending_install
            if str(operation or "").strip() == "approve"
            else skill_runtime.reject_pending_install
        )
        result = method(
            session=session,
            approval_id=approval_token,
            is_admin=True,
        )
        payload = result.to_dict()
        self.notify(
            "ui:session_updated",
            session_uid=session_token,
            operation=f"{operation}_pending_skill_install",
            status=str(payload.get("status") or ""),
            approval_id=approval_token,
        )
        message = str(payload.get("message") or "").strip()
        if message:
            self.notify(
                "ui:message",
                session_uid=session_token,
                role="agent",
                text=message,
                md2=True,
            )
        return payload

    def _desktop_admin_actions_allowed(self, session_uid: str) -> bool:
        payload = self.get_admin_status_payload(str(session_uid or "").strip())
        return bool(isinstance(payload, dict) and payload.get("active"))

    @staticmethod
    def _desktop_run_policy_operation(operation: str) -> str:
        token = str(operation or "").strip()
        if token == "promote_run_skills":
            return "promote_skills"
        return token

    @staticmethod
    def _desktop_run_policy_payload(decision: Any) -> Dict[str, Any]:
        return {
            "allowed": bool(getattr(decision, "allowed", False)),
            "reason": str(getattr(decision, "reason", "") or ""),
            "visibility": str(getattr(decision, "visibility", "") or "hide"),
        }

    def _desktop_run_policy_user_id(self, session: Any) -> Any:
        try:
            actor_chat_id = self._desktop_identity_provider_service().resolve_mode_launch_actor_chat_id(session)
        except Exception:
            self.logger.warning(
                "legacy fallback used: desktop run policy actor resolution failed session_id=%s",
                getattr(session, "id", ""),
                exc_info=True,
            )
            actor_chat_id = None
        if actor_chat_id is not None:
            return actor_chat_id
        scope = getattr(session, "conversation_scope", None)
        for candidate in (
            getattr(session, "mode_launch_actor_chat_id", None),
            getattr(session, "owner_chat_id", None),
            getattr(session, "telegram_chat_id", None),
            getattr(session, "chat_id", None),
            getattr(scope, "chat_id", None),
        ):
            token = str(candidate or "").strip()
            if token:
                return candidate
        return desktop_actor_id()

    def _desktop_run_policy_decision(
        self,
        *,
        operation: str,
        session: Any,
        session_uid: str,
    ) -> Any:
        return self._desktop_run_operations_policy.can_run_operation(
            operation=self._desktop_run_policy_operation(operation),
            user_id=self._desktop_run_policy_user_id(session),
            is_admin=self._desktop_admin_actions_allowed(session_uid),
            session=session,
            surface="desktop",
        )

    def _desktop_run_denied_payload(
        self,
        *,
        operation: str,
        mode_id: Optional[str],
        run_id: Optional[str],
        decision: Any,
    ) -> Dict[str, Any]:
        reason = str(getattr(decision, "reason", "") or "policy_denied")
        return {
            "operation": str(operation or "").strip(),
            "status": "denied",
            "mode_id": str(mode_id or "").strip(),
            "phase": "",
            "message": f"Run-операция запрещена policy: {reason}.",
            "run_id": str(run_id or "").strip() or None,
            "recommended_action": None,
            "blocked_by": [reason],
            "report": None,
            "policy": self._desktop_run_policy_payload(decision),
        }

    async def _execute_desktop_run_operation(
        self,
        operation: str,
        *,
        session_uid: str,
        mode_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_token = str(session_uid or "").strip()
        session = self.session_service.get_session_by_uid(session_token)
        if session is None:
            result = {
                "operation": str(operation or "").strip(),
                "status": "not_found",
                "mode_id": str(mode_id or "").strip(),
                "phase": "",
                "message": "Сессия не найдена.",
                "run_id": str(run_id or "").strip() or None,
                "recommended_action": None,
                "blocked_by": [],
                "report": None,
            }
            self.notify("ui:runs_updated", session_uid=session_token, operation=operation, status="not_found")
            return result
        decision = self._desktop_run_policy_decision(
            operation=operation,
            session=session,
            session_uid=session_token,
        )
        if not bool(getattr(decision, "allowed", False)):
            result = self._desktop_run_denied_payload(
                operation=operation,
                mode_id=mode_id,
                run_id=run_id,
                decision=decision,
            )
            self.notify("ui:runs_updated", session_uid=session_token, operation=operation, status="denied")
            self.notify(
                "ui:message",
                session_uid=session_token,
                role="agent",
                text=str(result["message"]),
                md2=True,
            )
            return result
        service = self._desktop_run_operations()
        method = getattr(service, f"{str(operation or '').strip()}_run", None)
        if not callable(method):
            result = {
                "operation": str(operation or "").strip(),
                "status": "disabled",
                "mode_id": str(mode_id or "").strip(),
                "phase": "",
                "message": "Run-операция не поддерживается.",
                "run_id": str(run_id or "").strip() or None,
                "recommended_action": None,
                "blocked_by": [],
                "report": None,
            }
            self.notify("ui:runs_updated", session_uid=session_token, operation=operation, status="disabled")
            return result
        operation_kwargs = {
            "session": session,
            "mode_id": mode_id,
            "run_id": run_id,
        }
        recovery_context, recovery_dest = self._desktop_recovery_execution_vector(
            session=session,
            context=None,
            dest=None,
        )
        operation_kwargs["context"] = recovery_context
        operation_kwargs["dest"] = recovery_dest
        operation_result = await method(**operation_kwargs)
        recommended_action = str(operation_result.recommended_action or "") or None
        if recommended_action == "no_action":
            recommended_action = None
        payload = {
            "operation": str(operation_result.operation),
            "status": str(operation_result.status),
            "mode_id": str(operation_result.mode_id or ""),
            "phase": str(operation_result.phase or ""),
            "message": str(operation_result.message or ""),
            "run_id": str(operation_result.run_id or "") or None,
            "recommended_action": recommended_action,
            "blocked_by": list(operation_result.blocked_by or ()),
            "report": dict(operation_result.report or {}) if isinstance(operation_result.report, dict) else None,
        }
        self.notify(
            "ui:runs_updated",
            session_uid=session_token,
            operation=payload["operation"],
            status=payload["status"],
            mode_id=payload["mode_id"],
            run_id=payload["run_id"],
        )
        if payload["message"]:
            self.notify(
                "ui:message",
                session_uid=session_token,
                role="agent",
                text=payload["message"],
                md2=True,
            )
        return payload

    def _desktop_recovery_execution_vector(
        self,
        *,
        session: Any,
        context: Any,
        dest: Optional[Dict[str, Any]],
    ) -> tuple[Any, Dict[str, Any]]:
        session_uid = session_runtime_uid(session)
        resolved_dest = dict(dest or {})
        resolved_dest["kind"] = "desktop"
        resolved_dest["chat_id"] = session_uid
        resolved_context = context
        if resolved_context is None:
            resolved_context = SimpleNamespace(
                transport="desktop",
                session_uid=session_uid,
            )
        return resolved_context, resolved_dest

    async def _execute_recommended_run_action(
        self,
        *,
        session: Any,
        mode_id: str,
        operation: str,
        run: Any,
        state: Dict[str, Any],
        report: Any,
        context: Any = None,
        dest: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_mode = str(mode_id or "").strip()
        operation_name = str(operation or "").strip()
        if not operation_name:
            return {"status": "blocked", "message": "Recovery operation не определена."}
        self._ensure_modes_ready()
        mode = self.mode_registry_service.get(resolved_mode) if self.mode_registry_service else None
        if mode is None:
            return {"status": "blocked", "message": f"Mode `{resolved_mode}` недоступен."}
        desktop_bot_app = self._desktop_bot_app()
        resolved_context, resolved_dest = self._desktop_recovery_execution_vector(
            session=session,
            context=context,
            dest=dest or build_recovery_dest(default_kind="desktop", session=session, state=state),
        )
        custom_executor = getattr(mode, "execute_recovery_action", None)
        if callable(custom_executor):
            return await custom_executor(
                session=session,
                action=operation_name,
                run=run,
                state=state,
                report=report,
                bot_app=desktop_bot_app,
                context=resolved_context,
                dest=resolved_dest,
            )
        if resolved_mode == "codebase_mapper":
            if not hasattr(mode, "run_pipeline"):
                return {"status": "blocked", "message": "Codebase Mapper mode недоступен."}
            output = await mode.run_pipeline(
                session=session,
                user_text=operation_name,
                bot_app=desktop_bot_app,
                context=resolved_context,
                dest=resolved_dest,
            )
            return {
                "status": "ok",
                "message": str(output or "").strip() or f"Операция `{operation_name}` выполнена.",
                "executed_operation": operation_name,
                "executed_via": "mode_run_pipeline",
            }
        if not hasattr(mode, "run_pipeline"):
            return {"status": "blocked", "message": f"Recovery недоступен для режима `{resolved_mode}`."}
        prompt_text = build_recovery_prompt(
            session=session,
            mode_id=resolved_mode,
            action=operation_name,
            state=state,
        )
        if not prompt_text:
            return {
                "status": "blocked",
                "message": "Не удалось восстановить входные данные для recovery action.",
                "executed_operation": operation_name,
            }
        artifact_store = self._desktop_run_operations().artifact_store
        latest_before = artifact_store.latest_run(session=session, mode_id=resolved_mode)
        output = await mode.run_pipeline(
            session=session,
            user_text=prompt_text,
            bot_app=desktop_bot_app,
            context=resolved_context,
            dest=resolved_dest,
        )
        latest_after = artifact_store.latest_run(session=session, mode_id=resolved_mode)
        payload = {
            "status": "ok",
            "message": str(output or "").strip() or f"Операция `{operation_name}` выполнена.",
            "executed_operation": operation_name,
            "executed_via": f"mode_run_pipeline:{operation_name}",
        }
        if latest_after is not None:
            before_run_id = str(getattr(latest_before, "run_id", "") or "")
            after_run_id = str(getattr(latest_after, "run_id", "") or "")
            if after_run_id and after_run_id not in {before_run_id, str(run.run_id)}:
                payload["spawned_run_id"] = after_run_id
        return payload

    def _serialize_run_listing(
        self,
        *,
        store: Any,
        run: Any,
        session: Any,
        run_policy_user_id: Any,
        run_policy_is_admin: bool,
    ) -> Dict[str, Any]:
        state = store.load_state(run)
        recovery = store.load_recovery(run)
        events = store.load_events_tail(run, limit=24)
        status = clean_run_listing_text(state.get("status"), max_len=32) or "running"
        phase = clean_run_listing_text(state.get("phase"), max_len=64) or "unknown"
        mode_context = state.get("mode_context") if isinstance(state.get("mode_context"), dict) else {}
        issues = recovery.get("issues") if isinstance(recovery.get("issues"), list) else []
        issue_codes = [
            clean_run_listing_text(item.get("code"), max_len=64)
            for item in issues
            if isinstance(item, dict) and clean_run_listing_text(item.get("code"), max_len=64)
        ]
        recommended_action = clean_run_listing_text(recovery.get("recommended_action"), max_len=64) or None
        if recommended_action == "no_action":
            recommended_action = None
        can_resume = bool(recovery.get("can_resume")) if recovery else False
        can_recover = bool(recommended_action) and recommended_action in {
            "rollback_to_checkpoint",
            "restart_from_phase",
            "replay_finalize",
            "mark_failed",
        }
        terminal_status = is_terminal_status(status)
        terminal_actions_blocked = status in {"completed", "superseded"}
        if terminal_actions_blocked:
            can_resume = False
            can_recover = False
        last_requested_operation = (
            dict(recovery.get("last_requested_operation"))
            if isinstance(recovery.get("last_requested_operation"), dict)
            else None
        )
        skill_log = summarize_run_skill_log(events, state)
        selected_skill_ids = [
            clean_run_listing_text(item, max_len=64)
            for item in list(state.get("selected_skill_ids") or [])
            if clean_run_listing_text(item, max_len=64)
        ]
        project_local_skill_ids = self._project_local_selected_skill_ids(session=session, skill_ids=selected_skill_ids)
        started_at = float(state.get("started_at") or 0.0)
        updated_at = float(state.get("updated_at") or 0.0)
        finished_at_raw = state.get("finished_at")
        finished_at = float(finished_at_raw or 0.0) if finished_at_raw not in (None, "") else None
        can_apply_recommendation = (
            str(run.mode_id or "").strip() == "codebase_mapper"
            and (recommended_action or "") in {"rerun_same_operation", "run_validate", "run_repair"}
        )
        run_operations_policy = {
            operation: self._desktop_run_policy_payload(
                self._desktop_run_operations_policy.can_run_operation(
                    operation=operation,
                    user_id=run_policy_user_id,
                    is_admin=run_policy_is_admin,
                    session=session,
                    surface="desktop",
                )
            )
            for operation in (
                "doctor",
                "recover",
                "resume",
                "apply_recommendation",
                "promote_skills",
            )
        }
        return {
            "session_uid": str(run.session_uid),
            "mode_id": str(run.mode_id),
            "run_id": str(run.run_id),
            "status": status,
            "phase": phase,
            "started_at": started_at,
            "updated_at": updated_at,
            "finished_at": finished_at,
            "active": not finished_at and not terminal_status,
            "terminal_status": terminal_status,
            "terminal_actions_blocked": terminal_actions_blocked,
            "current_unit_id": clean_run_listing_text(
                state.get("current_unit_id") or state.get("current_step_id"),
                max_len=128,
            )
            or None,
            "recommended_action": recommended_action,
            "can_resume": can_resume,
            "can_recover": can_recover,
            "can_apply_recommendation": can_apply_recommendation,
            "run_operations_policy": run_operations_policy,
            "issue_codes": issue_codes,
            "last_requested_operation": last_requested_operation,
            "skill_log": skill_log,
            "selected_skill_ids": selected_skill_ids,
            "project_local_skill_ids": project_local_skill_ids,
            "cli_work_type": clean_run_listing_text(
                mode_context.get("cli_work_type"),
                max_len=64,
            )
            or None,
            "executor_profile": clean_run_listing_text(
                mode_context.get("executor_profile"),
                max_len=64,
            )
            or None,
        }

    def _project_local_selected_skill_ids(self, *, session: Any, skill_ids: List[str]) -> List[str]:
        cleaned = [
            clean_run_listing_text(item, max_len=64)
            for item in list(skill_ids or [])
            if clean_run_listing_text(item, max_len=64)
        ]
        if not cleaned:
            return []
        try:
            registry = self._desktop_mode_dependencies().skill_runtime.registry_service.load_registry(session=session)
        except Exception:
            self.logger.exception(
                "desktop run listing failed to resolve project-local skills session_uid=%s",
                session_runtime_uid(session),
            )
            return []
        return [
            skill_id
            for skill_id in cleaned
            if registry.project_manifests.get(skill_id) is not None
        ]

    @staticmethod
    def _validate_desktop_runtime_payload(payload: dict) -> dict:
        DesktopRuntimePayload.model_validate(dict(payload or {}))
        return payload

    def _resolve_desktop_session_uid(self, *args: Any, api_name: str) -> str:
        if len(args) != 1:
            raise TypeError(f"{api_name} expects (session_uid)")
        payload = self._validate_desktop_runtime_payload({"session_uid": args[0]})
        return str(payload.get("session_uid") or "").strip()

    def _parse_run_session_input_args(self, *args: Any) -> tuple[str, str]:
        if len(args) != 2:
            raise TypeError("run_session_input expects (session_uid, text)")
        payload = self._validate_desktop_runtime_payload({"session_uid": args[0]})
        return str(payload.get("session_uid") or "").strip(), str(args[1] or "")

    def set_task_priority(self, task_id: str, priority: int) -> bool:
        ok = bool(self.task_service.set_priority(str(task_id), int(priority)))
        if ok:
            self.notify("task:updated", task_id=str(task_id), priority=int(priority))
        return ok

    def update_task_progress(self, task_id: str, *, progress: float, stage: str = "") -> bool:
        ok = bool(self.task_service.set_progress(str(task_id), progress=float(progress), stage=str(stage or "")))
        if ok:
            self.notify("task:updated", task_id=str(task_id), progress=float(progress), stage=str(stage or ""))
        return ok

    async def cancel_task(self, task_id: str, *, reason: str = "cancelled", timeout_s: float = 1.0) -> bool:
        rec = self.task_service.get(str(task_id))
        ok = await self.task_service.cancel(str(task_id), reason=str(reason), timeout_s=float(timeout_s))
        if ok:
            self.notify(
                "task:cancelled",
                task_id=str(task_id),
                session_uid=(getattr(rec, "session_uid", None) if rec is not None else None),
                name=(getattr(rec, "name", "") if rec is not None else ""),
                reason=str(reason),
            )
        return bool(ok)

    def get_manager_plan(self, session_uid: str) -> Optional[Any]:
        """Загрузить план проекта для сессии."""
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return None
        try:
            from modes.sdk.planning import load_plan
            return load_plan(session.workdir, scoped_key=session_scoped_key(session))
        except Exception:
            self.logger.exception("get_manager_plan failed session_uid=%s", session_uid)
            return None

    async def export_data(self, session_uid: str) -> str:
        """
        Инициирует экспорт данных через CLI-обработчик.
        Вызывает session.run_prompt со специальной командой или использует внутренний экспорт.
        """
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            raise ValueError(f"unknown session: {session_uid}")

        # Для Manager Mode экспорт - это генерация отчета.
        # Мы можем вызвать это как фоновую задачу.
        try:
            from modes.sdk.planning import load_plan
            plan = load_plan(session.workdir, scoped_key=session_scoped_key(session))
            if not plan:
                return "План не найден, нечего экспортировать."

            # Эмуляция вызова CLI-обработчика через facade.run_session_input
            # или прямое использование manager runtime если он доступен.
            # По условию задачи "Экспорт вызывает CLI-обработчик через ApplicationFacade.export_data()".
            # Мы выполним экспорт как запрос к системе.
            return await self.run_session_input(
                session_uid,
                "Генерируй итоговый отчет и экспортируй данные проекта.",
            )
        except Exception as e:
            self.logger.exception("export_data failed")
            return f"Ошибка при экспорте: {e}"

    def resolve_analyst_question(self, question_id: str, answer: str) -> bool:
        """Резолвит вопрос от аналитика/агента, отвечая на него."""
        bot_app = self._desktop_bot_app()
        registry = getattr(bot_app, "_tool_registry", None)
        if registry and hasattr(registry, "resolve_question"):
            resolved = bool(registry.resolve_question(str(question_id), str(answer)))
            if resolved:
                self._clear_pending_question(str(question_id))
            return resolved
        return False

    def _clear_pending_question(self, question_id: str) -> bool:
        qid = str(question_id or "").strip()
        if not qid:
            return False

        bot_app = self._desktop_bot_app()
        pending_map = bot_app.ui_state.pending_questions
        active_by_chat = bot_app.ui_state.active_ask_question_by_chat
        registry = getattr(bot_app, "_tool_registry", None)
        registry_pending = getattr(registry, "pending_questions", None) if registry is not None else None

        meta = pending_map.pop(qid, None)
        if isinstance(meta, dict):
            candidate_keys: list[str] = []
            for key_name in ("chat_id", "session_uid"):
                try:
                    token = str(meta.get(key_name) or "").strip()
                except Exception:
                    token = ""
                if token:
                    candidate_keys.append(token)
            for candidate in candidate_keys:
                if str(active_by_chat.get(candidate) or "") == qid:
                    active_by_chat.pop(candidate, None)

        fut = registry_pending.pop(qid, None) if isinstance(registry_pending, dict) else None
        if fut is not None and hasattr(fut, "done") and not fut.done():
            try:
                fut.cancel()
            except Exception:
                self.logger.exception("desktop clear pending question future failed question_id=%s", qid)
        return bool(meta is not None or fut is not None)

    def _clear_pending_questions(self, *, session_uid: Optional[str] = None, chat_id: Optional[int] = None) -> int:
        sid = str(session_uid or "").strip() or None
        cid = chat_id if chat_id is not None else None

        bot_app = self._desktop_bot_app()
        pending_map = bot_app.ui_state.pending_questions
        active_by_chat = bot_app.ui_state.active_ask_question_by_chat

        removed = 0
        for qid, meta in list(pending_map.items()):
            if not isinstance(meta, dict):
                continue
            if sid is not None and str(meta.get("session_uid") or "") != sid:
                continue
            if cid is not None and int(meta.get("chat_id") or 0) != int(cid):
                continue
            if self._clear_pending_question(qid):
                removed += 1

        if cid is not None and cid in active_by_chat:
            active_qid = str(active_by_chat.get(cid) or "").strip()
            if active_qid and self._clear_pending_question(active_qid):
                removed += 1
            active_by_chat.pop(cid, None)

        return removed

    def _desktop_session_interrupt_service(self) -> SessionInterruptService:
        if self._desktop_interrupt_service is not None:
            return self._desktop_interrupt_service

        async def _cancel_session(session_uid: str, timeout_s: float) -> int:
            return int(await self.task_service.cancel_session(str(session_uid or ""), timeout_s=float(timeout_s)))

        def _list_session(session_uid: str) -> list[str]:
            return [str(rec.name or "") for rec in self.task_service.list_active(session_uid=str(session_uid or ""))]

        def _clear_pending_for_interrupt(_session: Any, reply_chat_id: Optional[int], _thread_id: Optional[int]) -> int:
            return int(self._clear_pending_questions(session_uid=session_runtime_uid(_session), chat_id=reply_chat_id))

        self._desktop_interrupt_service = SessionInterruptService(
            cancel_session_tasks=_cancel_session,
            list_session_tasks=_list_session,
            clear_pending_questions=_clear_pending_for_interrupt,
            logger_=self.logger,
        )
        return self._desktop_interrupt_service

    def _clear_mode_runtime_cache(self, session_uid: str) -> None:
        for runtime in self.iter_mode_runtimes():
            if runtime is None or not hasattr(runtime, "clear_session_cache"):
                continue
            try:
                runtime.clear_session_cache(session_uid)
            except Exception:
                self.logger.exception(
                    "desktop clear_session_cache failed session_uid=%s runtime=%s",
                    session_uid,
                    runtime.__class__.__name__,
                )

    def _desktop_agent_sandbox_service(self) -> AgentSandboxService:
        workdir = ""
        cfg = self.config
        if cfg is not None:
            workdir = str(getattr(getattr(cfg, "defaults", None), "workdir", "") or "").strip()
        if not workdir and self.runtime_params is not None:
            workdir = str(getattr(self.runtime_params, "workdir", "") or "").strip()
        if not workdir:
            workdir = os.getcwd()

        if self._desktop_sandbox_service is None or self._desktop_sandbox_workdir != workdir:
            self._desktop_sandbox_service = AgentSandboxService(workdir)
            self._desktop_sandbox_workdir = workdir
        try:
            self._desktop_sandbox_service.configure()
        except Exception:
            self.logger.exception("desktop sandbox configure failed workdir=%s", workdir)
        return self._desktop_sandbox_service

    async def start(self, *, validate_secrets: bool = True) -> AppRuntimeParams:
        self.notify("startup:begin")
        previous_config = self.config
        cfg = await self.config_service.load()
        scheduler_service_to_stop: Optional[SchedulerService] = None
        if previous_config is not None and self._desktop_scheduler_service is not None:
            previous_scheduler = getattr(previous_config, "scheduler", None)
            next_scheduler = getattr(cfg, "scheduler", None)
            try:
                previous_state_path = normalize_optional_state_path(
                    getattr(getattr(previous_config, "defaults", None), "state_path", None)
                ) or ""
                next_state_path = normalize_optional_state_path(
                    getattr(getattr(cfg, "defaults", None), "state_path", None)
                ) or ""
            except TypeError:
                previous_state_path = ""
                next_state_path = ""
            if previous_scheduler != next_scheduler or previous_state_path != next_state_path:
                scheduler_service_to_stop = self._desktop_scheduler_service
        if scheduler_service_to_stop is not None:
            try:
                await scheduler_service_to_stop.stop()
            except Exception:
                self.logger.exception("desktop scheduler stop before config reload failed")
            finally:
                if self._desktop_scheduler_started_instance is scheduler_service_to_stop:
                    self._desktop_scheduler_started_instance = None
                self._desktop_scheduler_service = None
        self.config = cfg
        self.cli_limits_service.set_gemini_oauth_client_secret(
            getattr(cfg.defaults, "gemini_oauth_client_secret", None)
        )
        self._desktop_mode_dependencies_instance = None
        self._desktop_run_operations_service = None
        self.notify("startup:config_loaded", path=str(cfg.path))
        try:
            configure_pending_commands_store(getattr(cfg.defaults, "state_path", None))
            set_approval_callback(self._request_command_approval)
        except Exception:
            self.logger.exception("desktop approval runtime init failed")
        if validate_secrets:
            missing_secrets = await self.config_service.validate_required_secrets(cfg)
            if missing_secrets:
                self.notify("startup:missing_secrets", keys=list(missing_secrets))
                raise RuntimeError("missing required secrets: " + ", ".join(missing_secrets))
            self.notify("startup:secrets_validated")
        else:
            # Desktop does not depend on Telegram secrets; keep startup permissive.
            self.notify("startup:secrets_skipped")
        params = await self.config_service.resolve_runtime_params(cfg)
        self.runtime_params = params
        self.notify("startup:runtime_params_ready", workdir=params.workdir, log_path=params.log_path)
        self._ensure_modes_ready()
        await self._ensure_desktop_event_runtime_started()
        self.started = True
        self.notify("startup:ready")
        return params

    def _ensure_modes_ready(self) -> None:
        if self._modes_initialized:
            return
        if not self.mode_registry_service or not getattr(self.mode_registry_service, "registry", None):
            self._modes_initialized = True
            return
        cfg = self.config
        if cfg is None:
            return
        self._register_mode_runtimes_from_plugins(cfg)
        # Ensure ToolRegistry singleton is created early so mode tooling works in Desktop too.
        try:
            bot_app = self._desktop_bot_app()
            _ = getattr(bot_app, "_tool_registry", None)
        except Exception:
            self.logger.exception("desktop tool registry init failed")
        try:
            mode_dependencies = self._desktop_mode_dependencies()
            # Desktop mode runtime services:
            # - messaging/dialogs/callbacks/dirs flows
            # - tasks/session control/pipeline
            # - runtime_by_capability for run_pipeline() execution
            self.mode_registry_service.initialize_plugins(
                config=cfg,
                services={
                    "mode_dependencies": mode_dependencies,
                    "messaging_factory": self._desktop_messaging_factory,
                    "dialogs": self._get_mode_dialogs(),
                    "pipeline": self._desktop_mode_pipeline_service(),
                    "tasks": self._desktop_mode_tasks_service(),
                    "dirs_flow": self._desktop_dirs_flow_service(),
                    "session_control": self._desktop_session_control_service(),
                    "run_artifacts": mode_dependencies.run_artifacts,
                    "run_observability": mode_dependencies.run_observability,
                    "run_doctor": mode_dependencies.run_doctor,
                    "run_boundary_validation": mode_dependencies.run_boundary_validation,
                    "mode_run_lifecycle": mode_dependencies.mode_run_lifecycle,
                    "skill_runtime": mode_dependencies.skill_runtime,
                    "agent_runtime": self._desktop_agent_runtime_service(),
                    "manager_pending": DictStateService(self._manager_resume_pending),
                    "runtime_by_capability": self.get_runtime_by_capability,
                    "tooling": self._desktop_tooling_service(),
                    "ssh": self.ssh_service,
                },
            )
        except Exception:
            self.logger.exception("mode plugins initialization failed")
        self._modes_initialized = True

    def _desktop_lint_evolution_hook(self) -> Optional[Callable[[Any], None]]:
        return make_lint_evolution_hook(getattr(self.config, "lint_evolution", None))

    def _desktop_mode_dependencies(self) -> ModeDependencies:
        if self._desktop_mode_dependencies_instance is not None:
            return self._desktop_mode_dependencies_instance
        if self.mode_registry_service is None:
            raise RuntimeError("mode_registry_service is not configured")
        cfg = self._require_desktop_config()
        foundation = build_mode_foundation_services(cfg)
        self._desktop_mode_dependencies_instance = ModeDependencies(
            session_manager=self.session_service._manager,
            registry=self.mode_registry_service,
            pipeline=self._desktop_mode_pipeline_service(),
            run_artifacts=foundation.run_artifacts,
            run_observability=foundation.run_observability,
            run_doctor=foundation.run_doctor,
            run_boundary_validation=foundation.run_boundary_validation,
            mode_run_lifecycle=foundation.mode_run_lifecycle,
            skill_runtime=foundation.skill_runtime,
            tasks=self._desktop_mode_tasks_service(),
            dialogs=self._get_mode_dialogs(),
            session_control=self._desktop_session_control_service(),
            messaging_factory=self._desktop_messaging_factory,
            agent_runtime=self._desktop_agent_runtime_service(),
            dirs_flow=self._desktop_dirs_flow_service(),
            manager_pending=DictStateService(self._manager_resume_pending),
            runtime_by_capability=self.get_runtime_by_capability,
            tooling=self._desktop_tooling_service(),
        )
        return self._desktop_mode_dependencies_instance

    def _desktop_run_operations(self) -> RunOperationsService:
        if self._desktop_run_operations_service is not None:
            return self._desktop_run_operations_service
        foundation = build_mode_foundation_services(self._require_desktop_config())
        self._desktop_run_operations_service = RunOperationsService(
            enabled=bool(foundation.run_artifacts.is_enabled() and foundation.run_doctor.is_enabled()),
            artifact_store=foundation.run_doctor.artifact_store,
            doctor_service=foundation.run_doctor,
            observability_service=foundation.run_observability,
            recommended_action_executor=self._execute_recommended_run_action,
            logger_=self.logger,
        )
        return self._desktop_run_operations_service

    async def _run_desktop_cli_prompt_with_skill_hook(
        self,
        *,
        session: Any,
        prompt: str,
        source: str,
        image_paths: Optional[List[str]] = None,
        task_bearing: bool = True,
        technical_command: Optional[bool] = None,
    ) -> str:
        cfg = getattr(session, "config", None) or self.config
        if cfg is None:
            cfg = self._require_desktop_config()
        cli_switch = switch_session_active_cli_if_needed(session)
        if cli_switch.switched:
            self._persist_sessions_best_effort(reason="desktop_cli_switch")
        switch_notice = consume_session_cli_switch_notice_text(session)
        if switch_notice:
            self.notify(
                "ui:message",
                session_uid=session_runtime_uid(session),
                role="agent",
                text=switch_notice,
                md2=True,
            )
        hook = get_task_bearing_cli_hook_service(cfg)
        prepared = await hook.prepare_prompt(
            session=session,
            prompt=str(prompt or ""),
            source=str(source or "desktop_direct"),
            phase="execute",
            task_bearing=task_bearing,
            technical_command=technical_command,
        )
        preview_stop_event: Optional[asyncio.Event] = None
        preview_task: Optional[asyncio.Task] = None
        session_uid = session_runtime_uid(session)
        try:
            if assistant_preview_enabled(cfg):
                preview_stop_event = asyncio.Event()

                async def _emit_preview(text: str) -> None:
                    if not text:
                        return
                    self.notify(
                        "ui:assistant_preview",
                        session_uid=session_uid,
                        text=text,
                    )

                preview_task = asyncio.create_task(
                    watch_session_assistant_preview(
                        session,
                        emit_update=_emit_preview,
                        stop_event=preview_stop_event,
                    ),
                    name=f"desktop-assistant-preview:{session_uid}",
                )
            output = await session.run_prompt(str(prepared.prompt_for_cli or ""), image_paths=image_paths or None)
        except asyncio.CancelledError:
            if preview_stop_event is not None:
                preview_stop_event.set()
            if preview_task is not None:
                await asyncio.gather(preview_task, return_exceptions=True)
            self.notify("ui:assistant_preview_clear", session_uid=session_uid)
            raise
        except Exception as exc:
            if preview_stop_event is not None:
                preview_stop_event.set()
            if preview_task is not None:
                await asyncio.gather(preview_task, return_exceptions=True)
            self.notify("ui:assistant_preview_clear", session_uid=session_uid)
            hook.record_error(prepared, error=exc)
            raise
        if preview_stop_event is not None:
            final_preview_text = build_assistant_preview_text(getattr(session, "last_assistant_text_value", None))
            if final_preview_text:
                self.notify("ui:assistant_preview", session_uid=session_uid, text=final_preview_text)
            preview_stop_event.set()
        if preview_task is not None:
            await asyncio.gather(preview_task, return_exceptions=True)
        self.notify("ui:assistant_preview_clear", session_uid=session_uid)
        hook.record_success(prepared, output=output)
        return str(output or "")

    @staticmethod
    def _security_from_runtime_config(bot_app: Any) -> SecurityFacade:
        return SecurityFacade.from_app_config(
            getattr(bot_app, "config", None),
            is_admin_fn=lambda chat_id: bool(bot_app.is_admin(int(chat_id))),
            is_user_fn=lambda chat_id: bool(bot_app.is_user(int(chat_id))),
            system_event_bus=getattr(bot_app, "system_event_bus", None),
        )

    def _desktop_bot_app(self) -> Any:
        """
        Minimal BotApp adapter for modes. This is intentionally small and Desktop-only:
        mode code expects `bot_app.config` and some helper methods for messaging/policy.
        """

        # Reuse a single adapter instance: it carries counters and references used by modes/plugins.
        if self._desktop_bot_app_instance is not None:
            config_changed = getattr(self._desktop_bot_app_instance, "_config_ref", None) is not self.config
            self._desktop_bot_app_instance.config = self.config
            self._desktop_bot_app_instance._config_ref = self.config
            self._desktop_bot_app_instance.mode_registry_service = self.mode_registry_service
            self._desktop_bot_app_instance.mode_run_operations = self._desktop_run_operations()
            self._desktop_bot_app_instance.ssh_service = self.ssh_service
            self._desktop_bot_app_instance.system_event_bus = self._desktop_system_event_bus_instance()
            if config_changed:
                policy = getattr(self._desktop_bot_app_instance, "access_policy_service", None)
                if policy is not None:
                    self._desktop_bot_app_instance.access_policy_service = policy.__class__()
                self._desktop_bot_app_instance.security = self._security_from_runtime_config(
                    self._desktop_bot_app_instance
                )
                try:
                    from modes.sdk.runtime.tooling.registry import get_tool_registry

                    if self.config is not None:
                        self._desktop_bot_app_instance._tool_registry = get_tool_registry(self.config)
                except Exception:
                    self.logger.exception("desktop get_tool_registry refresh failed")
            router = getattr(self._desktop_bot_app_instance, "mode_input_router", None)
            if router is not None:
                router.mode_registry = self.mode_registry_service
                router.dialogs = self._get_mode_dialogs()
                router.send_message = getattr(self._desktop_bot_app_instance, "_send_message", None)
                router.send_output = getattr(self._desktop_bot_app_instance, "send_output", None)
                router.lint_evolution_hook = self._desktop_lint_evolution_hook()
            return self._desktop_bot_app_instance

        facade = self

        class _AccessPolicy:
            def __init__(self) -> None:
                self._adapter_bot_app = self._build_adapter_bot_app()
                self._adapter_bot_app.security = facade._security_from_runtime_config(self._adapter_bot_app)
                self._delegate = AccessPolicyService(self._adapter_bot_app)

            def is_mode_allowed_for_chat(self, chat_id: Any, mode_id: str) -> bool:
                if self._is_desktop_session_chat_id(chat_id):
                    return True
                resolved_chat_id = self._positive_chat_id(chat_id)
                if resolved_chat_id is None:
                    return False
                return bool(self._delegate.is_mode_allowed_for_chat(int(resolved_chat_id), str(mode_id or "")))

            def default_mode_id_for_chat(self, chat_id: Any) -> Optional[str]:
                if self._is_desktop_session_chat_id(chat_id):
                    return None
                resolved_chat_id = self._positive_chat_id(chat_id)
                if resolved_chat_id is None:
                    return None
                return self._delegate.default_mode_id_for_chat(int(resolved_chat_id))

            def _build_adapter_bot_app(self) -> Any:
                facade_config = facade.config
                mode_registry_service = facade.mode_registry_service

                class _AdapterBotApp:
                    @staticmethod
                    def is_admin(chat_id: Any) -> bool:
                        resolved = _AccessPolicy._positive_chat_id(chat_id)
                        if resolved is None:
                            return False
                        return int(resolved) in set(
                            getattr(getattr(facade_config, "telegram", None), "admlist_chat_ids", []) or []
                        )

                    @staticmethod
                    def is_user(chat_id: Any) -> bool:
                        resolved = _AccessPolicy._positive_chat_id(chat_id)
                        if resolved is None:
                            return False
                        if _AdapterBotApp.is_admin(resolved):
                            return False
                        whitelist = set(getattr(getattr(facade_config, "telegram", None), "whitelist_chat_ids", []) or [])
                        if int(resolved) not in whitelist:
                            return False
                        raw_workdirs = (
                            getattr(getattr(facade_config, "telegram", None), "user_workdirs", {}) or {}
                        ).get(int(resolved))
                        if isinstance(raw_workdirs, str):
                            raw_workdirs = [raw_workdirs]
                        for item in raw_workdirs if isinstance(raw_workdirs, list) else []:
                            path = str(item or "").strip()
                            if path and os.path.isdir(os.path.realpath(path)):
                                return True
                        return False

                _AdapterBotApp.config = facade_config
                _AdapterBotApp.mode_registry_service = mode_registry_service
                return _AdapterBotApp()

            @staticmethod
            def _positive_chat_id(value: Any) -> Optional[int]:
                try:
                    resolved = int(value)
                except Exception:
                    return None
                return resolved if resolved > 0 else None

            @staticmethod
            def _is_desktop_session_chat_id(value: Any) -> bool:
                token = str(value or "").strip()
                return token.startswith("desktop:")

        class DesktopBotApp:
            """
            Legacy compatibility shim for mode code that still expects BotApp.

            This adapter is not an extension point. New mode-facing behavior should
            be added to typed SDK services/dependencies instead of growing this
            fake BotApp surface.
            """

            config = facade.config
            mode_registry = getattr(facade.mode_registry_service, "registry", None) if facade.mode_registry_service else None
            access_policy_service = _AccessPolicy()
            ssh_service = facade.ssh_service
            _tool_registry = None

            class _Metrics:
                def __init__(self) -> None:
                    self._counters: Dict[str, int] = {}

                def inc(self, _name: str) -> None:
                    key = str(_name or "").strip()
                    if not key:
                        return None
                    self._counters[key] = int(self._counters.get(key, 0)) + 1
                    return None

                def snapshot(self) -> str:
                    if not self._counters:
                        return "No metrics yet."
                    parts = [f"{name}: {count}" for name, count in sorted(self._counters.items())]
                    return "\n".join(parts)

            def __init__(self) -> None:
                self._config_ref = facade.config
                self.ui_state = ChatUiState()
                self.metrics = self._Metrics()
                from desktop.services.pending_input_ui import DesktopPendingInputUiAdapter
                self.pending_input_ui = DesktopPendingInputUiAdapter(facade, self)
                self.input_dispatch_service = InputDispatchService(self, pending_input_ui=self.pending_input_ui)
                self.system_event_bus = facade._desktop_system_event_bus_instance()
                self.mode_input_router = ModeInputRoutingService(
                    mode_registry=facade.mode_registry_service,
                    dialogs=facade._get_mode_dialogs(),
                    send_message=getattr(self, "_send_message", None),
                    send_output=getattr(self, "send_output", None),
                    lint_evolution_hook=facade._desktop_lint_evolution_hook(),
                )

            def is_admin(self, _session_uid: str) -> bool:
                return True

            def is_user(self, _session_uid: str) -> bool:
                return True

            def is_allowed(self, _session_uid: str) -> bool:
                return True

            async def run_prompt(self, session: Any, text: str, dest: dict, context: Any) -> str:
                image_paths: Optional[List[str]] = None
                if isinstance(dest, dict):
                    raw_many = dest.get("image_paths")
                    raw_one = dest.get("image_path")
                    if isinstance(raw_many, list) and raw_many:
                        image_paths = [str(p) for p in raw_many if str(p)]
                    elif raw_one:
                        image_paths = [str(raw_one)]
                _ = context
                return await facade._run_desktop_cli_prompt_with_skill_hook(
                    session=session,
                    prompt=str(text or ""),
                    source="desktop_bot_adapter",
                    image_paths=image_paths,
                )

            async def _handle_cli_input(
                self,
                session: Any,
                text: str,
                chat_id: Any,
                context: Any,
                *,
                dest: Optional[dict] = None,
            ) -> None:
                _ = context
                image_paths: Optional[List[str]] = None
                payload = dict(dest or {})
                raw_many = payload.get("image_paths")
                raw_one = payload.get("image_path")
                if isinstance(raw_many, list) and raw_many:
                    image_paths = [str(p) for p in raw_many if str(p)]
                elif raw_one:
                    image_paths = [str(raw_one)]
                output = await facade._run_desktop_cli_prompt_with_skill_hook(
                    session=session,
                    prompt=str(text or ""),
                    source="desktop_input_dispatch",
                    image_paths=image_paths,
                )
                if str(output or "").strip():
                    facade.notify(
                        "ui:message",
                        session_id=str(chat_id or session_runtime_uid(session)),
                        role="agent",
                        text=str(output),
                        md2=True,
                    )

            def _mode_allows_plugin_ui(self, session: Any) -> bool:
                svc = facade.mode_registry_service
                if not svc:
                    return False
                return bool(svc.allows_agent_plugin_ui(session))

            def notify(self, event: str, **payload: Any) -> None:
                """Прокси к DesktopFacade.notify — позволяет режимам публиковать UI-события."""
                facade.notify(event, **payload)

            def get_runtime_by_capability(self, capability: str) -> Any:
                return facade.get_runtime_by_capability(str(capability or ""))

            @staticmethod
            def _parse_progress_text(text: str) -> tuple[str, int]:
                """Из текста «🧠 Аналитик: <phase>\n⏱ M:SS» извлекает phase и elapsed_seconds."""
                phase = "Анализ"
                elapsed = 0
                if "⏱" in text:
                    parts = text.split("⏱")
                    if len(parts) >= 2:
                        ts = parts[1].strip()
                        # M:SS или MM:SS
                        m = 0
                        s = 0
                        if ":" in ts:
                            try:
                                m, s = map(int, ts.split(":", 1))
                            except ValueError:
                                pass
                        elapsed = m * 60 + s
                if "🧠 Аналитик:" in text:
                    start = text.index("🧠 Аналитик:") + len("🧠 Аналитик:")
                    end = text.index("⏱") if "⏱" in text else len(text)
                    phase = text[start:end].strip()
                return phase, elapsed

            async def _send_message(self, _context: Any, *, chat_id: Any, text: str, md2: bool = True, **_kwargs: Any) -> Any:
                session = facade.session_service.get_session_by_uid(str(chat_id))
                if not session:
                    return None
                # Перехватываем progress-сообщения (аналитик шлёт их через _send_message)
                if "🧠 Аналитик:" in text and "⏱" in text:
                    phase, elapsed = self._parse_progress_text(text)
                    facade.notify(
                        "ui:analyst_progress",
                        session_id=str(chat_id),
                        phase=phase,
                        elapsed_seconds=elapsed,
                    )
                    facade._desktop_message_id += 1
                    return type("DesktopMessageRef", (), {"message_id": facade._desktop_message_id})()
                reply_markup = _kwargs.get("reply_markup")
                rows = facade._extract_inline_keyboard(reply_markup)
                if rows:
                    facade.notify(
                        "ui:mode_menu",
                        session_id=str(chat_id),
                        text=str(text),
                        rows=rows,
                    )
                else:
                    facade.notify("ui:message", session_id=str(chat_id), role="agent", text=str(text), md2=bool(md2))
                facade._desktop_message_id += 1
                return type("DesktopMessageRef", (), {"message_id": facade._desktop_message_id})()

            async def _edit_message(
                self,
                _context: Any,
                *,
                session_uid: Optional[str] = None,
                chat_id: Any = None,
                message_id: int,
                text: str,
                md2: bool = True,
                **_kwargs: Any,
            ) -> Any:
                session_token = str(session_uid or chat_id or "").strip()
                if not session_token:
                    return None
                # Только progress-тексты маршрутизируем в ui:analyst_progress.
                if "🧠 Аналитик:" in text and "⏱" in text:
                    phase, elapsed = self._parse_progress_text(text)
                    facade.notify(
                        "ui:analyst_progress",
                        session_id=session_token,
                        phase=phase,
                        elapsed_seconds=elapsed,
                    )
                    return type("DesktopMessageRef", (), {"message_id": message_id})()
                # Desktop не поддерживает in-place edits — делегируем на _send_message (как раньше).
                return await self._send_message(_context, chat_id=session_token, text=text, md2=md2, **_kwargs)

            async def _delete_message(
                self,
                _context: Any,
                *,
                session_uid: Optional[str] = None,
                chat_id: Any = None,
                message_id: int,
                **_kwargs: Any,
            ) -> bool:
                _ = message_id, _kwargs
                session_token = str(session_uid or chat_id or "").strip()
                if not session_token:
                    return False
                facade.notify("ui:analyst_progress_clear", session_id=session_token)
                return True

            async def _clear_message_reply_markup(
                self,
                _context: Any,
                *,
                session_uid: Optional[str] = None,
                chat_id: Any = None,
                message_id: int,
                dest: Optional[dict] = None,
            ) -> bool:
                _ = message_id, dest
                session_token = str(session_uid or chat_id or "").strip()
                if not session_token:
                    return False
                facade.notify("ui:mode_menu", session_id=session_token, text="", rows=[])
                return True

            async def _send_document(self, _context: Any, *, chat_id: Any, document: Any, **_kwargs: Any) -> bool:
                """
                Telegram modes sometimes try to send a file. For Desktop, persist it under workdir
                so user can open it manually.
                """
                session = facade.session_service.get_session_by_uid(str(chat_id))
                if not session:
                    return False
                name = getattr(document, "name", None) or "document.bin"
                try:
                    base = getattr(facade.runtime_params, "workdir", None) or session.workdir
                    out_dir = cli_proxy_artifact_path(str(base), ".desktop_downloads")
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, os.path.basename(str(name)))
                    with open(out_path, "wb") as f:
                        f.write(document.read())
                    facade.notify("ui:message", session_id=str(chat_id), role="agent", text=f"Файл сохранен: {out_path}", md2=True)
                    return True
                except Exception:
                    facade.logger.exception("desktop _send_document failed")
                    return False

            async def send_output(
                self,
                session: Any,
                dest: dict,
                output: str,
                context: Any,
                *,
                send_header: bool = True,
                header_override: Optional[str] = None,
                force_html: bool = False,
            ) -> None:
                """Соответствует интерфейсу bot_app.send_output (session, dest, output, context)."""
                if not session:
                    return
                facade.notify("ui:message", session_uid=session_runtime_uid(session), role="agent", text=str(output), md2=True)

            async def _send_ask_question(
                self,
                _context: Any,
                chat_id: Any,
                session_id: str,
                question_id: str,
                question: str,
                options: list[str],
                allow_custom: bool = True,
                system_options: bool = True,
            ) -> None:
                """Уведомляет UI о необходимости уточнения у пользователя."""
                _ = system_options
                normalized_options = [str(opt).strip() for opt in (options or []) if str(opt).strip()]
                qid = str(question_id or "").strip()
                session_uid = str(chat_id or "").strip()
                sid = str(session_id or "").strip() or session_uid
                if qid:
                    self.ui_state.pending_questions[qid] = {
                        "question_id": qid,
                        "question": str(question),
                        "options": normalized_options,
                        "chat_id": session_uid,
                        "session_uid": session_uid,
                        "session_id": sid,
                        "allow_custom": bool(allow_custom),
                        "created_at": time.time(),
                    }
                    self.ui_state.active_ask_question_by_chat[session_uid] = qid
                facade.notify(
                    "ui:ask_question",
                    chat_id=session_uid,
                    session_uid=session_uid,
                    session_id=sid,
                    question_id=qid,
                    question=str(question),
                    options=normalized_options,
                    allow_custom=bool(allow_custom),
                )

            def _clear_pending_question(self, question_id: str) -> bool:
                return facade._clear_pending_question(str(question_id or ""))

        import os

        bot_app = DesktopBotApp()
        bot_app.mode_run_operations = self._desktop_run_operations()
        # Bind ToolRegistry singleton (used by agent plugin UI and some modes).
        try:
            from modes.sdk.runtime.tooling.registry import get_tool_registry

            if facade.config is not None:
                bot_app._tool_registry = get_tool_registry(facade.config)
        except Exception:
            facade.logger.exception("desktop get_tool_registry failed")

        self._desktop_bot_app_instance = bot_app
        return bot_app

    def _desktop_messaging_factory(self, context: Any) -> MessagingService:
        bot_app = self._desktop_bot_app()
        return MessagingService(
            send_message=bot_app._send_message,
            edit_message=bot_app._edit_message,
            delete_message=bot_app._delete_message,
            send_document=bot_app._send_document,
            transport_context=context,
        )

    def _desktop_tool_registry(self) -> Any:
        bot_app = self._desktop_bot_app()
        registry = getattr(bot_app, "_tool_registry", None)
        if registry is None:
            raise RuntimeError("desktop tool registry is not initialized")
        return registry

    def _desktop_tooling_service(self) -> ModeToolingService:
        async def _execute(tool_name: str, args: dict, tool_ctx: dict) -> dict:
            registry = self._desktop_tool_registry()
            return await registry.execute(str(tool_name or ""), dict(args or {}), dict(tool_ctx or {}))

        return ModeToolingService(
            execute_tool_fn=_execute,
            registry_provider=self._desktop_tool_registry,
        )

    def _build_desktop_session_overview(self, session_uid: str) -> tuple[str, list[list[dict[str, str]]]]:
        """
        Сборка обзора сессии (предыдущий уровень меню) для Desktop.
        Возвращает (text, rows) в формате для ui:mode_menu.
        """
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return "Сессия не выбрана.", [[{"text": "❌ Отмена", "data": "sess_close_menu"}]]
        active_mode = str(get_active_mode(session, "") or "").strip()
        rows: list[list[dict[str, str]]] = []
        access_policy = getattr(self._desktop_bot_app(), "access_policy_service", None)
        chat_id = getattr(session, "chat_id", None)
        modes: list[tuple[str, str]] = []
        if self.mode_registry_service:
            modes = list(self.mode_registry_service.list_modes())
            checker = getattr(access_policy, "is_mode_allowed_for_chat", None) if access_policy is not None else None
            if callable(checker) and chat_id is not None:
                modes = [
                    (mode_id, label)
                    for mode_id, label in modes
                    if bool(checker(chat_id, mode_id))
                ]
        visibility = build_session_overview_visibility(
            session=session,
            chat_id=chat_id if chat_id is not None else 0,
            access_policy=access_policy,
            available_tool_count=0,
            registered_mode_count=len(modes),
            visible_session_count=1,
        )
        if visibility.allows("mode_selector"):
            row: list[dict[str, str]] = []
            for mode_id, label in modes:
                prefix = "●" if (active_mode == mode_id) else "○"
                row.append({"text": f"{prefix} {label}", "data": f"sess_mode:{mode_id}"})
                if len(row) >= 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        if visibility.allows("orchestrator"):
            orch_enabled = is_orchestrator_enabled(session, False)
            orch_text = f"🧠 {'Выключить' if orch_enabled else 'Включить'} оркестратор"
            rows.append([{"text": orch_text, "data": f"sess_orch_toggle:{session_runtime_uid(session)}"}])
        rows.append([{"text": "❌ Отмена", "data": "sess_close_menu"}])
        text = f"Сессия {session_runtime_uid(session)}"
        if getattr(session, "name", None):
            text += f" — {session.name}"
        return text, rows

    def _build_desktop_mode_menu(
        self,
        *,
        plugin: Any,
        session: Any,
        mode_id: str,
        back_callback: str,
        back_text: str = "⬅️ Назад",
    ) -> tuple[str, Any]:
        menu_visibility = build_mode_menu_visibility(
            session=session,
            mode_id=mode_id,
            access_policy=getattr(self._desktop_bot_app(), "access_policy_service", None),
        )
        return call_mode_build_menu(
            plugin,
            session,
            back_callback=back_callback,
            back_text=back_text,
            menu_visibility=menu_visibility,
        )

    @staticmethod
    def _extract_inline_keyboard(reply_markup: Any) -> list[list[dict[str, str]]]:
        """
        Convert Telegram InlineKeyboardMarkup-like object into a neutral structure for Desktop UI.
        Returns rows: [[{"text": "...", "data": "callback_data"}], ...]
        """
        if reply_markup is None:
            return []
        try:
            rows = getattr(reply_markup, "inline_keyboard", None)
        except Exception:
            rows = None
        if not isinstance(rows, (list, tuple)):
            return []
        out: list[list[dict[str, str]]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            row_out: list[dict[str, str]] = []
            for btn in row:
                try:
                    text = str(getattr(btn, "text", "") or "").strip()
                    data = str(getattr(btn, "callback_data", "") or "").strip()
                except Exception:
                    continue
                if text and data:
                    row_out.append({"text": text, "data": data})
            if row_out:
                out.append(row_out)
        return out

    def _get_mode_dialogs(self) -> DialogService:
        if self._mode_dialogs is None:
            self._mode_dialogs = DialogService(
                pending_questions_provider=lambda: self._desktop_bot_app().ui_state.pending_questions,
            )
        return self._mode_dialogs

    def _desktop_mode_pipeline_service(self) -> ModePipelineService:
        async def _run(session: Any, prompt: str, dest: dict, context: Any, mode_id: str) -> None:
            # Run pipeline via mode.run_pipeline, then deliver output to UI if requested.
            self._ensure_modes_ready()
            if not self.mode_registry_service:
                return
            mode = self.mode_registry_service.get(str(mode_id))
            if mode is None or not hasattr(mode, "run_pipeline"):
                return
            reset_mode_id = None
            if hasattr(mode, "pre_run_reset_mode_id"):
                reset_mode_id = str(mode.pre_run_reset_mode_id() or "").strip() or None
            if reset_mode_id:
                was_reset = self._mode_pre_run_reset.apply(
                    session=session,
                    mode_id=reset_mode_id,
                    clear_runtime_cache=self._clear_mode_runtime_cache,
                    clear_pending_questions=lambda sid: self._clear_pending_questions(session_uid=sid),
                )
                if was_reset:
                    try:
                        self.session_service._manager._persist_sessions()  # type: ignore[attr-defined]
                    except Exception:
                        self.logger.exception(
                            "desktop mode pre-run persist failed session_uid=%s mode_id=%s",
                            str(getattr(session, "id", "") or ""),
                            str(mode_id or ""),
                        )
            out = await mode.run_pipeline(
                session=session,
                user_text=str(prompt or ""),
                bot_app=self._desktop_bot_app(),
                context=context,
                dest=dict(dest or {}),
            )
            try:
                set_orchestrator_last_mode_output(session, str(out or ""))
                set_orchestrator_last_mode_id(session, str(mode_id or ""))
            except Exception:
                self.logger.exception(
                    "desktop failed to capture orchestrator mode output session=%s mode=%s",
                    getattr(session, "id", ""),
                    str(mode_id or ""),
                )
            if getattr(mode, "framework_sends_output", None) and callable(mode.framework_sends_output):
                if bool(mode.framework_sends_output()):
                    self.notify(
                        "ui:message",
                        session_uid=session_runtime_uid(session),
                        role="agent",
                        text=str(out or ""),
                        md2=True,
                    )

        return ModePipelineService(run_mode_pipeline_fn=_run)

    def _desktop_dirs_flow_service(self) -> DirsFlowService:
        async def _start(session_uid: str, context: Any, root: str, mode_token: str) -> None:
            self._dirs_mode_token_by_chat[session_uid] = str(mode_token or "")
            self.notify("ui:dirs_flow:start", chat_id=session_uid, root=str(root), mode_token=str(mode_token or ""))

        def _clear(session_uid: str, mode_id: str, flow: str) -> None:
            cid = session_uid
            mode_raw = str(self._dirs_mode_token_by_chat.get(cid, "") or "")
            token_mode_id, token_flow = decode_mode_dirs(mode_raw)
            expected_mode = str(mode_id or "").strip()
            expected_flow = str(flow or "").strip()
            if expected_mode and token_mode_id != expected_mode:
                return
            if expected_flow and token_flow != expected_flow:
                return
            self._dirs_mode_token_by_chat.pop(cid, None)

        def _get_token(session_uid: str, _message_thread_id: int | None = None) -> str:
            return str(self._dirs_mode_token_by_chat.get(session_uid, "") or "")

        return DirsFlowService(start_flow_fn=_start, clear_flow_fn=_clear, get_mode_token_fn=_get_token)

    def _desktop_mode_tasks_service(self) -> Any:
        """
        Adapter that matches modes.sdk.services.tasks.TaskService API but delegates to app TaskService,
        so tasks become visible for Desktop UI (Working/Idle) and cancellation.
        """

        facade = self

        class _Tasks:
            @staticmethod
            def _resolve_session_uid(*, session_uid: Optional[str] = None, session_id: Optional[str] = None) -> str:
                value = session_uid if session_uid not in (None, "") else session_id
                return str(value or "")

            def _group(self, session_uid: str, mode_id: str) -> tuple[str, str]:
                return (str(session_uid), str(mode_id))

            def _prune(self, group: tuple[str, str]) -> None:
                ids = list(facade._mode_task_ids.get(group, []))
                alive = {rec.task_id for rec in facade.task_service.list_active(session_id=group[0])}
                kept = [tid for tid in ids if tid in alive]
                if kept:
                    facade._mode_task_ids[group] = kept
                else:
                    facade._mode_task_ids.pop(group, None)

            def create(
                self,
                *,
                session_uid: Optional[str] = None,
                session_id: Optional[str] = None,
                mode_id: str,
                coro: Awaitable[Any],
                name: str,
            ) -> asyncio.Task:
                resolved_session_uid = self._resolve_session_uid(session_uid=session_uid, session_id=session_id)
                group = self._group(resolved_session_uid, mode_id)
                self._prune(group)

                async def _runner(_token) -> Any:
                    return await coro

                rec = facade.task_service.create(
                    name=f"mode:{mode_id}:{name}",
                    session_id=resolved_session_uid,
                    runner=_runner,
                )
                facade._mode_task_ids.setdefault(group, []).append(rec.task_id)
                facade._mode_task_names[rec.task_id] = str(name)
                facade.notify(
                    "task:started",
                    task_id=rec.task_id,
                    session_id=resolved_session_uid,
                    name=f"mode:{mode_id}:{name}",
                )

                def _on_done(t: asyncio.Task) -> None:
                    payload = {
                        "task_id": rec.task_id,
                        "session_id": resolved_session_uid,
                        "name": f"mode:{mode_id}:{name}",
                    }
                    if t.cancelled():
                        facade.notify("task:cancelled", **payload)
                        return
                    try:
                        err = t.exception()
                    except asyncio.CancelledError:
                        facade.notify("task:cancelled", **payload)
                        return
                    except Exception:
                        facade.logger.exception(
                            "desktop mode task telemetry inspection failed task_id=%s session_uid=%s name=%s",
                            rec.task_id,
                            resolved_session_uid,
                            f"mode:{mode_id}:{name}",
                        )
                        facade.notify("task:failed", **payload, error="failed_to_inspect_task_result")
                        return
                    if err is None:
                        facade.notify("task:completed", **payload)
                        return
                    facade.notify("task:failed", **payload, error=str(err))

                rec.task.add_done_callback(_on_done)
                return rec.task

            def list(
                self,
                *,
                session_uid: Optional[str] = None,
                session_id: Optional[str] = None,
                mode_id: str,
            ) -> List[str]:
                group = self._group(self._resolve_session_uid(session_uid=session_uid, session_id=session_id), mode_id)
                self._prune(group)
                out: List[str] = []
                for tid in facade._mode_task_ids.get(group, []):
                    name = facade._mode_task_names.get(tid, "task")
                    out.append(str(name))
                return out

            async def cancel_all(
                self,
                *,
                session_uid: Optional[str] = None,
                session_id: Optional[str] = None,
                mode_id: str,
                timeout_s: float = 1.0,
            ) -> int:
                group = self._group(self._resolve_session_uid(session_uid=session_uid, session_id=session_id), mode_id)
                self._prune(group)
                ids = list(facade._mode_task_ids.get(group, []))
                if not ids:
                    return 0
                count = 0
                for tid in ids:
                    ok = await facade.task_service.cancel(tid, reason=f"mode:{mode_id}:cancelled", timeout_s=float(timeout_s))
                    if ok:
                        count += 1
                self._prune(group)
                return count

            async def cancel_session(self, *, session_uid: str, timeout_s: float = 1.0) -> int:
                return int(await facade.task_service.cancel_session(session_uid, timeout_s=float(timeout_s)))

        return _Tasks()

    def _desktop_session_control_service(self) -> SessionControlService:
        def _persist() -> Any:
            try:
                self.session_service._manager._persist_sessions()  # type: ignore[attr-defined]
            except Exception:
                self.logger.exception("desktop persist sessions failed")
            return None

        async def _cancel_mode(session_uid: str, mode_id: str, timeout_s: float) -> int:
            tasks = self._desktop_mode_tasks_service()
            return int(await tasks.cancel_all(session_id=session_uid, mode_id=str(mode_id), timeout_s=float(timeout_s)))

        async def _cancel_session(session_uid: str, timeout_s: float) -> int:
            return int(await self.task_service.cancel_session(session_uid, timeout_s=float(timeout_s)))

        return SessionControlService(
            persist_sessions=_persist,
            cancel_mode_tasks=_cancel_mode,
            cancel_session_tasks=_cancel_session,
        )

    def _desktop_agent_runtime_service(self) -> AgentRuntimeService:
        def _interrupt(session_uid: str, _session_uid: str, _context: Any) -> None:
            session = self.session_service.get_session_by_uid(session_uid)
            if session:
                try:
                    session.interrupt()
                except Exception:
                    self.logger.exception("agent_runtime interrupt failed session_uid=%s", session_uid)

        def _clear_sandbox(_chat_id: Optional[int] = None) -> tuple[int, int]:
            try:
                sandbox = self._desktop_agent_sandbox_service()
                removed, errors = sandbox.clear(chat_id=_chat_id)
                return int(removed), int(errors)
            except Exception:
                self.logger.exception("desktop clear_sandbox failed")
                return (0, 1)

        def _clear_session_files(_session_uid: str) -> bool:
            try:
                sandbox = self._desktop_agent_sandbox_service()
                return bool(sandbox.clear_session(str(_session_uid)))
            except Exception:
                self.logger.exception("desktop clear_session_files failed session_uid=%s", _session_uid)
                return False

        def _clear_session_cache(_session_uid: str) -> None:
            try:
                self._clear_mode_runtime_cache(str(_session_uid))
            except Exception:
                self.logger.exception("desktop clear_session_cache failed session_uid=%s", _session_uid)

        def _get_session(_session_uid: str) -> Any:
            return self.session_service.get_session_by_uid(_session_uid)

        def _get_session_by_uid(_session_uid: str, _chat_id: Optional[int] = None) -> Any:
            return self.session_service.get_session_by_uid(_session_uid)

        return AgentRuntimeService(
            interrupt_session_fn=_interrupt,
            clear_sandbox_fn=_clear_sandbox,
            clear_session_files_fn=_clear_session_files,
            clear_session_cache_fn=_clear_session_cache,
            get_session_fn=_get_session,
            get_session_by_uid_fn=_get_session_by_uid,
        )

    def _desktop_callback_router(self) -> ModeCallbackRouterService:
        if self._mode_callback_router is not None:
            return self._mode_callback_router
        if not self.mode_registry_service:
            raise RuntimeError("mode_registry_service is not configured")

        async def _send_message(context: Any, **kwargs: Any) -> Any:
            bot_app = self._desktop_bot_app()
            extra = {key: value for key, value in kwargs.items() if key not in {"chat_id", "text", "md2"}}
            return await bot_app._send_message(
                context,
                chat_id=kwargs.get("chat_id"),
                text=str(kwargs.get("text") or ""),
                md2=bool(kwargs.get("md2", True)),
                **extra,
            )

        def _get_session(session_uid: str) -> Any:
            return self.session_service.get_session_by_uid(session_uid)

        def _get_dirs_mode(session_uid: str, _message_thread_id: int | None = None) -> str:
            return str(self._dirs_mode_token_by_chat.get(session_uid, "") or "")

        def _clear_dirs_mode(session_uid: str, _message_thread_id: int | None = None) -> None:
            self._dirs_mode_token_by_chat.pop(session_uid, None)

        self._mode_callback_router = ModeCallbackRouterService(
            mode_registry=self.mode_registry_service,
            dialogs=self._get_mode_dialogs(),
            send_message=_send_message,
            get_session=_get_session,
            get_dirs_mode_token=_get_dirs_mode,
            clear_dirs_mode_token=_clear_dirs_mode,
        )
        return self._mode_callback_router

    async def _notify_next_pending_busy_input(self, *, session_uid: str) -> None:
        bot_app = self._desktop_bot_app()
        pending = InputDispatchService.pending_head(self._desktop_pending_map(), session_uid)
        if not pending:
            return
        dispatch = getattr(bot_app, "input_dispatch_service", None)
        action = str(getattr(pending, "action", "") or InputDispatchService.PENDING_ACTION_QUEUE_CHOICE)
        if dispatch is None or not hasattr(dispatch, "send_pending_input_decision"):
            return
        try:
            await dispatch.send_pending_input_decision(
                context=object(),
                decision=dispatch.pending_input_decision_for_action(action, pending_input=pending),
                dest=getattr(pending, "dest", None),
                chat_id=session_uid,
                ui_key=session_uid,
            )
        except Exception:
            self.logger.exception("desktop notify next pending input failed chat_id=%s", session_uid)

    def _desktop_pending_map(self) -> Dict[Any, Any]:
        bot_app = self._desktop_bot_app()
        pending_map = bot_app.ui_state.pending
        if isinstance(pending_map, dict):
            return pending_map
        self.logger.warning(
            "desktop pending store has invalid type=%s; resetting to empty dict",
            type(pending_map).__name__,
        )
        fixed: Dict[int, Any] = {}
        try:
            bot_app.ui_state.pending = fixed
        except Exception:
            self.logger.exception("desktop failed to reset pending store")
        return fixed

    def _persist_sessions_best_effort(self, *, reason: str) -> None:
        if not self._session_mutations.persist_all():
            self.logger.warning("desktop persist sessions returned false: %s", str(reason or "unknown"))

    def _schedule_queue_kick(self, *, session_uid: str) -> None:
        sid = str(session_uid or "").strip()
        if not sid:
            return
        current = self._queue_kick_tasks.get(sid)
        if current is not None and not current.done():
            return

        async def _run() -> None:
            try:
                await self._kick_session_queue_if_idle(session_uid=session_uid)
            except Exception:
                self.logger.exception("desktop queue kick failed session_uid=%s", sid)
            finally:
                self._queue_kick_tasks.pop(sid, None)

        self._queue_kick_tasks[sid] = asyncio.create_task(_run())

    async def _kick_session_queue_if_idle(self, *, session_uid: str) -> None:
        while True:
            session = self.session_service.get_session_by_uid(session_uid)
            if not session:
                return
            if InputDispatchService._is_session_running(session, self._desktop_bot_app()):
                return
            queue_ref = getattr(session, "queue", None)
            if not queue_ref:
                await self._notify_next_pending_busy_input(session_uid=session_uid)
                return

            if hasattr(queue_ref, "__getitem__"):
                raw_next_item = queue_ref[0]
            else:
                return
            next_item = normalize_queue_item(
                raw_next_item,
                fallback_dest={"kind": "desktop", "session_uid": str(session_uid)},
            )
            if hasattr(queue_ref, "popleft"):
                queue_ref.popleft()
            elif isinstance(queue_ref, list):
                queue_ref.pop(0)
            else:
                return
            self._persist_sessions_best_effort(reason="queue_kick.pop")

            next_prompt = str(next_item.text or "")
            image_paths = []
            if isinstance(raw_next_item, dict):
                raw_many = (raw_next_item or {}).get("image_paths")
                raw_one = (raw_next_item or {}).get("image_path")
                if isinstance(raw_many, list):
                    image_paths = [str(x) for x in raw_many if str(x)]
                elif raw_one:
                    image_paths = [str(raw_one)]
            prepared = PreparedAttachments(image_paths=list(image_paths), meta=[]) if image_paths else None
            try:
                await self.run_session_input(
                    session_uid,
                    next_prompt,
                    prepared_attachments=prepared,
                )
            except Exception:
                self.logger.exception("desktop queued input dispatch failed session_uid=%s", session.id)
                try:
                    restore_queue = getattr(session, "queue", None)
                    if hasattr(restore_queue, "appendleft"):
                        restore_queue.appendleft(raw_next_item)
                    elif isinstance(restore_queue, list):
                        restore_queue.insert(0, raw_next_item)
                    self._persist_sessions_best_effort(reason="queue_kick.restore")
                except Exception:
                    self.logger.exception("desktop queued input restore failed session_uid=%s", session.id)
                return

    async def handle_mode_callback(self, session_uid: str, *, data: str) -> bool:
        """
        Desktop entrypoint for mode callbacks (Telegram-style callback_data).
        """
        self._ensure_modes_ready()
        sdata = str(data or "").strip()
        if sdata.startswith("approve_cmd:"):
            cmd_id = sdata.split(":", 1)[1].strip()
            waiter_active = has_pending_command_waiter(cmd_id)
            pending = approve_pending_command(cmd_id)
            if not pending:
                session = self.session_service.get_session_by_uid(session_uid)
                if session:
                    self.notify("ui:message", session_id=session_uid, role="agent", text="Запрос уже обработан.", md2=True)
                return True
            session = self.session_service.get_session_by_uid(str(pending.session_id))
            if waiter_active:
                target = session or self.session_service.get_session_by_uid(session_uid)
                if target:
                    self.notify(
                        "ui:message",
                        session_uid=target.id,
                        role="agent",
                        text="Одобрено. Продолжаю выполнение шага...",
                        md2=True,
                    )
                return True
            if session:
                self.notify("ui:message", session_id=session_uid, role="agent", text="Одобрено. Выполняю команду...", md2=True)
            result = await execute_shell_command(str(pending.command), str(pending.cwd))
            output = result.get("output") if bool(result.get("success")) else result.get("error")
            text = str(output or "(пустой вывод)")
            target = session or self.session_service.get_session_by_uid(session_uid)
            if target:
                self.notify("ui:message", session_uid=target.id, role="agent", text=text, md2=True)
            return True
        if sdata.startswith("deny_cmd:"):
            cmd_id = sdata.split(":", 1)[1].strip()
            waiter_active = has_pending_command_waiter(cmd_id)
            pending = deny_pending_command(cmd_id)
            if not pending:
                session = self.session_service.get_session_by_uid(session_uid)
                if session:
                    self.notify("ui:message", session_id=session_uid, role="agent", text="Запрос уже обработан.", md2=True)
                return True
            session = None
            if pending:
                session = self.session_service.get_session_by_uid(str(pending.session_id))
            if session is None:
                session = self.session_service.get_session_by_uid(session_uid)
            if session:
                text = "Команда отклонена. Продолжаю без неё." if waiter_active else "Команда отклонена."
                self.notify("ui:message", session_id=session_uid, role="agent", text=text, md2=True)
            return True
        if sdata in {"cancel_current", "queue_input", "discard_input", "take_pending_input", "queue_append_pending"}:
            pending_map = self._desktop_pending_map()
            pending = InputDispatchService.pending_head(pending_map, session_uid)
            if not pending:
                session = self.session_service.get_session_by_uid(session_uid)
                if session:
                    self.notify("ui:message", session_id=session_uid, role="agent", text="Нет ожидающего ввода.", md2=True)
                return True
            pending_session_uid = str(
                getattr(pending, "session_uid", "") or getattr(pending, "session_id", "") or ""
            ).strip()
            session = self.session_service.get_session_by_uid(pending_session_uid)
            if not session:
                active = self.session_service.get_session_by_uid(session_uid)
                if active:
                    self.notify("ui:message", session_uid=active.id, role="agent", text="Сессия уже закрыта.", md2=True)
                dispatch = getattr(self._desktop_bot_app(), "input_dispatch_service", None)
                if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                    dispatch.clear_pending_prompt_record(session_uid)
                InputDispatchService.pop_pending(pending_map, session_uid)
                await self._notify_next_pending_busy_input(session_uid=session_uid)
                return True
            dispatch = getattr(self._desktop_bot_app(), "input_dispatch_service", None)
            if sdata == "cancel_current":
                if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                    dispatch.clear_pending_prompt_record(session_uid)
                InputDispatchService.pop_pending(pending_map, session_uid)
                message_text = "Текущая генерация прервана. Ввод отброшен."
                try:
                    report = await self._desktop_session_interrupt_service().interrupt_session_runtime(
                        session,
                        owner_chat_id=None,
                        reply_chat_id=None,
                        message_thread_id=None,
                        reason="desktop_cancel_current",
                    )
                except Exception:
                    self.logger.exception("desktop cancel_current interrupt failed session_uid=%s", session.id)
                else:
                    if str(report.status or "") == "completed":
                        message_text = "Текущая генерация прервана. Сессия освобождена. Ввод отброшен."
                    elif str(report.status or "") == "partial_timeout":
                        message_text = "Текущая генерация прервана, но часть runtime еще завершает остановку. Ввод отброшен."
                    else:
                        message_text = "Не удалось полностью прервать сессию. Ввод отброшен."
                self.notify("ui:message", session_id=session_uid, role="agent", text=message_text, md2=True)
                await self._notify_next_pending_busy_input(session_uid=session_uid)
                return True
            if sdata == "take_pending_input":
                if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                    dispatch.clear_pending_prompt_record(session_uid)
                InputDispatchService.pop_pending(pending_map, session_uid)
                if InputDispatchService._is_session_busy(session, bot_app=self._desktop_bot_app()):
                    self.notify(
                        "ui:message",
                        session_id=session_uid,
                        role="agent",
                        text="Сессия занята. Переношу ввод в очередь.",
                        md2=True,
                    )
                    if dispatch is not None and hasattr(dispatch, "_handle_busy_pending_input"):
                        await dispatch._handle_busy_pending_input(
                            session=session,
                            pending_input=pending,
                            chat_id=session_uid,
                            context=object(),
                        )
                    return True
                self.notify("ui:message", session_id=session_uid, role="agent", text="Взято в работу.", md2=True)
                prepared = None
                image_paths = list(getattr(pending, "image_paths", []) or [])
                if image_paths:
                    prepared = PreparedAttachments(image_paths=list(image_paths), meta=[])
                await self.run_session_input(
                    session_uid,
                    str(getattr(pending, "text", "") or ""),
                    prepared_attachments=prepared,
                )
                return True
            if sdata == "queue_append_pending":
                if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                    dispatch.clear_pending_prompt_record(session_uid)
                InputDispatchService.pop_pending(pending_map, session_uid)
                try:
                    ok = InputDispatchService.append_pending_to_queue_tail(session, pending)
                    if not ok:
                        raise RuntimeError("queue append rejected")
                    self._persist_sessions_best_effort(reason="queue_append_pending")
                except Exception:
                    self.logger.exception("desktop queue_append_pending failed session_uid=%s", session.id)
                    self.notify(
                        "ui:message",
                        session_id=session_uid,
                        role="agent",
                        text="Не удалось обновить сообщение в очереди. Попробуйте еще раз.",
                        md2=True,
                    )
                    return True
                self.notify(
                    "ui:message",
                    session_id=session_uid,
                    role="agent",
                    text="Ввод добавлен к текущему сообщению в очереди.",
                    md2=True,
                )
                await self._notify_next_pending_busy_input(session_uid=session_uid)
                self._schedule_queue_kick(session_uid=session_uid)
                return True
            if sdata == "queue_input":
                if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                    dispatch.clear_pending_prompt_record(session_uid)
                item = InputDispatchService.queue_item_from_pending(pending)
                if not InputDispatchService.append_queue_item(session, item):
                    raise RuntimeError("queue append rejected")
                InputDispatchService.pop_pending(pending_map, session_uid)
                self._persist_sessions_best_effort(reason="queue_input")
                self.notify("ui:message", session_id=session_uid, role="agent", text="Ввод поставлен в очередь.", md2=True)
                await self._notify_next_pending_busy_input(session_uid=session_uid)
                self._schedule_queue_kick(session_uid=session_uid)
                return True
            if dispatch is not None and hasattr(dispatch, "clear_pending_prompt_record"):
                dispatch.clear_pending_prompt_record(session_uid)
            InputDispatchService.pop_pending(pending_map, session_uid)
            self.notify("ui:message", session_id=session_uid, role="agent", text="Ввод отменен.", md2=True)
            await self._notify_next_pending_busy_input(session_uid=session_uid)
            return True
        # sess_active — вернуться к предыдущему уровню (обзор сессии)
        if sdata == "sess_active":
            session = self.session_service.get_session_by_uid(session_uid)
            if session:
                text, rows = self._build_desktop_session_overview(session_uid)
                self.notify("ui:mode_menu", session_id=session_uid, text=text, rows=rows)
            return True
        if sdata.startswith("sess_active_pick:"):
            target_session_uid = sdata.split(":", 1)[1].strip()
            session = self.session_service.get_session_by_uid(target_session_uid)
            if session:
                text, rows = self._build_desktop_session_overview(target_session_uid)
                self.notify("ui:mode_menu", session_id=target_session_uid, text=text, rows=rows)
            return True
        # sess_close_menu — закрыть меню
        if sdata == "sess_close_menu":
            session = self.session_service.get_session_by_uid(session_uid)
            if session:
                self.notify("ui:mode_menu", session_id=session_uid, text="", rows=[])
            return True
        # sess_mode:X — открыть меню режима X (переход в меню)
        if sdata.startswith("sess_mode:"):
            mode_id = sdata.split(":", 1)[1].strip() if ":" in sdata else ""
            session = self.session_service.get_session_by_uid(session_uid)
            if session and mode_id and self.mode_registry_service:
                plugin = self.mode_registry_service.get(mode_id)
                if plugin and hasattr(plugin, "build_menu"):
                    try:
                        text, keyboard = self._build_desktop_mode_menu(
                            plugin=plugin,
                            session=session,
                            mode_id=mode_id,
                            back_callback=build_session_overview_callback_data(session),
                        )
                        rows = self._extract_inline_keyboard(keyboard)
                        self.notify("ui:mode_menu", session_id=session_uid, text=str(text or ""), rows=rows)
                    except Exception:
                        self.logger.exception("show_mode_menu sess_mode failed mode=%s", mode_id)
            return True
        # sess_mode_pick:<session_uid>[:mode_id] — активировать сессию и открыть меню режима
        if sdata.startswith("sess_mode_pick:"):
            payload = sdata.split(":", 1)[1].strip()
            session_uid = payload
            explicit_mode_id = ""
            session = self.session_service.get_session_by_uid(session_uid) if session_uid else None
            if session is None and ":" in payload:
                candidate_uid, candidate_mode_id = payload.rsplit(":", 1)
                candidate_uid = candidate_uid.strip()
                candidate_mode_id = candidate_mode_id.strip()
                candidate_session = self.session_service.get_session_by_uid(candidate_uid) if candidate_uid else None
                if candidate_session is not None:
                    session_uid = candidate_uid
                    explicit_mode_id = candidate_mode_id
                    session = candidate_session
            if not session:
                self.notify("ui:message", session_uid=session_uid, role="agent", text="Сессия не найдена.", md2=True)
                return True
            mode_id = explicit_mode_id or str(get_active_mode(session, "") or "").strip()
            if session and mode_id and self.mode_registry_service:
                plugin = self.mode_registry_service.get(mode_id)
                if plugin and hasattr(plugin, "build_menu"):
                    try:
                        text, keyboard = self._build_desktop_mode_menu(
                            plugin=plugin,
                            session=session,
                            mode_id=mode_id,
                            back_callback=build_session_overview_callback_data(session),
                        )
                        rows = self._extract_inline_keyboard(keyboard)
                        self.notify("ui:mode_menu", session_id=session_uid, text=str(text or ""), rows=rows)
                    except Exception:
                        self.logger.exception("show_mode_menu sess_mode_pick failed mode=%s", mode_id)
                return True
            text, rows = self._build_desktop_session_overview(session_uid)
            self.notify("ui:mode_menu", session_id=session_uid, text=text, rows=rows)
            return True
        if sdata.startswith("sess_orch_toggle:"):
            session_uid = sdata.split(":", 1)[1].strip()
            session = self.session_service.get_session_by_uid(session_uid)
            if not session:
                return True
            set_orchestrator_enabled(session, not is_orchestrator_enabled(session, False))
            set_orchestrator_pending_input(session, None)
            try:
                self.session_service._manager._persist_sessions()  # type: ignore[attr-defined]
            except Exception:
                self.logger.exception("desktop persist orchestrator toggle failed")
            text, rows = self._build_desktop_session_overview(session_uid)
            self.notify("ui:mode_menu", session_id=session_uid, text=text, rows=rows)
            return True
        if sdata.startswith("orch_transition:"):
            parts = sdata.split(":")
            if len(parts) < 3:
                return True
            action = str(parts[1] or "").strip().lower()
            if action == "apply":
                if len(parts) < 4:
                    return True
                target_mode_id = str(parts[-1] or "").strip()
                session_uid = ":".join(parts[2:-1]).strip()
            else:
                target_mode_id = ""
                session_uid = ":".join(parts[2:]).strip()
            session = self.session_service.get_session_by_uid(session_uid)
            if not session:
                return True
            pending = get_orchestrator_pending_input(session, None)
            if not isinstance(pending, dict):
                return True
            payload_text = str(pending.get("text") or "")
            payload_prepared = pending.get("prepared_attachments")
            payload_attachments = pending.get("attachments")
            expected_target = str(pending.get("target_mode_id") or "").strip()
            disable_on_cancel = bool(pending.get("disable_orchestrator_on_cancel"))
            set_orchestrator_pending_input(session, None)

            if action == "apply":
                if target_mode_id and target_mode_id == expected_target:
                    self.advanced_orchestrator_service.apply_mode(session=session, target_mode_id=target_mode_id)
                    try:
                        self.session_service._manager._persist_sessions()  # type: ignore[attr-defined]
                    except Exception:
                        self.logger.exception("desktop persist orchestrator apply failed")
            elif action == "cancel":
                if disable_on_cancel:
                    set_orchestrator_enabled(session, False)
                    try:
                        self.session_service._manager._persist_sessions()  # type: ignore[attr-defined]
                    except Exception:
                        self.logger.exception("desktop persist orchestrator disable failed")
                    self.notify(
                        "ui:message",
                        session_id=session_uid,
                        role="agent",
                        text="Процесс остановлен пользователем. Продвинутый оркестратор выключен.",
                        md2=True,
                    )
                    return True
            else:
                return True

            await self.run_session_input(
                session_uid,
                payload_text,
                attachments=payload_attachments if isinstance(payload_attachments, list) else None,
                prepared_attachments=payload_prepared,
                skip_orchestrator=True,
            )
            return True
        # Agent plugins and some mode UIs use Telegram-style plugin callbacks:
        # dlg_cancel:/dlg:/cb:... are handled by plugin DialogMixin.
        if sdata.startswith(("cb:", "dlg:", "dlg_cancel:")):
            try:
                dispatched = await self._dispatch_plugin_callback(session_uid, data=sdata)
                if dispatched:
                    return True
            except Exception:
                self.logger.exception("desktop plugin callback dispatch failed data=%s", data)
        if not self.mode_registry_service:
            return False
        router = self._desktop_callback_router()
        return bool(
            await router.handle_mode_action_callback(
                data=sdata,
                chat_id=session_uid,
                query=None,
                context=None,
                bot_app=self._desktop_bot_app(),
            )
        )

    async def handle_dirs_flow_event(self, session_uid: str, *, event: str, path: str) -> Optional[Any]:
        self._ensure_modes_ready()
        router = self._desktop_callback_router()
        result = await router.dispatch_dirs_event(
            chat_id=session_uid,
            context=None,
            event=str(event or ""),
            path=str(path or ""),
            bot_app=self._desktop_bot_app(),
        )
        # If handler returns output, deliver it to UI.
        if result is not None and getattr(result, "output", None):
            session = self.session_service.get_session_by_uid(session_uid)
            if session:
                self.notify("ui:message", session_id=session_uid, role="agent", text=str(result.output), md2=True)
        return result

    async def handle_dialog_message(self, session_uid: str, *, text: str) -> Optional[str]:
        """
        If a mode dialog is active for (chat_id, session_uid, active_mode),
        route the message to it and return ToolResult.output if any.
        """
        session = self.session_service.get_session_by_uid(session_uid)
        if not session or not self.mode_registry_service:
            return None
        # First: plugin dialogs (DialogMixin awaiting_input) intercept messages regardless of mode dialogs.
        handled_plugin = await self._dispatch_plugin_message(session_uid, str(text or ""))
        if handled_plugin:
            return ""
        mode_id = str(get_active_mode(session, "") or "").strip()
        if not mode_id:
            return None
        # For Desktop, use int hash of session_uid as chat_id for dialog routing
        try:
            dialog_chat_id = int(session_uid)
        except (ValueError, TypeError):
            dialog_chat_id = hash(str(session_uid)) % 1000000
        dialogs = self._get_mode_dialogs()
        if not dialogs.is_active(chat_id=dialog_chat_id, session_id=session_runtime_uid(session), mode_id=mode_id):
            return None
        from modes.sdk.models import MessageModel

        result = await dialogs.route_message(
            MessageModel(text=str(text or ""), chat_id=dialog_chat_id, user_id=None, message_id=None),
            {
                "bot_app": self._desktop_bot_app(),
                "session": session,
                "chat_id": session_uid,
                "context": object(),  # placeholder для ask_user
                "query": None,
                "mode_id": mode_id,
                "dest": {"kind": "desktop", "chat_id": session_uid},
            },
            session_id=session_uid,
            mode_id=mode_id,
        )
        out = str(getattr(result, "output", "") or "").strip()
        return out or None

    async def _dispatch_plugin_callback(self, session_uid: str, *, data: str) -> bool:
        """
        Dispatch cb:/dlg:/dlg_cancel: callbacks to an agent plugin DialogMixin handler.
        This mirrors Telegram wiring where plugins register a single CallbackQueryHandler.
        """
        bot_app = self._desktop_bot_app()
        registry = getattr(bot_app, "_tool_registry", None)
        if registry is None:
            return False

        sdata = str(data or "")
        parts = sdata.split(":", 3)
        if len(parts) < 2:
            return False
        pid = str(parts[1] or "").strip()
        if not pid:
            return False

        # Gate by mode: plugin UI is only allowed when active mode allows it.
        session = self.session_service.get_session_by_uid(session_uid)
        if session is None or not bot_app._mode_allows_plugin_ui(session):
            try:
                await bot_app._send_message(None, chat_id=session_uid, text="Режим плагинов не активен.", md2=True)
            except Exception:
                self.logger.exception(
                    "desktop plugin ui callback failed to send inactive-mode message session_uid=%s",
                    session_uid,
                )
            return True

        plugin = None
        try:
            plugins = getattr(registry, "plugins", {}) or {}
            for _name, inst in dict(plugins).items():
                try:
                    get_pid = getattr(inst, "get_plugin_id", None)
                    inst_pid = str(get_pid() if callable(get_pid) else getattr(inst, "plugin_id", "")).strip()
                except Exception:
                    continue
                if inst_pid == pid:
                    plugin = inst
                    break
        except Exception:
            plugin = None
        if plugin is None:
            return False

        handler = getattr(plugin, "_dispatch_callback", None)
        if not callable(handler):
            return False

        update, context = self._build_desktop_callback_update(session_uid=session_uid, data=sdata)
        res = handler(update, context)
        if asyncio.iscoroutine(res):
            await res
        return True

    async def _dispatch_plugin_message(self, session_uid: str, text: str) -> bool:
        """
        If any plugin has an active dialog (awaiting_input), route the message to it.
        """
        bot_app = self._desktop_bot_app()
        registry = getattr(bot_app, "_tool_registry", None)
        if registry is None:
            return False
        session = self.session_service.get_session_by_uid(session_uid)
        if session is None or not bot_app._mode_allows_plugin_ui(session):
            return False
        try:
            plugins = getattr(registry, "plugins", {}) or {}
        except Exception:
            return False
        for _name, plugin in dict(plugins).items():
            awaiting = getattr(plugin, "awaiting_input", None)
            if not callable(awaiting):
                continue
            try:
                if not bool(awaiting(session_uid)):
                    continue
            except Exception:
                continue
            handler = getattr(plugin, "handle_message", None)
            if not callable(handler):
                continue
            update, context = self._build_desktop_message_update(session_uid=session_uid, text=str(text or ""))
            res = handler(update, context)
            if asyncio.iscoroutine(res):
                await res
            return True
        return False

    def _build_desktop_callback_update(self, *, session_uid: str, data: str) -> tuple[Any, Any]:
        bot_app = self._desktop_bot_app()

        class _Chat:
            def __init__(self, cid: Any) -> None:
                self.id = cid

        class _Message:
            def __init__(self, cid: Any) -> None:
                self.chat_id = cid
                self.chat = _Chat(cid)
                self.message_id = 0

            async def reply_text(self, text: str, **kwargs: Any) -> Any:
                return await bot_app._send_message(
                    None,
                    chat_id=self.chat_id,
                    text=str(text),
                    md2=bool(kwargs.get("md2", True)),
                    **kwargs,
                )

            async def edit_text(self, text: str, **kwargs: Any) -> Any:
                return await bot_app._edit_message(
                    None,
                    session_uid=self.chat_id,
                    message_id=int(self.message_id),
                    text=str(text),
                    md2=bool(kwargs.get("md2", True)),
                    **kwargs,
                )

        class _CallbackQuery:
            def __init__(self, cid: Any, cbd: str) -> None:
                self.data = str(cbd or "")
                self.message = _Message(cid)

            async def answer(self, *_a: Any, **_kw: Any) -> None:
                return None

        class _Update:
            def __init__(self, cid: Any, cbd: str) -> None:
                self.callback_query = _CallbackQuery(cid, cbd)
                self.effective_chat = _Chat(cid)

        return _Update(session_uid, str(data or "")), None

    def _build_desktop_message_update(self, *, session_uid: str, text: str) -> tuple[Any, Any]:
        bot_app = self._desktop_bot_app()

        class _Chat:
            def __init__(self, cid: Any) -> None:
                self.id = cid

        class _Message:
            def __init__(self, cid: Any, t: str) -> None:
                self.chat_id = cid
                self.chat = _Chat(cid)
                self.text = str(t or "")
                self.message_id = 0

            async def reply_text(self, text: str, **kwargs: Any) -> Any:
                return await bot_app._send_message(
                    None,
                    chat_id=self.chat_id,
                    text=str(text),
                    md2=bool(kwargs.get("md2", True)),
                    **kwargs,
                )

        class _Update:
            def __init__(self, cid: Any, t: str) -> None:
                self.message = _Message(cid, t)
                self.effective_chat = _Chat(cid)

        return _Update(session_uid, str(text or "")), None

    async def show_mode_menu(self, *args: Any) -> bool:
        """
        Ask the active mode plugin to build its menu and deliver it to Desktop UI via ui:mode_menu.
        """
        session_uid = self._resolve_desktop_session_uid(*args, api_name="show_mode_menu")
        self._ensure_modes_ready()
        session = self.session_service.get_session_by_uid(session_uid)
        if not session or not self.mode_registry_service:
            return False
        mode_id = str(get_active_mode(session, "") or "").strip()
        if not mode_id:
            return False
        plugin = self.mode_registry_service.get(mode_id)
        if plugin is None or not hasattr(plugin, "build_menu"):
            return False
        try:
            text, keyboard = self._build_desktop_mode_menu(
                plugin=plugin,
                session=session,
                mode_id=mode_id,
                back_callback=build_session_overview_callback_data(session),
            )
            rows = self._extract_inline_keyboard(keyboard)
            self.notify("ui:mode_menu", session_id=session_uid, text=str(text or ""), rows=rows)
            return True
        except Exception:
            self.logger.exception("show_mode_menu failed mode=%s session_uid=%s", mode_id, session.id)
            return False

    def register_mode_runtime(self, mode_id: str, runtime: Any) -> None:
        mid = str(mode_id or "").strip()
        if not mid:
            return
        self._mode_runtime_registry[mid] = runtime

    def _request_command_approval(self, session_uid: str, cmd_id: str, cmd: str, reason: str) -> None:
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return
        self.notify(
            "ui:mode_menu",
            session_id=session_uid,
            text=f"Нужное подтверждение: {str(reason or 'Dangerous')}\nКоманда:\n{str(cmd or '')}",
            rows=[
                [
                    {"text": "✅ Одобрить", "data": f"approve_cmd:{cmd_id}"},
                    {"text": "❌ Запретить", "data": f"deny_cmd:{cmd_id}"},
                ]
            ],
        )

    def _register_mode_runtimes_from_plugins(self, cfg: Any) -> None:
        reg = getattr(self.mode_registry_service, "registry", None) if self.mode_registry_service else None
        if reg is None:
            return
        try:
            mode_ids = list(reg.list_ids() or [])
        except Exception:
            return
        for mode_id in mode_ids:
            try:
                plugin = reg.get(mode_id)
            except Exception:
                plugin = None
            if plugin is None or not hasattr(plugin, "build_runtime"):
                continue
            try:
                runtime = plugin.build_runtime(cfg)
            except Exception as e:
                self.logger.exception("mode runtime init failed mode=%s err=%s", mode_id, e)
                continue
            if runtime is not None:
                self.register_mode_runtime(mode_id, runtime)

    def iter_mode_runtimes(self) -> list[Any]:
        return list(self._mode_runtime_registry.values())

    def get_runtime_by_capability(self, capability: str) -> Any:
        cap = str(capability or "").strip()
        if not cap:
            return None
        for runtime in self.iter_mode_runtimes():
            try:
                supports = getattr(runtime, "supports_capability", None)
                if callable(supports) and bool(supports(cap)):
                    return runtime
                caps = getattr(runtime, "capabilities", None)
                if isinstance(caps, (set, frozenset, list, tuple)):
                    if cap in {str(x).strip() for x in caps}:
                        return runtime
            except Exception:
                continue
        return None

    async def prepare_attachments(self, session_uid: str, attachments: List[str]) -> PreparedAttachments:
        """
        Validate and copy attachments into session temp_dir (defaults.image_temp_dir under session.workdir).

        Phase 1 output:
        - image_paths: stored local paths for images (pass into Session.run_prompt(image_paths=...))
        - meta: structured description suitable for persisting in chat history.
        """
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            raise ValueError(f"unknown session: {session_uid}")
        cfg = self.config
        if cfg is None:
            raise RuntimeError("facade config is not loaded")

        base_dir = str(getattr(cfg.defaults, "image_temp_dir", ".cli-proxy/.attachments") or ".cli-proxy/.attachments")
        if os.path.isabs(base_dir):
            temp_dir = base_dir
        else:
            temp_dir = os.path.join(str(session.workdir), base_dir)
        os.makedirs(temp_dir, exist_ok=True)
        self._cleanup_attachments_dir(temp_dir)

        max_mb = int(getattr(cfg.defaults, "image_max_mb", 10) or 10)
        max_bytes = max_mb * 1024 * 1024
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

        out_images: List[str] = []
        meta: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in attachments or []:
            src = str(raw or "").strip()
            if not src:
                continue
            if src in seen:
                continue
            seen.add(src)

            try:
                st = os.stat(src)
                size = int(st.st_size)
            except FileNotFoundError:
                meta.append({"kind": "missing", "original_path": src, "error": "not_found"})
                continue
            except Exception as e:
                meta.append({"kind": "invalid", "original_path": src, "error": str(e)})
                continue
            if not os.path.isfile(src):
                meta.append({"kind": "invalid", "original_path": src, "error": "not_a_file"})
                continue
            if size > max_bytes:
                meta.append({"kind": "invalid", "original_path": src, "error": f"too_large>{max_mb}mb", "size_bytes": size})
                continue

            name = os.path.basename(src) or "attachment.bin"
            ext = os.path.splitext(name)[1].lower()
            kind = "image" if ext in image_exts else "file"

            stamp = time.strftime("%Y%m%d_%H%M%S")
            stored_path = self._unique_copy_path(temp_dir, f"{stamp}_{name}")
            try:
                shutil.copy2(src, stored_path)
            except Exception as e:
                meta.append({"kind": kind, "original_path": src, "name": name, "error": f"copy_failed:{e}"})
                continue

            rel = None
            try:
                rel = os.path.relpath(stored_path, str(session.workdir))
            except Exception:
                rel = None

            item: Dict[str, Any] = {
                "kind": kind,
                "name": name,
                "ext": ext,
                "size_bytes": size,
                "original_path": src,
                "stored_path": stored_path,
            }
            if rel:
                item["stored_rel"] = rel
            meta.append(item)

            if kind == "image":
                out_images.append(stored_path)

        return PreparedAttachments(image_paths=out_images, meta=meta)

    @staticmethod
    def _unique_copy_path(dest_dir: str, filename: str) -> str:
        base = os.path.basename(str(filename or "attachment.bin"))
        root, ext = os.path.splitext(base)
        candidate = os.path.join(dest_dir, base)
        if not os.path.exists(candidate):
            return candidate
        for i in range(1, 1000):
            alt = os.path.join(dest_dir, f"{root}_{i}{ext}")
            if not os.path.exists(alt):
                return alt
        return os.path.join(dest_dir, f"{root}_{int(time.time())}{ext}")

    @staticmethod
    def _cleanup_attachments_dir(path: str) -> None:
        cutoff = time.time() - 24 * 60 * 60
        try:
            for name in os.listdir(path):
                full = os.path.join(path, name)
                try:
                    if not os.path.isfile(full):
                        continue
                    st = os.stat(full)
                    if float(getattr(st, "st_mtime", 0.0) or 0.0) < cutoff:
                        os.unlink(full)
                except Exception:
                    continue
        except Exception:
            return

    async def _maybe_offer_post_run_transition_desktop(
        self,
        *,
        session: Any,
        mode_id: str,
        output_text: str,
        session_uid: str,
        attachments: Optional[List[str]] = None,
        prepared_attachments: Optional[PreparedAttachments] = None,
    ) -> bool:
        if not is_orchestrator_enabled(session, False):
            return False
        if get_orchestrator_pending_input(session, None):
            return False
        if str(mode_id or "").strip() == "agent":
            return False

        proposal = None
        try:
            proposal = await self.advanced_orchestrator_service.propose_transition_hybrid(
                session=session,
                text=str(output_text or ""),
                mode_registry=self.mode_registry_service,
                app_config=self.config,
                llm_router_fn=self.orchestrator_chat_completion,
            )
        except Exception:
            self.logger.exception("desktop post-run orchestrator proposal failed")
            return False
        if proposal is None:
            return False
        previous_mode_id = str(getattr(session, "_orchestrator_prev_mode_id", "") or "").strip()
        is_return_to_previous = bool(
            previous_mode_id and str(proposal.target_mode_id or "").strip() == previous_mode_id
        )

        handoff_text = self.advanced_orchestrator_service.build_handoff_input(
            session=session,
            original_user_text=str(output_text or ""),
        )
        set_orchestrator_pending_input(session, {
            "text": handoff_text,
            "attachments": list(attachments or []),
            "prepared_attachments": prepared_attachments,
            "target_mode_id": str(proposal.target_mode_id),
            "disable_orchestrator_on_cancel": True,
        })
        current_label = self.advanced_orchestrator_service.current_mode_label(
            session=session,
            mode_registry=self.mode_registry_service,
        )
        text = "Результат текущего режима готов.\n" + self.advanced_orchestrator_service.build_confirm_text(
            current_mode_label=current_label,
            proposal=proposal,
        )
        if is_return_to_previous:
            text += (
                "\n\n⚠️ Предложен возврат в предыдущий режим цепочки. "
                "Проверьте уверенность и подтвердите вручную."
            )
        self.notify(
            "ui:mode_menu",
            session_id=session_uid,
            text=text,
            rows=[
                [
                    {
                        "text": "✅ Передать дальше",
                        "data": f"orch_transition:apply:{session_uid}:{proposal.target_mode_id}",
                    },
                    {
                        "text": "⛔ Остановить процесс",
                        "data": f"orch_transition:cancel:{session_uid}",
                    },
                ]
            ],
        )
        return True

    async def try_queue_busy_input(
        self,
        session_uid: str,
        text: str,
        *,
        prepared_attachments: Optional[PreparedAttachments] = None,
    ) -> bool:
        """Ставит busy-ввод в очередь или показывает queue-choice, если сессия занята."""
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            raise ValueError(f"unknown session: {session_uid}")
        if not InputDispatchService._is_session_busy(session, bot_app=self._desktop_bot_app()):
            return False

        image_paths = list(getattr(prepared_attachments, "image_paths", []) or [])
        bot_app = self._desktop_bot_app()
        dest = {"kind": "desktop", "session_id": session_uid, "chat_id": session_uid}
        dispatch = getattr(bot_app, "input_dispatch_service", None)
        if dispatch is None:
            return False
        pending = dispatch._build_pending_input(
            session=session,
            text=str(text or ""),
            chat_id=session_uid,
            dest=dest,
            image_path=None,
            image_paths=list(image_paths) if image_paths else None,
            action=InputDispatchService.PENDING_ACTION_QUEUE_CHOICE,
        )
        await dispatch._handle_busy_pending_input(
            session=session,
            pending_input=pending,
            chat_id=session_uid,
            context=object(),
        )
        return True

    async def stage_session_input(
        self,
        session_uid: str,
        text: str,
        *,
        prepared_attachments: Optional[PreparedAttachments] = None,
    ) -> None:
        """Ставит desktop-ввод в общий confirm/queue flow вместо немедленного запуска."""
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            raise ValueError(f"unknown session: {session_uid}")

        bot_app = self._desktop_bot_app()
        dispatch = getattr(bot_app, "input_dispatch_service", None)
        if dispatch is None:
            raise RuntimeError("desktop input dispatch service is unavailable")

        image_paths = list(getattr(prepared_attachments, "image_paths", []) or [])
        await dispatch.stage_user_input(
            session,
            str(text or ""),
            session_uid,
            object(),
            dest={"kind": "desktop", "session_uid": session_uid, "chat_id": session_uid},
            image_paths=list(image_paths) if image_paths else None,
        )

    async def run_session_input(
        self,
        *args: Any,
        attachments: Optional[List[str]] = None,
        prepared_attachments: Optional[PreparedAttachments] = None,
        skip_orchestrator: bool = False,
    ) -> str:
        """
        Основной метод выполнения ввода пользователя.

        - Если в сессии включен active_mode: запускает mode pipeline (mode.run_pipeline).
        - Иначе: вызывает session.run_prompt.
        - Генерирует события task:started/task:completed (и task:failed при ошибке).
        """
        session_uid, text = self._parse_run_session_input_args(*args)
        route_chat_id = session_uid

        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            raise ValueError(f"unknown session: {session_uid}")
        if (
            not skip_orchestrator
            and is_orchestrator_enabled(session, False)
            and not get_orchestrator_pending_input(session, None)
        ):
            proposal = None
            try:
                proposal = await self.advanced_orchestrator_service.propose_transition_hybrid(
                    session=session,
                    text=str(text or ""),
                    mode_registry=self.mode_registry_service,
                    app_config=self.config,
                    llm_router_fn=self.orchestrator_chat_completion,
                )
            except Exception:
                self.logger.exception("desktop hybrid orchestrator proposal failed")
            if proposal is not None:
                handoff_text = self.advanced_orchestrator_service.build_handoff_input(
                    session=session,
                    original_user_text=str(text or ""),
                )
                set_orchestrator_pending_input(session, {
                    "text": handoff_text,
                    "attachments": list(attachments or []),
                    "prepared_attachments": prepared_attachments,
                    "target_mode_id": str(proposal.target_mode_id),
                    "disable_orchestrator_on_cancel": False,
                })
                current_label = self.advanced_orchestrator_service.current_mode_label(
                    session=session,
                    mode_registry=self.mode_registry_service,
                )
                rows = [
                    [
                        {
                            "text": "✅ Перейти",
                            "data": f"orch_transition:apply:{session_uid}:{proposal.target_mode_id}",
                        },
                        {
                            "text": "⛔ Отменить",
                            "data": f"orch_transition:cancel:{session_uid}",
                        },
                    ]
                ]
                self.notify(
                    "ui:mode_menu",
                    session_id=session_uid,
                    text=self.advanced_orchestrator_service.build_confirm_text(
                        current_mode_label=current_label,
                        proposal=proposal,
                    ),
                    rows=rows,
                )
                return ""

        name = "run_session_input"
        desktop_dest = {"kind": "desktop", "session_id": session_uid, "chat_id": route_chat_id}

        async def _runner(token) -> str:
            # If caller cancels the task, make sure underlying CLI processes stop too.
            async def _watch_cancel() -> None:
                try:
                    await token.wait_cancelled()
                    try:
                        session.interrupt()
                    except Exception:
                        self.logger.exception("session interrupt failed session_uid=%s", session.id)
                except Exception:
                    # Best-effort: cancellation watcher must never crash the runner.
                    return None

            watch = asyncio.create_task(_watch_cancel())
            busy_marked = False
            try:
                prepared = prepared_attachments
                if prepared is None and attachments:
                    prepared = await self.prepare_attachments(session_uid, list(attachments))
                image_paths = list(getattr(prepared, "image_paths", []) or [])
                mode_id = str(get_active_mode(session, "") or "").strip()
                if mode_id and self.mode_registry_service:
                    self._ensure_modes_ready()
                    bot_app = self._desktop_bot_app()
                    captured: Dict[str, str] = {"output": ""}

                    async def _send_output(
                        _session: Any,
                        _dest: dict,
                        output: str,
                        _context: Any,
                        *,
                        send_header: bool = True,
                        header_override: Optional[str] = None,
                        force_html: bool = False,
                    ) -> None:
                        _ = send_header
                        _ = header_override
                        _ = force_html
                        text_out = str(output or "")
                        if not text_out:
                            return
                        captured["output"] = text_out
                        set_orchestrator_last_mode_output(session, text_out)
                        set_orchestrator_last_mode_id(session, str(mode_id or ""))

                    async def _cli_fallback(_session: Any, prompt: str, _session_uid: str, _context: Any) -> None:
                        if not self._is_desktop_direct_cli_allowed(session=session):
                            self._notify_desktop_direct_cli_denied(session_uid=session_uid)
                            return
                        out = await self._run_desktop_cli_prompt_with_skill_hook(
                            session=session,
                            prompt=str(prompt or ""),
                            source="desktop_mode_fallback",
                            image_paths=image_paths,
                        )
                        captured["output"] = out

                    router = ModeInputRoutingService(
                        mode_registry=self.mode_registry_service,
                        dialogs=self._get_mode_dialogs(),
                        send_message=getattr(bot_app, "_send_message", None),
                        send_output=_send_output,
                        lint_evolution_hook=self._desktop_lint_evolution_hook(),
                    )
                    mode_result = await router.route_mode_or_cli(
                        bot_app=bot_app,
                        session=session,
                        text=str(text or ""),
                        chat_id=route_chat_id,
                        context=object(),
                        dest=dict(desktop_dest),
                        user_id=None,
                        cli_fallback=_cli_fallback,
                    )
                    await self._bridge_mode_result_v2(
                        session_uid=session_runtime_uid(session),
                        result=mode_result,
                        fallback_step_id=str(mode_id or "desktop-mode"),
                    )
                    return str(captured.get("output") or "")
                # Unknown mode or missing registry: fallback to CLI prompt.
                if not self._is_desktop_direct_cli_allowed(session=session):
                    self._notify_desktop_direct_cli_denied(session_uid=session_uid)
                    return ""
                session.busy = True
                busy_marked = True
                return await self._run_desktop_cli_prompt_with_skill_hook(
                    session=session,
                    prompt=str(text or ""),
                    source="desktop_direct",
                    image_paths=image_paths,
                )
            except asyncio.CancelledError:
                # Ensure subprocess/pexpect gets interrupted on cancellation.
                try:
                    session.interrupt()
                except Exception:
                    self.logger.exception("session interrupt failed on cancel session_uid=%s", session.id)
                raise
            finally:
                if busy_marked:
                    session.busy = False
                watch.cancel()
                self._schedule_queue_kick(session_uid=session_uid)

        rec = self.task_service.create(
            name=name,
            session_id=session_uid,
            runner=_runner,
        )
        self.notify("task:started", task_id=rec.task_id, session_id=session_uid, name=name)
        try:
            result = await rec.task
            result_text = str(result or "")
            self.notify("task:completed", task_id=rec.task_id, session_id=session_uid, name=name)
            if result_text.strip():
                self.notify("ui:message", session_id=session_uid, role="agent", text=result_text, md2=True)
            result_mode_id = str(get_active_mode(session, "") or "").strip()
            await self._maybe_offer_post_run_transition_desktop(
                session=session,
                mode_id=result_mode_id,
                output_text=result_text,
                session_uid=session_uid,
                attachments=attachments,
                prepared_attachments=prepared_attachments,
            )
            return result_text
        except asyncio.CancelledError:
            # Do not propagate cancellation outside: Desktop UI expects a clean return.
            self.notify(
                "task:cancelled",
                task_id=rec.task_id,
                session_id=session_uid,
                name=name,
                reason=rec.token.reason or "cancelled",
            )
            return ""
        except Exception as e:
            self.notify("task:failed", task_id=rec.task_id, session_id=session_uid, name=name, error=str(e))
            raise

    async def run_background(
        self,
        *,
        session_uid: str,
        name: str,
        runner: Callable[[Any], Awaitable[Any]],
    ) -> str:
        rec = self.session_service.start_background_task(session_id=session_uid, name=name, runner=runner)
        self.notify("task:started", task_id=rec.task_id, session_id=session_uid, name=name)

        async def _watch() -> None:
            try:
                await rec.task
                self.notify("task:completed", task_id=rec.task_id, session_id=session_uid, name=name)
            except asyncio.CancelledError:
                self.notify(
                    "task:cancelled",
                    task_id=rec.task_id,
                    session_id=session_uid,
                    name=name,
                    reason=rec.token.reason or "cancelled",
                )
            except Exception as e:
                self.notify("task:failed", task_id=rec.task_id, session_id=session_uid, name=name, error=str(e))

        self.task_service.create(name=f"watch:{name}", session_id=session_uid, runner=lambda _token: _watch())
        return rec.task_id

    def get_plugin_ui(self, allowed_tools: Optional[List[str]] = None) -> Dict[str, Any]:
        """Получает информацию о плагинах для UI."""
        bot_app = self._desktop_bot_app()
        registry = getattr(bot_app, "_tool_registry", None)
        if registry is None:
            return {}
        try:
            return registry.build_bot_ui(allowed_tools if allowed_tools is not None else ["All"])
        except Exception:
            self.logger.exception("get_plugin_ui failed")
            return {}

    async def reload(self) -> AppRuntimeParams:
        """Перезагружает конфигурацию и переинициализирует компоненты."""
        self._modes_initialized = False
        self._desktop_mode_dependencies_instance = None
        self._desktop_run_operations_service = None
        self._mode_runtime_registry.clear()
        return await self.start()

    async def shutdown(self) -> None:
        self.notify("shutdown:begin")
        if self._shutdown_in_progress:
            self.started = False
            self.notify("shutdown:done")
            return

        self._shutdown_in_progress = True
        self.started = False
        bot_app = self._desktop_bot_app_instance
        if bot_app is not None:
            try:
                setattr(bot_app, "_shutdown_in_progress", True)
            except Exception:
                self.logger.exception("failed to set desktop bot shutdown flag")

        try:
            mode_launch_adapter = self._desktop_mode_launch_adapter
            if mode_launch_adapter is not None:
                try:
                    await mode_launch_adapter.stop()
                except Exception:
                    self.logger.exception("desktop mode launch adapter stop failed")

            scheduler_service = self._desktop_scheduler_service
            if scheduler_service is not None:
                try:
                    await scheduler_service.stop()
                except Exception:
                    self.logger.exception("desktop scheduler stop failed")
                finally:
                    self._desktop_scheduler_started_instance = None

            event_bus = self._desktop_system_event_bus
            if event_bus is not None:
                try:
                    await event_bus.shutdown()
                except Exception:
                    self.logger.exception("desktop system event bus shutdown failed")

            for rec in list(self.task_service.list_active()):
                try:
                    await self.task_service.cancel(
                        str(getattr(rec, "task_id", "")),
                        reason="app_shutdown",
                        timeout_s=0.5,
                    )
                except Exception:
                    self.logger.exception(
                        "task cancellation failed on shutdown task_id=%s",
                        getattr(rec, "task_id", None),
                    )

            manager = getattr(self.session_service, "_manager", None)
            sessions_by_chat = getattr(manager, "sessions_by_chat", None)
            if isinstance(sessions_by_chat, dict):
                for chat_id, sessions in list(sessions_by_chat.items()):
                    if not isinstance(sessions, dict):
                        continue
                    for session_uid, session in list(sessions.items()):
                        try:
                            session.interrupt()
                        except Exception:
                            self.logger.exception(
                                "session interrupt failed on desktop shutdown sid=%s",
                                getattr(session, "id", session_uid),
                            )
                        try:
                            session.close()
                        except Exception:
                            self.logger.exception(
                                "session close failed on desktop shutdown sid=%s",
                                getattr(session, "id", session_uid),
                            )
                    sessions.clear()
                try:
                    manager._persist_sessions()
                except Exception:
                    self.logger.exception("persist sessions failed during desktop shutdown")

            if self.ui_state_service and hasattr(self.ui_state_service, "shutdown"):
                try:
                    self.ui_state_service.shutdown()
                except Exception:
                    self.logger.exception("ui state shutdown failed")
        finally:
            self.notify("shutdown:done")

    def _desktop_identity_provider_service(self) -> DesktopIdentityProvider:
        cfg = self._require_desktop_config()
        try:
            state_path = normalize_optional_state_path(getattr(getattr(cfg, "defaults", None), "state_path", None)) or ""
        except TypeError:
            state_path = ""
        if state_path != self._desktop_state_path:
            self._desktop_project_registry = None
            self._desktop_system_event_bus = None
            self._desktop_scheduled_job_repository = None
            self._desktop_scheduler_service = None
            self._desktop_scheduler_started_instance = None
            self._desktop_mode_launch_adapter = None
            self._desktop_identity_provider = None
            self._desktop_state_path = state_path
        if not state_path:
            raise RuntimeError("desktop state_path is not configured")
        if self._desktop_project_registry is None:
            self._desktop_project_registry = ProjectRegistry(state_path)
        if self._desktop_identity_provider is None:
            self._desktop_identity_provider = DesktopIdentityProvider(
                project_registry=self._desktop_project_registry,
                session_service=self.session_service,
                logger_=self.logger,
            )
        return self._desktop_identity_provider

    def _desktop_system_event_bus_instance(self) -> SystemEventBus:
        if self._desktop_system_event_bus is None:
            self._desktop_system_event_bus = SystemEventBus()
        return self._desktop_system_event_bus

    def _desktop_mode_launch_adapter_instance(self) -> ModeLaunchAdapterService:
        bot_app = self._desktop_bot_app()
        bot_app.manager = self.session_service._manager
        bot_app.mode_registry_service = self.mode_registry_service
        bot_app.system_event_bus = self._desktop_system_event_bus_instance()
        if getattr(bot_app, "mode_input_router", None) is None:
            bot_app.mode_input_router = ModeInputRoutingService(
                mode_registry=self.mode_registry_service,
                dialogs=self._get_mode_dialogs(),
                send_message=getattr(bot_app, "_send_message", None),
                send_output=getattr(bot_app, "send_output", None),
                lint_evolution_hook=self._desktop_lint_evolution_hook(),
            )
        else:
            bot_app.mode_input_router.lint_evolution_hook = self._desktop_lint_evolution_hook()
        bot_app.security = self._security_from_runtime_config(bot_app)
        if self._desktop_mode_launch_adapter is None:
            self._desktop_mode_launch_adapter = ModeLaunchAdapterService(bot_app)
        return self._desktop_mode_launch_adapter

    async def _ensure_desktop_event_runtime_started(self) -> None:
        self._desktop_system_event_bus_instance()
        await self._ensure_desktop_scheduler_started()
        if self.mode_registry_service is None:
            return
        adapter = self._desktop_mode_launch_adapter_instance()
        await adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

    async def _ensure_desktop_scheduler_started(self) -> None:
        cfg = self._require_desktop_config()
        if not bool(getattr(getattr(cfg, "scheduler", None), "enabled", False)):
            return
        scheduler = self._desktop_scheduler_service_instance()
        if self._desktop_scheduler_started_instance is scheduler:
            return
        await scheduler.start()
        self._desktop_scheduler_started_instance = scheduler

    def _desktop_scheduler_service_instance(self) -> SchedulerService:
        cfg = self._require_desktop_config()
        self._desktop_identity_provider_service()
        if self._desktop_scheduled_job_repository is None:
            try:
                state_path = normalize_optional_state_path(getattr(getattr(cfg, "defaults", None), "state_path", None)) or ""
            except TypeError:
                state_path = ""
            if not state_path:
                raise RuntimeError("desktop state_path is not configured")
            self._desktop_scheduled_job_repository = ScheduledJobRepository(state_path)
        if self._desktop_scheduler_service is None:
            self._desktop_scheduler_service = SchedulerService(
                repository=self._desktop_scheduled_job_repository,
                event_bus=self._desktop_system_event_bus_instance(),
                scheduler_config=cfg.scheduler,
                logger_=self.logger,
            )
        bot_app = self._desktop_bot_app()
        bot_app.scheduler_service = self._desktop_scheduler_service
        return self._desktop_scheduler_service

    def _desktop_scheduler_presentation_service(self) -> SchedulerPresentationService:
        return SchedulerPresentationService(self._desktop_scheduler_service_instance)

    def _require_desktop_scheduler_project_job(
        self,
        *,
        provider: Any,
        project_slug: str,
        job_id: str,
    ) -> Any:
        if not str(job_id or "").strip():
            raise SchedulerValidationError(f"scheduled job is not found: {job_id}")
        try:
            return self._desktop_scheduler_presentation_service().require_project_job(
                project_slug,
                job_id,
                owner_id=provider.owner_id,
            )
        except SchedulerNotFoundError as exc:
            raise SchedulerValidationError(
                f"scheduled job is not found: {job_id}"
            ) from exc
        except SchedulerOwnershipError as exc:
            raise ProjectOwnershipError(
                f"scheduler job is outside owned project: {project_slug}"
            ) from exc

    def _require_desktop_config(self) -> Any:
        cfg = self.config
        if cfg is None:
            cfg = getattr(self.config_service, "config", None)
        if cfg is None:
            raise RuntimeError("desktop config is not loaded")
        self.config = cfg
        return cfg

    # ------------------------------------------------------------------
    # Admin Autonomy bridge (inventory / baseline / drift / memory / runbooks)
    # ------------------------------------------------------------------

    def _admin_autonomy_workdir(self, session_uid: str) -> str:
        session = self.session_service.get_session_by_uid(str(session_uid or "").strip())
        if not session:
            return ""
        return str(getattr(session, "workdir", "") or "").strip()

    def _admin_autonomy_service(self, session_uid: str):
        workdir = self._admin_autonomy_workdir(session_uid)
        if not workdir:
            return None
        from modes.admin.facade import AdminAutonomyService
        try:
            return AdminAutonomyService(workdir)
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy: service init failed session_uid=%s", session_uid,
            )
            return None

    @staticmethod
    def _admin_autonomy_run_async(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return asyncio.run(coro_factory())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro_factory())
            finally:
                loop.close()

    def admin_autonomy_list_servers(self, session_uid: str) -> List[Dict[str, Any]]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return []
        try:
            return [s.to_dict() for s in svc.list_servers()]
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_list_servers failed session_uid=%s", session_uid,
            )
            return []

    def admin_autonomy_server_summary(
        self, session_uid: str, server_id: str,
    ) -> Optional[Dict[str, Any]]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return None
        try:
            summary = svc.get_server_summary(server_id)
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_server_summary failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return None
        return summary.to_dict() if summary is not None else None

    def admin_autonomy_global_summary(self, session_uid: str) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {}
        try:
            return svc.global_summary()
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_global_summary failed session_uid=%s", session_uid,
            )
            return {}

    def admin_autonomy_get_baseline(
        self, session_uid: str, server_id: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {}
        try:
            return svc.get_baseline(server_id)
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_get_baseline failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {}

    def admin_autonomy_accept_baseline(
        self, session_uid: str, server_id: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            result = svc.accept_baseline(server_id)
            return {"ok": True, "result": result}
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_accept_baseline failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {"ok": False, "error": str(exc)}

    def admin_autonomy_discard_baseline(
        self, session_uid: str, server_id: str,
    ) -> bool:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return False
        try:
            return bool(svc.discard_baseline_proposal(server_id))
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_discard_baseline failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return False

    def admin_autonomy_list_drifts(
        self,
        session_uid: str,
        server_id: str,
        *,
        open_only: bool = True,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return []
        try:
            return svc.list_drifts(server_id, limit=limit, open_only=open_only)
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_list_drifts failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return []

    def admin_autonomy_ack_drift(
        self, session_uid: str, server_id: str, drift_id: int,
    ) -> bool:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return False
        try:
            return bool(svc.ack_drift(server_id, int(drift_id), by="desktop"))
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_ack_drift failed session_uid=%s server_id=%s drift_id=%s",
                session_uid, server_id, drift_id,
            )
            return False

    def admin_autonomy_get_memory(
        self, session_uid: str, server_id: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {}
        try:
            return svc.get_memory(server_id)
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_get_memory failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {}

    def admin_autonomy_update_fact(
        self,
        session_uid: str,
        server_id: str,
        *,
        key: str,
        value: Any,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            entry = svc.update_memory_fact(server_id, key=key, value=value, by="desktop")
            return {"ok": True, "entry": entry}
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_update_fact failed session_uid=%s server_id=%s key=%s",
                session_uid, server_id, key,
            )
            return {"ok": False, "error": str(exc)}

    def admin_autonomy_delete_fact(
        self, session_uid: str, server_id: str, key: str,
    ) -> bool:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return False
        try:
            return bool(svc.delete_memory_fact(server_id, key))
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_delete_fact failed session_uid=%s server_id=%s key=%s",
                session_uid, server_id, key,
            )
            return False

    def admin_autonomy_append_note(
        self,
        session_uid: str,
        server_id: str,
        text: str,
        *,
        tags: Optional[List[str]] = None,
    ) -> bool:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return False
        try:
            svc.append_memory_note(server_id, text, source="desktop", tags=list(tags or []))
            return True
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_append_note failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return False

    def admin_autonomy_compact_memory(
        self, session_uid: str, server_id: str, *, force: bool = False,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            result = svc.compact_memory(server_id, force=bool(force))
            return {"ok": True, "result": result}
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_compact_memory failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {"ok": False, "error": str(exc)}

    def admin_autonomy_list_runbooks(
        self,
        session_uid: str,
        server_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return []
        try:
            return svc.list_runbook_summary(
                server_id=server_id, tags=tags, limit=100,
            )
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_list_runbooks failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return []

    def admin_autonomy_get_runbook(
        self,
        session_uid: str,
        runbook_id: str,
        *,
        server_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return None
        try:
            rb = svc.get_runbook(runbook_id, server_id=server_id)
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_get_runbook failed session_uid=%s runbook_id=%s",
                session_uid, runbook_id,
            )
            return None
        return rb.as_dict() if rb is not None else None

    def admin_autonomy_rescan_server(
        self, session_uid: str, server_id: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}

        async def _run():
            return await svc.rescan_server(server_id)

        try:
            report = self._admin_autonomy_run_async(_run)
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_rescan_server failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {"ok": False, "error": str(exc)}
        return {
            "ok": bool(getattr(report, "ok", False)),
            "server_id": str(getattr(report, "server_id", server_id) or ""),
            "baseline_present": bool(getattr(report, "baseline_present", False)),
            "snapshots_written": int(getattr(report, "snapshots_written", 0) or 0),
            "drifts_written": int(getattr(report, "drifts_written", 0) or 0),
            "drifts_by_severity": dict(getattr(report, "drifts_by_severity", {}) or {}),
            "alarm_count": int(getattr(report, "alarm_count", 0) or 0),
            "warn_count": int(getattr(report, "warn_count", 0) or 0),
            "error": getattr(report, "error", None),
        }

    def admin_autonomy_rescan_all(self, session_uid: str) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}

        async def _run():
            return await svc.rescan_all()

        try:
            reports = self._admin_autonomy_run_async(_run) or []
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_rescan_all failed session_uid=%s", session_uid,
            )
            return {"ok": False, "error": str(exc)}
        servers: List[Dict[str, Any]] = []
        for report in reports:
            servers.append({
                "server_id": str(getattr(report, "server_id", "") or ""),
                "ok": bool(getattr(report, "ok", False)),
                "drifts_written": int(getattr(report, "drifts_written", 0) or 0),
                "alarm_count": int(getattr(report, "alarm_count", 0) or 0),
                "warn_count": int(getattr(report, "warn_count", 0) or 0),
                "error": getattr(report, "error", None),
            })
        return {"ok": True, "servers": servers}

    def admin_autonomy_run_daily_maintenance(
        self, session_uid: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            report = svc.run_daily_maintenance()
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_run_daily_maintenance failed session_uid=%s",
                session_uid,
            )
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "report": dict(report or {})}

    def admin_autonomy_validate_runbook(
        self, session_uid: str, runbook_id: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}

        async def _run():
            return await svc.validate_runbook(runbook_id)

        try:
            report = self._admin_autonomy_run_async(_run)
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_validate_runbook failed session_uid=%s runbook_id=%s",
                session_uid, runbook_id,
            )
            return {"ok": False, "error": str(exc)}
        as_dict = getattr(report, "to_dict", None)
        payload = as_dict() if callable(as_dict) else {
            "ok": bool(getattr(report, "ok", False)),
            "checks": list(getattr(report, "checks", []) or []),
            "errors": list(getattr(report, "errors", []) or []),
            "warnings": list(getattr(report, "warnings", []) or []),
        }
        return {"ok": True, "report": payload}

    def admin_autonomy_promote_runbook(
        self,
        session_uid: str,
        runbook_id: str,
        *,
        add_servers: List[str],
        confidence: Optional[float] = None,
        run_validation: bool = True,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}

        async def _run():
            return await svc.promote_runbook(
                runbook_id,
                add_servers=list(add_servers or []),
                confidence=confidence,
                run_validation=bool(run_validation),
            )

        try:
            result = self._admin_autonomy_run_async(_run)
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_promote_runbook failed session_uid=%s runbook_id=%s",
                session_uid, runbook_id,
            )
            return {"ok": False, "error": str(exc)}
        as_dict = getattr(result, "to_dict", None)
        payload = as_dict() if callable(as_dict) else {
            "rb_id": getattr(result, "rb_id", runbook_id),
            "added_servers": list(getattr(result, "added_servers", []) or []),
            "already_present": list(getattr(result, "already_present", []) or []),
            "confidence_before": getattr(result, "confidence_before", None),
            "confidence_after": getattr(result, "confidence_after", None),
        }
        return {"ok": True, "result": payload}

    def admin_autonomy_run_step(
        self,
        session_uid: str,
        runbook_id: str,
        *,
        step_name: str,
        server_id: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}

        async def _run():
            return await svc.run_runbook_step(
                rb_id=runbook_id,
                step_name=step_name,
                server_id=server_id,
                dry_run=bool(dry_run),
            )

        try:
            result = self._admin_autonomy_run_async(_run)
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_run_step failed session_uid=%s runbook_id=%s step=%s",
                session_uid, runbook_id, step_name,
            )
            return {"ok": False, "error": str(exc)}
        as_dict = getattr(result, "to_dict", None)
        payload = as_dict() if callable(as_dict) else {
            "rb_id": getattr(result, "rb_id", runbook_id),
            "step": getattr(result, "step", step_name),
            "target": getattr(result, "target", server_id),
            "dry_run": bool(getattr(result, "dry_run", dry_run)),
            "success": bool(getattr(result, "success", False)),
            "exit_code": getattr(result, "exit_code", None),
            "stdout": getattr(result, "stdout", "") or "",
            "stderr": getattr(result, "stderr", "") or "",
            "error": getattr(result, "error", None),
        }
        return {"ok": True, "result": payload}

    def admin_autonomy_scan_scripts(
        self, session_uid: str, directory: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty", "files": []}
        try:
            files = svc.scan_script_sources(str(directory or "").strip())
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_scan_scripts failed session_uid=%s dir=%s",
                session_uid, directory,
            )
            return {"ok": False, "error": str(exc), "files": []}
        out: List[Dict[str, Any]] = []
        for f in files or []:
            out.append({
                "name": getattr(f, "name", ""),
                "path": getattr(f, "path", ""),
                "size_bytes": getattr(f, "size_bytes", 0),
                "sha1": getattr(f, "sha1", ""),
            })
        return {"ok": True, "files": out}

    def admin_autonomy_build_runbook(
        self,
        session_uid: str,
        *,
        title: str,
        dev_server_id: str,
        scripts: List[Dict[str, Any]],
        rb_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        description: str = "",
        force: bool = False,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            rb = svc.create_runbook_from_scripts(
                title=str(title or "").strip(),
                dev_server_id=str(dev_server_id or "").strip(),
                scripts=list(scripts or []),
                rb_id=rb_id,
                tags=list(tags or []),
                description=str(description or ""),
                force=bool(force),
            )
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_build_runbook failed session_uid=%s title=%s",
                session_uid, title,
            )
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "runbook": {
                "id": getattr(rb, "id", ""),
                "title": getattr(rb, "title", ""),
                "path": str(getattr(rb, "path", "") or ""),
                "servers": list(getattr(rb, "servers", []) or []),
                "tags": list(getattr(rb, "tags", []) or []),
            },
        }

    def admin_autonomy_list_snapshot_checks(
        self, session_uid: str, server_id: str,
    ) -> List[str]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return []
        try:
            return list(svc.list_snapshot_checks(server_id) or [])
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_list_snapshot_checks failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return []

    def admin_autonomy_get_snapshots(
        self,
        session_uid: str,
        server_id: str,
        check_id: str,
        *,
        limit: int = 100,
        since_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return []
        try:
            return list(svc.get_snapshots(
                server_id, check_id, limit=int(limit), since_ts=since_ts,
            ) or [])
        except Exception:
            self.logger.exception(
                "desktop admin_autonomy_get_snapshots failed session_uid=%s server_id=%s check_id=%s",
                session_uid, server_id, check_id,
            )
            return []

    def admin_autonomy_check_prereqs(
        self, session_uid: str, server_id: str,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            report = svc.check_server_prereqs(server_id)
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_check_prereqs failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {"ok": False, "error": str(exc)}
        as_dict = getattr(report, "to_dict", None)
        payload = as_dict() if callable(as_dict) else {}
        return {"ok": True, "report": payload}

    def admin_autonomy_build_prereqs_bootstrap(
        self, session_uid: str, server_id: str, *, force: bool = False,
    ) -> Dict[str, Any]:
        svc = self._admin_autonomy_service(session_uid)
        if svc is None:
            return {"ok": False, "error": "session_workdir_empty"}
        try:
            result = svc.generate_bootstrap_runbook(server_id, force=bool(force))
        except Exception as exc:
            self.logger.exception(
                "desktop admin_autonomy_build_prereqs_bootstrap failed session_uid=%s server_id=%s",
                session_uid, server_id,
            )
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **(result or {})}
