from __future__ import annotations

import asyncio
import copy
import dataclasses
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, Iterable, Mapping, Optional, TypeVar


EventSelector = str | type["BaseSystemEvent"]
SystemEventHandler = Callable[..., Any]
TEvent = TypeVar("TEvent", bound="BaseSystemEvent")

_EVENT_TYPES_BY_NAME: dict[str, type["BaseSystemEvent"]] = {}


def _register_event_type(cls: type[TEvent]) -> type[TEvent]:
    event_name = str(getattr(cls, "EVENT_NAME", "") or "").strip()
    if event_name:
        _EVENT_TYPES_BY_NAME[event_name] = cls
    return cls


class BaseSystemEvent:
    EVENT_NAME: ClassVar[str] = "system.event"

    @property
    def event_name(self) -> str:
        return str(type(self).EVENT_NAME)

    def to_payload(self) -> dict[str, Any]:
        if not dataclasses.is_dataclass(self):
            return {}
        return dataclasses.asdict(self)

    @classmethod
    def from_payload(cls: type[TEvent], payload: Mapping[str, Any] | None = None) -> TEvent:
        data = dict(payload or {})
        kwargs: dict[str, Any] = {}
        for field_def in dataclasses.fields(cls):
            if field_def.init and field_def.name in data:
                kwargs[field_def.name] = data[field_def.name]
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class GenericSystemEvent(BaseSystemEvent):
    name: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_name(self) -> str:
        return str(self.name or self.EVENT_NAME)

    def to_payload(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.payload))


@_register_event_type
@dataclass(frozen=True, slots=True)
class TelegramIngressEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "telegram.ingress"

    chat_id: int = 0
    user_id: int = 0
    update_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class WebhookReceivedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "webhook.received"

    source: str = ""
    path: str = ""
    method: str = ""
    correlation_id: str = ""
    dry_run: bool = False
    headers: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class DesktopCommandEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "desktop.command"

    session_uid: str = ""
    project_slug: str = ""
    command: str = ""
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class MiniAppCommandEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "miniapp.command"

    user_id: str = ""
    session_uid: str = ""
    project_slug: str = ""
    command: str = ""
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class ScheduledJobEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "scheduler.job"

    job_id: str = ""
    job_name: str = ""
    status: str = ""
    scheduled_for: float = 0.0
    cron: str = ""
    target_mode: str = ""
    owner_id: str = ""
    correlation_id: str = ""
    dry_run: bool = False
    notification_target: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class ModeLaunchRequestedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "mode.launch.requested"

    origin: str = ""
    mode_id: str = ""
    session_uid: str = ""
    project_slug: str = ""
    correlation_id: str = ""
    actor: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    dry_run: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class ModeLaunchCompletedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "mode.launch.completed"

    origin: str = ""
    mode_id: str = ""
    session_uid: str = ""
    project_slug: str = ""
    correlation_id: str = ""
    status: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class NotificationRequestedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "notification.requested"

    channel: str = ""
    session_uid: str = ""
    chat_id: int = 0
    message_thread_id: int = 0
    correlation_id: str = ""
    producer: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class ManageTasksChangedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "manage_tasks.changed"

    session_uid: str = ""
    chat_id: Any = ""
    scope_key: str = ""
    run_id: str = ""
    correlation_id: str = ""
    action: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class SecurityAuditEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "security.audit"

    category: str = ""
    action: str = ""
    status: str = ""
    user_id: str = ""
    subject: str = ""
    scope: str = ""
    reason: str = ""
    timestamp: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@_register_event_type
@dataclass(frozen=True, slots=True)
class RuntimeConfigReloadedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "runtime.config.reloaded"

    path: str = ""
    status: str = ""
    applied: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@_register_event_type
@dataclass(frozen=True, slots=True)
class RuntimeConfigReloadFailedEvent(BaseSystemEvent):
    EVENT_NAME: ClassVar[str] = "runtime.config.reload_failed"

    path: str = ""
    status: str = ""
    applied: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _QueuedEvent:
    event: BaseSystemEvent
    done: asyncio.Future[None]


