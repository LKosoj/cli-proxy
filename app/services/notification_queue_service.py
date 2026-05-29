from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sessions.conversation_scope import ConversationScope


TResult = TypeVar("TResult")
NotificationFactory = Callable[[], Awaitable[TResult]]
ClockFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class _QueuedNotification:
    future: asyncio.Future[Any]
    operation: str
    scope: ConversationScope
    factory: NotificationFactory[Any]


class NotificationQueueService:
    """Serialize outbound notifications per ConversationScope."""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        *,
        min_interval_sec: float = 1.0,
        clock: Optional[ClockFn] = None,
        sleep: Optional[SleepFn] = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._min_interval_sec = max(0.0, float(min_interval_sec))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._last_delivery_ts_by_scope: dict[str, float] = {}
        self._queues: dict[str, asyncio.Queue[_QueuedNotification]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._active_scope_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "notification_queue_active_scope_key",
            default=None,
        )

    async def start(self) -> None:
        loop = self._ensure_runtime()
        if self._started:
            return
        self._started = True
        self._logger.info(
            "notification queue started loop_id=%s min_interval_sec=%.3f",
            id(loop),
            self._min_interval_sec,
        )

    async def enqueue(
        self,
        scope: ConversationScope,
        *,
        operation: str,
        factory: NotificationFactory[TResult],
    ) -> TResult:
        await self.start()
        loop = self._ensure_runtime()
        queue = self._ensure_scope_queue(scope)
        future: asyncio.Future[TResult] = loop.create_future()
        await queue.put(
            _QueuedNotification(
                future=future,
                operation=str(operation or "").strip() or "notification",
                scope=scope,
                factory=factory,
            )
        )
        return await future

    async def drain(self) -> None:
        if not self._started:
            return
        self._ensure_runtime()
        for queue in list(self._queues.values()):
            await queue.join()

    async def cancel_scope(self, session_uid: str) -> int:
        key = str(session_uid or "").strip()
        if not key:
            return 0
        self._ensure_runtime()
        worker = self._workers.pop(key, None)
        queue = self._queues.pop(key, None)
        self._last_delivery_ts_by_scope.pop(key, None)

        cancelled = 0
        if queue is not None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    if not item.future.done():
                        item.future.cancel()
                        cancelled += 1
                finally:
                    queue.task_done()
        if worker is not None:
            worker.cancel()
            cancelled += 1
            try:
                await asyncio.gather(worker, return_exceptions=True)
            except Exception:
                self._logger.exception("notification queue cancel_scope failed scope=%s", key)
        return cancelled

    async def shutdown(self) -> None:
        if not self._started and not self._workers and not self._queues:
            self._loop = None
            return
        workers = list(self._workers.values())
        queues = list(self._queues.values())
        self._started = False
        self._last_delivery_ts_by_scope = {}
        self._workers = {}
        self._queues = {}
        self._loop = None
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        for queue in queues:
            try:
                await asyncio.wait_for(queue.join(), timeout=2.0)
            except asyncio.TimeoutError:
                self._logger.warning("notification queue drain timed out during shutdown")

    def is_executing_scope(self, scope: ConversationScope) -> bool:
        key = str(getattr(scope, "session_uid", "") or "").strip()
        if not key:
            return False
        return self._active_scope_key.get() == key

    def _ensure_runtime(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError(
                "NotificationQueueService is bound to a different event loop; call shutdown() before reuse."
            )
        return loop

    def _ensure_scope_queue(self, scope: ConversationScope) -> asyncio.Queue[_QueuedNotification]:
        key = str(scope.session_uid or "").strip()
        queue = self._queues.get(key)
        if queue is not None:
            return queue
        queue = asyncio.Queue()
        self._queues[key] = queue
        self._workers[key] = asyncio.create_task(
            self._run_scope_queue(key, scope, queue),
            name=f"notification-queue:{key}",
        )
        return queue

    async def _apply_pacing(self, key: str, operation: str) -> None:
        if self._min_interval_sec <= 0:
            return
        last_delivery_ts = self._last_delivery_ts_by_scope.get(key)
        if last_delivery_ts is None:
            return
        now = self._clock()
        wait_sec = self._min_interval_sec - (now - last_delivery_ts)
        if wait_sec <= 0:
            return
        self._logger.info(
            "notification queue pacing scope=%s operation=%s wait_sec=%.3f",
            key,
            operation,
            wait_sec,
        )
        await self._sleep(wait_sec)

    async def _run_scope_queue(
        self,
        key: str,
        scope: ConversationScope,
        queue: asyncio.Queue[_QueuedNotification],
    ) -> None:
        try:
            while True:
                item = await queue.get()
                try:
                    await self._apply_pacing(key, item.operation)
                    # One queue item is an atomic delivery unit for a scope.
                    # Chunked Telegram sends must stay inside item.factory()
                    # so their parts cannot interleave with other producers.
                    token = self._active_scope_key.set(key)
                    try:
                        result = await item.factory()
                    finally:
                        self._active_scope_key.reset(token)
                except asyncio.CancelledError:
                    if not item.future.done():
                        item.future.cancel()
                    raise
                except Exception as exc:
                    if not item.future.done():
                        item.future.set_exception(exc)
                    self._logger.exception(
                        "notification queue failed scope=%s operation=%s",
                        key,
                        item.operation,
                    )
                else:
                    if not item.future.done():
                        item.future.set_result(result)
                finally:
                    self._last_delivery_ts_by_scope[key] = self._clock()
                    queue.task_done()
        except asyncio.CancelledError:
            self._logger.info("notification queue stopped scope=%s surface=%s", key, scope.session_surface)
            raise
