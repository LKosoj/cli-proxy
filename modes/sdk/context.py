from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .services.dialogs import DialogService
from .services.messaging import MessagingService
from .services.session_control import SessionControlService
from .services.storage import StorageService
from .services.tasks import TaskService
from .services.tooling import ModeToolingService


EventHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]
RuntimeByCapabilityFn = Callable[[str], Any]


class EventBus:
    """Minimal async event bus for mode/core communication."""

    def __init__(self) -> None:
        self._subs: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event: str, handler: EventHandler) -> None:
        self._subs.setdefault(str(event), []).append(handler)

    async def publish(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        handlers = list(self._subs.get(str(event), []))
        if not handlers:
            return
        data = payload if payload is not None else {}
        for h in handlers:
            try:
                await h(str(event), data)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "EventBus handler %s failed for event %s", getattr(h, '__name__', h), event
                )


@dataclass
class ModeContext:
    """
    Context provided to a Mode.

    Holds standard services and any Core-provided references (config, bot, transport context).
    """

    mode_id: str
    session_id: str
    chat_id: int
    user_id: Optional[int] = None

    config: Any = None
    transport: Any = None
    event_bus: EventBus = field(default_factory=EventBus)

    messaging: Optional[MessagingService] = None
    storage: Optional[StorageService] = None
    tasks: Optional[TaskService] = None
    dialogs: Optional[DialogService] = None
    session_control: Optional[SessionControlService] = None

    extras: Dict[str, Any] = field(default_factory=dict)

    def require_messaging(self) -> MessagingService:
        if not self.messaging:
            raise RuntimeError("MessagingService is not configured on ModeContext")
        return self.messaging

    def require_storage(self) -> StorageService:
        if not self.storage:
            raise RuntimeError("StorageService is not configured on ModeContext")
        return self.storage

    def require_tasks(self) -> TaskService:
        if not self.tasks:
            raise RuntimeError("TaskService is not configured on ModeContext")
        return self.tasks

    def require_dialogs(self) -> DialogService:
        if not self.dialogs:
            raise RuntimeError("DialogService is not configured on ModeContext")
        return self.dialogs

    def require_session_control(self) -> SessionControlService:
        if not self.session_control:
            raise RuntimeError("SessionControlService is not configured on ModeContext")
        return self.session_control


@dataclass(frozen=True)
class ModeRuntimeContext:
    """Typed runtime view over the legacy mode ctx dictionary."""

    mode_id: str
    session: Any
    chat_id: Any
    user_id: Optional[int]
    dest: Dict[str, Any]
    transport_context: Any
    config: Any
    messaging: MessagingService
    tasks: TaskService
    dialogs: DialogService
    session_control: SessionControlService
    tooling: ModeToolingService
    runtime_by_capability: RuntimeByCapabilityFn

    @property
    def context(self) -> Any:
        return self.transport_context


def _mode_service(mode: Any, name: str) -> Any:
    getter = getattr(mode, "get_service", None)
    if not callable(getter):
        raise RuntimeError("mode service access is not configured")
    value = getter(name)
    if value is None:
        raise RuntimeError(f"{name} service is not configured")
    return value


def mode_runtime_context_from_legacy(ctx: Dict[str, Any], mode: Any) -> ModeRuntimeContext:
    legacy_ctx = dict(ctx or {})
    transport_context = legacy_ctx.get("transport_context")
    if transport_context is None:
        transport_context = legacy_ctx.get("context")

    messaging_factory = _mode_service(mode, "messaging_factory")
    if not callable(messaging_factory):
        raise RuntimeError("messaging_factory is not configured")
    messaging = messaging_factory(transport_context)
    if not isinstance(messaging, MessagingService):
        mode_id = getattr(mode, "get_mode_id", lambda: mode.__class__.__name__)()
        raise RuntimeError(f"{mode_id} messaging_factory must return MessagingService")

    runtime_by_capability = _mode_service(mode, "runtime_by_capability")
    if not callable(runtime_by_capability):
        raise RuntimeError("runtime_by_capability is not configured")

    mode_id = getattr(mode, "get_mode_id", lambda: mode.__class__.__name__)()
    return ModeRuntimeContext(
        mode_id=str(mode_id),
        session=legacy_ctx.get("session"),
        chat_id=legacy_ctx.get("chat_id"),
        user_id=legacy_ctx.get("user_id"),
        dest=dict(legacy_ctx.get("dest") or {}),
        transport_context=transport_context,
        config=legacy_ctx.get("config", getattr(mode, "config", None)),
        messaging=messaging,
        tasks=_mode_service(mode, "tasks"),
        dialogs=_mode_service(mode, "dialogs"),
        session_control=_mode_service(mode, "session_control"),
        tooling=_mode_service(mode, "tooling"),
        runtime_by_capability=runtime_by_capability,
    )
