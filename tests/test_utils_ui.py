from __future__ import annotations

import asyncio
import inspect

from utils.ui import ensure_async


async def _sample_coro() -> str:
    return "ok"


def test_ensure_async_closes_coroutine_when_no_event_loop(monkeypatch) -> None:
    coro = _sample_coro()

    def _raise_runtime_error():
        raise RuntimeError("no loop")

    monkeypatch.setattr(asyncio, "get_running_loop", _raise_runtime_error)
    monkeypatch.setattr(asyncio, "get_event_loop", _raise_runtime_error)

    task = ensure_async(coro)

    assert task is None
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_ensure_async_closes_coroutine_when_create_task_fails(monkeypatch) -> None:
    coro = _sample_coro()

    class _BrokenLoop:
        def create_task(self, _coro):
            raise RuntimeError("boom")

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _BrokenLoop())

    task = ensure_async(coro)

    assert task is None
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_ensure_async_closes_coroutine_when_event_loop_is_not_running(monkeypatch) -> None:
    coro = _sample_coro()

    class _DormantLoop:
        def is_running(self) -> bool:
            return False

        def create_task(self, _coro):
            raise AssertionError("create_task should not be called for a dormant loop")

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError("no running loop")))
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: _DormantLoop())

    task = ensure_async(coro)

    assert task is None
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
