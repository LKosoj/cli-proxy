from __future__ import annotations

import os
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, Optional, Type, TypeVar, TYPE_CHECKING, cast

from sessions.queue_item import append_session_queue_item
from sessions.session_state_access import get_active_mode, set_active_mode

from .models import CallbackModel, MessageModel, ToolResult
from .context import ModeRuntimeContext, mode_runtime_context_from_legacy
from .session_busy import is_session_busy
from .services.callback_data import build_session_overview_callback_data
from .services.dialogs import DialogService
from .services.messaging import MessagingService
from .services.runtime import AgentRuntimeService, DictStateService, DirsFlowService, ModePipelineService
from .services.tooling import ModeToolingService

if TYPE_CHECKING:
    from app.mode_dependencies import (
        ModeDependencies,
        RunArtifactsService,
        RunBoundaryValidationService,
        RunDoctorService,
        RunObservabilityService,
        ModeRunLifecycleService,
        SkillRuntimeService,
    )

T = TypeVar("T")


def _session_runtime_uid(session: Any) -> str:
    from session import session_runtime_uid

    return session_runtime_uid(session)


class BaseMode(ABC):
    """
    Base class for "modes" (pluggable behavior modules).

    Mode dependencies are provided during `initialize(...)`.
    """

    mode_id: Optional[str] = None
    display_name: Optional[str] = None
    description: str = ""

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        self.config: Any = None
        self.mode_dependencies: Optional[ModeDependencies] = dependencies
        self._extra_services: Dict[str, Any] = {}
        self._log = logging.getLogger(self.__class__.__name__)

    def get_mode_id(self) -> str:
        return self.mode_id or self.__class__.__name__

    def initialize(
        self,
        config: Any = None,
        services: Optional[Dict[str, Any]] = None,
        mode_dependencies: Optional[ModeDependencies] = None,
    ) -> None:
        self.config = config
        raw_services = dict(services or {})
        deps = mode_dependencies or raw_services.get("mode_dependencies") or self.mode_dependencies
        if deps is not None:
            deps = deps.with_overrides(
                run_artifacts=raw_services.get("run_artifacts"),
                run_observability=raw_services.get("run_observability"),
                run_doctor=raw_services.get("run_doctor"),
                run_boundary_validation=raw_services.get("run_boundary_validation"),
                mode_run_lifecycle=raw_services.get("mode_run_lifecycle"),
                skill_runtime=raw_services.get("skill_runtime"),
                tasks=raw_services.get("tasks"),
                dialogs=raw_services.get("dialogs"),
                session_control=raw_services.get("session_control"),
                messaging_factory=raw_services.get("messaging_factory"),
                session_mutation_service=raw_services.get("session_mutation_service"),
                agent_runtime=raw_services.get("agent_runtime"),
                dirs_flow=raw_services.get("dirs_flow"),
                manager_pending=raw_services.get("manager_pending"),
                agent_pending=raw_services.get("agent_pending"),
                runtime_by_capability=raw_services.get("runtime_by_capability"),
                tooling=raw_services.get("tooling"),
            )
        self.mode_dependencies = deps
        excluded = {"mode_dependencies"}
        if deps is not None:
            excluded.update(
                {
                    "run_artifacts",
                    "run_observability",
                    "run_doctor",
                    "run_boundary_validation",
                    "mode_run_lifecycle",
                    "skill_runtime",
                    "tasks",
                    "dialogs",
                    "session_control",
                    "messaging_factory",
                    "session_mutation_service",
                    "agent_runtime",
                    "dirs_flow",
                    "manager_pending",
                    "agent_pending",
                    "runtime_by_capability",
                    "tooling",
                }
            )
        self._extra_services = {
            key: value
            for key, value in raw_services.items()
            if key not in excluded
        }

    def _require_mode_dependencies(self) -> "ModeDependencies":
        deps = self.mode_dependencies
        if deps is None:
            raise RuntimeError(f"{self.get_mode_id()} mode dependencies are not configured")
        return deps

    def _require_service(self, key: str, service_type: Type[T]) -> T:
        value = self.get_service(key)
        if value is None:
            raise RuntimeError(f"{key} service is not configured")
        return cast(T, value)

    def _optional_service(self, key: str, service_type: Type[T]) -> Optional[T]:
        value = self.get_service(key)
        return cast(T, value) if value is not None else None

    def _pipeline(self) -> ModePipelineService:
        return self._require_service("pipeline", ModePipelineService)

    def _runtime_getter(self) -> Callable[[str], Any]:
        runtime_getter = self.get_service("runtime_by_capability")
        if runtime_getter is None:
            raise RuntimeError("runtime_by_capability is not configured")
        return cast(Callable[[str], Any], runtime_getter)

    def _optional_runtime_getter(self) -> Optional[Callable[[str], Any]]:
        runtime_getter = self.get_service("runtime_by_capability")
        return cast(Callable[[str], Any], runtime_getter) if runtime_getter is not None else None

    def _tooling(self) -> ModeToolingService:
        return self._require_service("tooling", ModeToolingService)

    def _optional_tooling(self) -> Optional[ModeToolingService]:
        return self._optional_service("tooling", ModeToolingService)

    def _run_artifacts(self) -> RunArtifactsService:
        from app.mode_dependencies import RunArtifactsService
        return self._require_service("run_artifacts", RunArtifactsService)

    def _optional_run_artifacts(self) -> Optional[RunArtifactsService]:
        from app.mode_dependencies import RunArtifactsService
        return self._optional_service("run_artifacts", RunArtifactsService)

    def _run_observability(self) -> RunObservabilityService:
        from app.mode_dependencies import RunObservabilityService
        return self._require_service("run_observability", RunObservabilityService)

    def _optional_run_observability(self) -> Optional[RunObservabilityService]:
        from app.mode_dependencies import RunObservabilityService
        return self._optional_service("run_observability", RunObservabilityService)

    def _run_doctor(self) -> RunDoctorService:
        from app.mode_dependencies import RunDoctorService
        return self._require_service("run_doctor", RunDoctorService)

    def _optional_run_doctor(self) -> Optional[RunDoctorService]:
        from app.mode_dependencies import RunDoctorService
        return self._optional_service("run_doctor", RunDoctorService)

    def _run_boundary_validation(self) -> RunBoundaryValidationService:
        from app.mode_dependencies import RunBoundaryValidationService
        return self._require_service("run_boundary_validation", RunBoundaryValidationService)

    def _optional_run_boundary_validation(self) -> Optional[RunBoundaryValidationService]:
        from app.mode_dependencies import RunBoundaryValidationService
        return self._optional_service("run_boundary_validation", RunBoundaryValidationService)

    def _mode_run_lifecycle(self) -> ModeRunLifecycleService:
        from app.mode_dependencies import ModeRunLifecycleService
        return self._require_service("mode_run_lifecycle", ModeRunLifecycleService)

    def _optional_mode_run_lifecycle(self) -> Optional[ModeRunLifecycleService]:
        from app.mode_dependencies import ModeRunLifecycleService
        return self._optional_service("mode_run_lifecycle", ModeRunLifecycleService)

    def _skill_runtime(self) -> SkillRuntimeService:
        from app.mode_dependencies import SkillRuntimeService
        return self._require_service("skill_runtime", SkillRuntimeService)

    def _optional_skill_runtime(self) -> Optional[SkillRuntimeService]:
        from app.mode_dependencies import SkillRuntimeService
        return self._optional_service("skill_runtime", SkillRuntimeService)

    def _agent_runtime(self) -> AgentRuntimeService:
        return self._require_service("agent_runtime", AgentRuntimeService)

    def _dirs_flow(self) -> DirsFlowService:
        return self._require_service("dirs_flow", DirsFlowService)

    def _optional_dirs_flow(self) -> Optional[DirsFlowService]:
        return self._optional_service("dirs_flow", DirsFlowService)

    def _manager_pending(self) -> DictStateService:
        return self._require_service("manager_pending", DictStateService)

    def _agent_pending(self) -> DictStateService:
        return self._require_service("agent_pending", DictStateService)

    def _optional_agent_pending(self) -> Optional[DictStateService]:
        return self._optional_service("agent_pending", DictStateService)

    def _optional_dialogs(self) -> Optional[DialogService]:
        return self._optional_service("dialogs", DialogService)

    def _has_messaging_factory(self) -> bool:
        return callable(self.get_service("messaging_factory"))

    def _runtime_context(self, ctx: Dict[str, Any]) -> ModeRuntimeContext:
        return mode_runtime_context_from_legacy(ctx, self)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        """Called when this mode is enabled."""
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        """Called when this mode is disabled."""
        return None

    @abstractmethod
    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        """Handle a user message."""
        raise NotImplementedError

    @abstractmethod
    async def handle_callback(self, callback: CallbackModel, ctx: Dict[str, Any]) -> ToolResult:
        """Handle an interaction callback (e.g. inline button press)."""
        raise NotImplementedError

    def framework_sends_output(self) -> bool:
        """Whether SessionRunService should deliver run_pipeline() return value via send_output()."""
        return True

    @staticmethod
    def _normalize_callback_chat_id(chat_id: Any) -> Any:
        raw = str(chat_id or "").strip()
        if raw:
            try:
                return int(raw)
            except Exception:
                return raw
        return chat_id

    def build_runtime(self, config: Any) -> Any:
        """
        Optional mode-owned runtime factory.

        Core calls this for loaded plugins only; returning None means
        this mode does not provide a dedicated runtime service.
        """
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_service(self, key: str, default: Any = None) -> Any:
        deps = self.mode_dependencies
        if deps is not None:
            mapping = {
                "mode_dependencies": deps,
                "run_artifacts": deps.run_artifacts,
                "run_observability": deps.run_observability,
                "run_doctor": deps.run_doctor,
                "run_boundary_validation": deps.run_boundary_validation,
                "mode_run_lifecycle": deps.mode_run_lifecycle,
                "skill_runtime": deps.skill_runtime,
                "tasks": deps.tasks,
                "dialogs": deps.dialogs,
                "session_control": deps.session_control,
                "messaging_factory": deps.messaging_factory,
                "session_mutation_service": deps.session_mutation_service,
                "pipeline": deps.pipeline,
                "agent_runtime": deps.agent_runtime,
                "dirs_flow": deps.dirs_flow,
                "manager_pending": deps.manager_pending,
                "agent_pending": deps.agent_pending,
                "runtime_by_capability": deps.runtime_by_capability,
                "tooling": deps.tooling,
                "registry": deps.registry,
                "session_manager": deps.session_manager,
            }
            if key in mapping:
                value = mapping.get(key)
                return default if value is None else value
        return self._extra_services.get(key, default)

    def require_service(self, key: str) -> Any:
        value = self.get_service(key, None)
        if value is None:
            raise KeyError(f"Missing required service: {key}")
        return value

    def logger(self) -> logging.Logger:
        return self._log

    def _is_mode_active(self, session: Any) -> bool:
        return str(get_active_mode(session, "") or "").strip() == self.get_mode_id()

    def _persist_sessions(self, bot_app: Any) -> None:
        try:
            mutation_service = self.get_service("session_mutation_service")
            if mutation_service is not None:
                if not cast(Any, mutation_service).persist_all():
                    raise RuntimeError("session_mutation_service.persist_all returned false")
                return
            legacy_control = self.get_service("session_control")
            if legacy_control is None:
                raise RuntimeError("session_mutation_service is not configured")
            cast(Any, legacy_control).persist()
        except Exception:
            self._log.exception("%s persist sessions failed", self.get_mode_id())

    async def _activate_mode(
        self,
        *,
        session: Any,
        bot_app: Any,
        cli_work_type: Optional[str] = None,
        executor_profile: Optional[str] = None,
    ) -> None:
        set_active_mode(session, self.get_mode_id())
        if hasattr(session, "cli") and getattr(session, "cli", None) is not None:
            try:
                session.cli.cli_work_type = cli_work_type
            except Exception:
                setattr(session, "cli_work_type", cli_work_type)
        else:
            setattr(session, "cli_work_type", cli_work_type)
        session.executor_profile = executor_profile
        self._persist_sessions(bot_app)

    async def _deactivate_mode(
        self,
        *,
        session: Any,
        bot_app: Any,
        cancel_tasks: bool = False,
        timeout_s: float = 0.2,
    ) -> None:
        if self._is_mode_active(session):
            set_active_mode(session, None)
        if hasattr(session, "cli") and getattr(session, "cli", None) is not None:
            try:
                session.cli.cli_work_type = None
            except Exception:
                setattr(session, "cli_work_type", None)
        else:
            setattr(session, "cli_work_type", None)
        session.executor_profile = None
        self._persist_sessions(bot_app)
        if cancel_tasks:
            try:
                svc = self.get_service("session_control")
                if svc is None:
                    raise RuntimeError("session_control is not configured")
                await cast(Any, svc).cancel_mode(
                    session_id=_session_runtime_uid(session),
                    mode_id=self.get_mode_id(),
                    timeout_s=timeout_s,
                )
            except Exception:
                self._log.exception("%s cancel mode tasks failed", self.get_mode_id())

    async def _enqueue_if_busy(
        self,
        *,
        session: Any,
        bot_app: Any,
        ms: MessagingService,
        chat_id: int,
        text: str,
        dest: Dict[str, Any],
        notify_text: str = "Сессия занята. Добавил запрос в очередь.",
    ) -> bool:
        run_lock = getattr(session, "run_lock", None)
        queue_busy = bool(getattr(session, "queue", None))

        mode_task_busy = False
        try:
            mode_tasks = getattr(bot_app, "mode_tasks", None)
            session_uid = _session_runtime_uid(session)
            active_mode = str(get_active_mode(session, "") or "").strip()
            if mode_tasks is not None and session_uid and active_mode and hasattr(mode_tasks, "list"):
                mode_task_busy = bool(mode_tasks.list(session_uid=session_uid, mode_id=active_mode))
        except Exception:
            self._log.exception("%s busy check by mode tasks failed", self.get_mode_id())

        busy = is_session_busy(session, run_lock) or queue_busy or mode_task_busy
        if not busy:
            return False
        dispatcher = getattr(bot_app, "input_dispatch_service", None)
        if dispatcher is not None and getattr(ms, "transport_context", None) is not None:
            try:
                await dispatcher.handle_cli_input(
                    session,
                    text,
                    chat_id,
                    ms.transport_context,
                    dest=dict(dest or {}),
                    enforce_direct_cli_policy=False,
                )
                return True
            except Exception:
                self._log.exception("%s dispatch failed, falling back to queue", self.get_mode_id())
        try:
            if not append_session_queue_item(session, {"text": text, "dest": dict(dest)}):
                raise RuntimeError("queue append rejected")
            self._persist_sessions(bot_app)
        except Exception:
            self._log.exception("%s append to queue failed", self.get_mode_id())
        try:
            if getattr(ms, "send_message", None):
                await ms.send_message(
                    ms.transport_context,
                    chat_id=chat_id,
                    text=notify_text,
                    md2=True,
                )
            else:
                await ms.send_text(chat_id, notify_text, md2=True)
        except Exception:
            self._log.exception("%s queue notify failed", self.get_mode_id())
        return True

    def _normalize_dest(
        self,
        *,
        ctx_dest: Optional[Dict[str, Any]],
        chat_id: int,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        dest: Dict[str, Any] = dict(ctx_dest or {"kind": "telegram", "chat_id": chat_id})
        if dest.get("kind") == "telegram":
            if dest.get("chat_id") is None:
                dest["chat_id"] = chat_id
            if user_id is not None and dest.get("user_id") is None:
                dest["user_id"] = int(user_id)
        return dest

    def _start_mode_task(
        self,
        *,
        bot_app: Any,
        session: Any,
        coro: Awaitable[Any],
        name: str,
    ) -> None:
        tasks = self.get_service("tasks")
        if tasks is None:
            raise RuntimeError("tasks service is not configured")
        cast(Any, tasks).create(
            session_uid=_session_runtime_uid(session),
            mode_id=self.get_mode_id(),
            coro=coro,
            name=name,
        )

    def _mode_task_names(self, *, bot_app: Any, session: Any) -> list[str]:
        tasks = self.get_service("tasks")
        if tasks is None:
            raise RuntimeError("tasks service is not configured")
        return list(cast(Any, tasks).list(session_uid=_session_runtime_uid(session), mode_id=self.get_mode_id()) or [])

    async def _cancel_mode_tasks(
        self,
        *,
        bot_app: Any,
        session_id: str,
        mode_id: str,
        timeout_s: float = 0.2,
    ) -> int:
        svc = self.get_service("session_control")
        if svc is None:
            raise RuntimeError("session_control is not configured")
        return int(await cast(Any, svc).cancel_mode(session_id=session_id, mode_id=mode_id, timeout_s=timeout_s))

    async def _check_enable_requirements(
        self,
        *,
        bot_app: Any,
        session: Any,
        ms: MessagingService,
        query: Any,
        chat_id: int,
        require_openai: bool = False,
        require_workdir: bool = False,
        openai_error_text: str = "",
        workdir_error_text: str = "",
    ) -> bool:
        if require_openai:
            if not bot_app.config.defaults.openai_api_key or not bot_app.config.defaults.openai_model:
                text = openai_error_text or "Нужно настроить OpenAI API."
                await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
                return False
        if require_workdir and not os.path.isdir(session.workdir):
            text = workdir_error_text or "Сначала создайте сессию через /sessions."
            await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
            return False
        return True

    async def _dispatch_callback_action(
        self,
        *,
        action: str,
        handlers: Dict[str, Callable[[], Awaitable[ToolResult]]],
    ) -> Optional[ToolResult]:
        return await self._dispatch_mode_action(action=action, handlers=handlers)

    async def _dispatch_input_action(
        self,
        *,
        action: str,
        handlers: Dict[str, Callable[[], Awaitable[ToolResult]]],
    ) -> Optional[ToolResult]:
        return await self._dispatch_mode_action(action=action, handlers=handlers)

    async def _dispatch_mode_action(
        self,
        *,
        action: str,
        handlers: Dict[str, Callable[[], Awaitable[ToolResult]]],
    ) -> Optional[ToolResult]:
        fn = handlers.get(str(action or "").strip())
        if not fn:
            return None
        return await fn()

    def _messaging(self, *, bot_app: Any, context: Any) -> MessagingService:
        messaging_factory = self.get_service("messaging_factory")
        if not callable(messaging_factory):
            raise RuntimeError("messaging_factory is not configured")
        ms = messaging_factory(context)
        if not isinstance(ms, MessagingService):
            raise RuntimeError(f"{self.get_mode_id()} messaging_factory must return MessagingService")
        return ms

    def _dialogs(self, *, bot_app: Any) -> Any:
        dialogs = self.get_service("dialogs")
        if dialogs is None:
            raise RuntimeError("dialogs service is not configured")
        return dialogs

    async def _rerender_menu_common(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        note: str = "",
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
        note_formatter: Optional[Callable[[str], str]] = None,
    ) -> None:
        from app.services.menu_visibility_policy import build_mode_menu_visibility, call_mode_build_menu

        effective_back_callback = str(back_callback or "sess_active")
        if effective_back_callback == "sess_active":
            effective_back_callback = build_session_overview_callback_data(session)
        menu_visibility = build_mode_menu_visibility(
            session=session,
            mode_id=self.get_mode_id(),
            access_policy=getattr(bot_app, "access_policy_service", None),
            user_id=getattr(getattr(query, "from_user", None), "id", None),
        )
        text, keyboard = call_mode_build_menu(
            self,
            session,
            back_callback=effective_back_callback,
            back_text=back_text,
            menu_visibility=menu_visibility,
        )
        if note:
            note_text = note_formatter(note) if note_formatter else note
            text = f"{note_text}\n\n{text}"
        ms = self._messaging(bot_app=bot_app, context=context)
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True, reply_markup=keyboard)

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
    ) -> tuple[str, Any]:
        raise NotImplementedError(f"{self.__class__.__name__}.build_menu is not implemented")

    def allows_agent_plugin_ui(self) -> bool:
        """Whether agent ToolRegistry Telegram UI handlers should run in this mode."""
        return False

    def include_files_in_dirs(self, flow: str) -> bool:
        """Whether directory picker for the mode flow should include files."""
        return False

    async def handle_dirs_selection(self, *, flow: str, event: str, path: str, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        """Handle directory picker events for mode-scoped flows."""
        return None

    def pre_run_reset_mode_id(self) -> Optional[str]:
        """Mode id to preserve when resetting session state before a mode run."""
        return None
