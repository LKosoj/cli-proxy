from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


def _resolve_task_session_key(
    *,
    session_id: Optional[str] = None,
    session_uid: Optional[str] = None,
) -> Optional[str]:
    value = session_uid if session_uid not in (None, "") else session_id
    token = str(value or "").strip()
    return token or None


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: Optional[str] = None

    def cancel(self, reason: str = "cancelled") -> None:
        if self._event.is_set():
            return
        self._reason = str(reason)
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    async def wait_cancelled(self) -> None:
        await self._event.wait()

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError(self._reason or "cancelled")


class LogBus:
    """Шина для передачи логов в реальном времени подписчикам (например, UI)."""

    def __init__(self):
        self._subscribers: List[Callable[[logging.LogRecord], None]] = []

    def subscribe(self, callback: Callable[[logging.LogRecord], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def _unsubscribe():
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _unsubscribe

    def emit(self, record: logging.LogRecord) -> None:
        for callback in list(self._subscribers):
            try:
                callback(record)
            except Exception:
                pass


@dataclass
class ManagedTask:
    task_id: str
    name: str
    session_uid: Optional[str]
    token: CancellationToken
    task: asyncio.Task
    priority: int = 0
    progress: float = 0.0
    stage: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def session_id(self) -> Optional[str]:
        return self.session_uid


class TaskService:
    """Управление фоновыми задачами с cancellation token."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self._tasks: Dict[str, ManagedTask] = {}
        self.log_bus = LogBus()

        # Подключаем шину логов к системе логирования python
        from app.services.logging_service import register_log_bus
        register_log_bus(self.log_bus)

    def create(
        self,
        *,
        name: str,
        runner: Callable[[CancellationToken], Awaitable[Any]],
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
        priority: int = 0,
    ) -> ManagedTask:
        token = CancellationToken()
        task_id = uuid.uuid4().hex
        task = asyncio.create_task(runner(token))
        rec = ManagedTask(
            task_id=task_id,
            name=str(name),
            session_uid=_resolve_task_session_key(session_id=session_id, session_uid=session_uid),
            token=token,
            task=task,
            priority=int(priority),
        )
        self._tasks[task_id] = rec

        def _done_callback(done_task: asyncio.Task) -> None:
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("managed task failed task_id=%s name=%s", task_id, name)
            finally:
                self._tasks.pop(task_id, None)

        task.add_done_callback(_done_callback)
        return rec

    def list_active(
        self,
        *,
        session_id: Optional[str] = None,
        session_uid: Optional[str] = None,
    ) -> List[ManagedTask]:
        self._prune_done()
        key = _resolve_task_session_key(session_id=session_id, session_uid=session_uid)
        if key is None:
            tasks = list(self._tasks.values())
            return sorted(tasks, key=lambda r: (-int(r.priority), float(r.created_at)))
        tasks = [rec for rec in self._tasks.values() if rec.session_uid == key]
        return sorted(tasks, key=lambda r: (-int(r.priority), float(r.created_at)))

    def get(self, task_id: str) -> Optional[ManagedTask]:
        self._prune_done()
        return self._tasks.get(str(task_id))

    def set_priority(self, task_id: str, priority: int) -> bool:
        rec = self.get(str(task_id))
        if rec is None:
            return False
        rec.priority = int(priority)
        return True

    def set_progress(self, task_id: str, *, progress: float, stage: str = "") -> bool:
        rec = self.get(str(task_id))
        if rec is None:
            return False
        try:
            value = float(progress)
        except Exception:
            value = 0.0
        rec.progress = max(0.0, min(1.0, value))
        if stage:
            rec.stage = str(stage)
        return True

    async def cancel(self, task_id: str, *, reason: str = "cancelled", timeout_s: float = 1.0) -> bool:
        rec = self._tasks.get(str(task_id))
        if rec is None:
            return False
        rec.token.cancel(reason=reason)
        rec.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(rec.task), timeout=timeout_s)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            if rec.task.done():
                self._tasks.pop(rec.task_id, None)
        return True

    async def cancel_session(
        self,
        session_id: Optional[str] = None,
        *,
        session_uid: Optional[str] = None,
        reason: str = "session_cancelled",
        timeout_s: float = 1.0,
    ) -> int:
        key = _resolve_task_session_key(session_id=session_id, session_uid=session_uid)
        if key is None:
            return 0
        targets = [rec for rec in self._tasks.values() if rec.session_uid == key]
        if not targets:
            return 0
        for rec in targets:
            rec.token.cancel(reason=reason)
            rec.task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(rec.task) for rec in targets), return_exceptions=True),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self.logger.exception("task cancellation timeout session_uid=%s", key)
        finally:
            for rec in targets:
                if rec.task.done():
                    self._tasks.pop(rec.task_id, None)
        return len(targets)

    async def cancel_session_tasks(
        self,
        session_id: Optional[str] = None,
        *,
        session_uid: Optional[str] = None,
        reason: str = "session_cancelled",
        timeout_s: float = 1.0,
    ) -> int:
        """
        Совместимый алиас для Desktop/Facade: отменяет все задачи, привязанные к session_id.
        Не должен бросать исключения наружу.
        """
        return await self.cancel_session(
            session_id=session_id,
            session_uid=session_uid,
            reason=str(reason),
            timeout_s=float(timeout_s),
        )

    def _prune_done(self) -> None:
        done_ids = [task_id for task_id, rec in self._tasks.items() if rec.task.done()]
        for task_id in done_ids:
            self._tasks.pop(task_id, None)