class SystemEventBus:
    """Async queue-based system event bus with typed events and handler isolation."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._subs_by_name: Dict[str, list[SystemEventHandler]] = {}
        self._subs_by_type: Dict[type[BaseSystemEvent], list[SystemEventHandler]] = {}
        self._queue: asyncio.Queue[_QueuedEvent] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def subscribe(self, event: EventSelector, handler: SystemEventHandler) -> Callable[[], None]:
        if isinstance(event, str):
            key = str(event)
            bucket = self._subs_by_name.setdefault(key, [])
        else:
            key = str(getattr(event, "EVENT_NAME", event.__name__))
            bucket = self._subs_by_type.setdefault(event, [])
        bucket.append(handler)

        def _unsubscribe() -> None:
            handlers = self._subs_by_name.get(key) if isinstance(event, str) else self._subs_by_type.get(event)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:
                return
            if not handlers:
                if isinstance(event, str):
                    self._subs_by_name.pop(key, None)
                else:
                    self._subs_by_type.pop(event, None)

        return _unsubscribe

    async def publish(
        self,
        event: str | BaseSystemEvent,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if self._closed:
            return
        message = self._coerce_event(event, payload)
        runtime_queue = self._ensure_runtime()
        done = asyncio.get_running_loop().create_future()
        await runtime_queue.put(_QueuedEvent(event=message, done=done))
        await done

    async def drain(self) -> None:
        if self._closed:
            return
        runtime_queue = self._ensure_runtime()
        await runtime_queue.join()
        while self._dispatch_tasks:
            await asyncio.gather(*list(self._dispatch_tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        self._closed = True
        worker = self._worker_task
        queue = self._queue
        dispatch_tasks = list(self._dispatch_tasks)
        self._worker_task = None
        self._queue = None
        self._loop = None
        self._dispatch_tasks = set()
        if dispatch_tasks:
            await asyncio.gather(*dispatch_tasks, return_exceptions=True)
        if queue is not None:
            await queue.join()
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    def _ensure_runtime(self) -> asyncio.Queue[_QueuedEvent]:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._queue is None or self._worker_task is None or self._worker_task.done():
            self._loop = loop
            self._queue = asyncio.Queue()
            self._dispatch_tasks = set()
            self._worker_task = loop.create_task(self._worker(), name="system-event-bus-worker")
        return self._queue

    async def _worker(self) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            while True:
                queued = await queue.get()
                task = asyncio.create_task(self._dispatch(queued), name=f"system-event:{queued.event.event_name}")
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._on_dispatch_done)
                queue.task_done()
        except asyncio.CancelledError:
            raise

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self._logger.exception("system event dispatch task failed")

    async def _dispatch(self, queued: _QueuedEvent) -> None:
        try:
            event = queued.event
            handlers = self._resolve_handlers(event)
            if handlers:
                await asyncio.gather(
                    *(self._invoke_handler(handler, event) for handler in handlers),
                    return_exceptions=True,
                )
        finally:
            if not queued.done.done():
                queued.done.set_result(None)

    def _resolve_handlers(self, event: BaseSystemEvent) -> list[SystemEventHandler]:
        handlers: list[SystemEventHandler] = []
        handlers.extend(self._subs_by_name.get(event.event_name, ()))
        for event_type, registered_handlers in self._subs_by_type.items():
            if isinstance(event, event_type):
                handlers.extend(registered_handlers)
        return list(handlers)

    async def _invoke_handler(self, handler: SystemEventHandler, event: BaseSystemEvent) -> None:
        try:
            result = self._call_handler(handler, event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self._logger.exception("system event handler failed event=%s", event.event_name)

    @staticmethod
    def _call_handler(handler: SystemEventHandler, event: BaseSystemEvent) -> Any:
        payload = event.to_payload()
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            return handler(event.event_name, copy.deepcopy(payload))

        positional = [
            param
            for param in sig.parameters.values()
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in sig.parameters.values())
        if has_varargs or len(positional) >= 2:
            return handler(event.event_name, copy.deepcopy(payload))
        return handler(event)

    @staticmethod
    def _coerce_event(
        event: str | BaseSystemEvent,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> BaseSystemEvent:
        if isinstance(event, BaseSystemEvent):
            return event
        event_name = str(event or "").strip()
        data = dict(payload or {})
        event_type = _EVENT_TYPES_BY_NAME.get(event_name)
        if event_type is None:
            return GenericSystemEvent(name=event_name, payload=data)
        return event_type.from_payload(data)

    @staticmethod
    def known_event_types() -> Iterable[type[BaseSystemEvent]]:
        return tuple(_EVENT_TYPES_BY_NAME.values())


__all__ = [
    "BaseSystemEvent",
    "DesktopCommandEvent",
    "GenericSystemEvent",
    "ManageTasksChangedEvent",
    "MiniAppCommandEvent",
    "ModeLaunchCompletedEvent",
    "ModeLaunchRequestedEvent",
    "NotificationRequestedEvent",
    "RuntimeConfigReloadFailedEvent",
    "RuntimeConfigReloadedEvent",
    "ScheduledJobEvent",
    "SecurityAuditEvent",
    "SystemEventBus",
    "TelegramIngressEvent",
    "WebhookReceivedEvent",
]
