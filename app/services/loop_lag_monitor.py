"""Детектор блокировок event loop.

Асинхронный heartbeat отмечает, что цикл событий жив. Сторожевой поток замечает
пропущенные отметки и снимает стек главного потока — пока блокирующий код ещё
выполняется. Снимать стек из самого heartbeat бесполезно: он получает управление
только после того, как блокировка закончилась, и виновника в стеке уже нет.
"""

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

LOOP_LAG_TICK_INTERVAL_SEC = 0.5
LOOP_LAG_WARN_THRESHOLD_SEC = 1.0
LOOP_LAG_REPEAT_SEC = 5.0


class LoopLagMonitor:
    """Логирует стек главного потока, когда event loop не отвечает дольше порога."""

    def __init__(
        self,
        *,
        tick_interval_sec: float = LOOP_LAG_TICK_INTERVAL_SEC,
        warn_threshold_sec: float = LOOP_LAG_WARN_THRESHOLD_SEC,
        repeat_sec: float = LOOP_LAG_REPEAT_SEC,
    ) -> None:
        self._tick_interval_sec = max(0.01, float(tick_interval_sec))
        self._warn_threshold_sec = max(self._tick_interval_sec, float(warn_threshold_sec))
        self._repeat_sec = max(self._warn_threshold_sec, float(repeat_sec))
        self._last_tick = time.monotonic()
        self._loop_thread_id = threading.get_ident()
        self._stop_event = threading.Event()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._watchdog: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._heartbeat_task is not None:
            return
        self._last_tick = time.monotonic()
        self._loop_thread_id = threading.get_ident()
        self._stop_event.clear()
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="loop_lag_heartbeat")
        self._watchdog = threading.Thread(
            target=self._watch,
            name="loop-lag-watchdog",
            daemon=True,
        )
        self._watchdog.start()
        logger.info(
            "loop lag monitor started threshold=%.1fs tick=%.2fs",
            self._warn_threshold_sec,
            self._tick_interval_sec,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.join(timeout=max(1.0, self._tick_interval_sec * 4))

    async def _heartbeat(self) -> None:
        while not self._stop_event.is_set():
            self._last_tick = time.monotonic()
            await asyncio.sleep(self._tick_interval_sec)

    def _watch(self) -> None:
        reported_at: Optional[float] = None
        while not self._stop_event.wait(self._tick_interval_sec):
            now = time.monotonic()
            lag = now - self._last_tick
            if lag < self._warn_threshold_sec:
                reported_at = None
                continue
            if reported_at is not None and now - reported_at < self._repeat_sec:
                continue
            reported_at = now
            logger.warning(
                "event loop заблокирован %.1fs, стек главного потока:\n%s",
                lag,
                self._loop_thread_stack(),
            )

    def _loop_thread_stack(self) -> str:
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return "<стек потока event loop недоступен>"
        return "".join(traceback.format_stack(frame))
