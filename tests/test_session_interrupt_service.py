import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from app.services.session_interrupt_service import SessionInterruptService
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend
from sessions.conversation_scope import ConversationScope


class _FakeChild:
    def __init__(self, *, alive: bool) -> None:
        self._alive = bool(alive)

    def isalive(self) -> bool:
        return self._alive


def _build_session(tmp_path, *, busy: bool = True):
    session = SimpleNamespace(
        id="s1",
        queue=deque([{"text": "queued"}]),
        busy=bool(busy),
        run_lock=asyncio.Lock(),
        current_proc=None,
        child=None,
        _active_execution_backend="none",
        conversation_scope=ConversationScope.from_parts(-100777000111, 101),
        tool=SimpleNamespace(name="dummy"),
        cli=SimpleNamespace(active_cli="dummy", execution_backends={}),
        config=SimpleNamespace(defaults=SimpleNamespace(state_path=str(tmp_path / "state.json"))),
        workdir=str(tmp_path),
        last_tick_ts=time.time(),
        last_tick_value="tick",
        tick_seen=3,
        runtime_progress_last_event={"phase": "run"},
        runtime_progress_events=[{"phase": "run"}],
        _runtime_progress_last_sig="sig",
        _runtime_progress_last_ts=time.time(),
    )

    def _interrupt() -> None:
        return None

    def _is_active_by_tick() -> bool:
        value = getattr(session, "last_tick_ts", None)
        return bool(value and (time.time() - float(value)) <= 3.0)

    session.interrupt = _interrupt
    session.is_active_by_tick = _is_active_by_tick
    return session


@pytest.mark.asyncio
async def test_session_interrupt_service_clears_session_runtime_and_reports_completed(tmp_path) -> None:
    pending_calls: list[tuple[int | None, int | None]] = []
    persisted: list[tuple[int, str]] = []
    media_clear = {"count": 0}

    async def _cancel_session(_session_uid: str, _timeout_s: float) -> int:
        return 2

    async def _cancel_notifications(_session_uid: str) -> int:
        return 3

    def _clear_buffer(_session, reply_chat_id: int) -> bool:
        return int(reply_chat_id) == -100777000111

    def _clear_media(_session) -> None:
        media_clear["count"] += 1

    def _clear_pending(_session, reply_chat_id, message_thread_id) -> int:
        pending_calls.append((reply_chat_id, message_thread_id))
        return 2

    def _persist(chat_id: int, session_id: str) -> None:
        persisted.append((int(chat_id), str(session_id)))

    session = _build_session(tmp_path)
    service = SessionInterruptService(
        cancel_session_tasks=_cancel_session,
        list_session_tasks=lambda _session_uid: [],
        clear_message_buffer=_clear_buffer,
        clear_media_groups=_clear_media,
        clear_pending_questions=_clear_pending,
        cancel_notification_scope=_cancel_notifications,
        persist_session=_persist,
    )

    report = await service.interrupt_session_runtime(
        session,
        owner_chat_id=77,
        reply_chat_id=-100777000111,
        message_thread_id=101,
        reason="test",
    )

    assert report.status == "completed"
    assert report.cancelled_task_count == 2
    assert report.cleared_queue is True
    assert report.cleared_message_buffer is True
    assert report.cleared_media_groups is True
    assert report.cleared_pending_questions == 2
    assert report.cleared_notifications == 3
    assert report.cleared_busy_signals is True
    assert list(session.queue) == []
    assert session.busy is False
    assert session.last_tick_ts is None
    assert session.last_tick_value is None
    assert session.tick_seen == 0
    assert session.runtime_progress_last_event is None
    assert session.runtime_progress_events == []
    assert pending_calls == [(-100777000111, 101)]
    assert media_clear["count"] == 1
    assert persisted == [(77, "s1")]


@pytest.mark.asyncio
async def test_session_interrupt_service_persists_even_with_empty_queue(tmp_path) -> None:
    persisted: list[tuple[int, str]] = []

    async def _cancel_session(_session_uid: str, _timeout_s: float) -> int:
        return 0

    def _persist(chat_id: int, session_id: str) -> None:
        persisted.append((int(chat_id), str(session_id)))

    session = _build_session(tmp_path)
    session.queue = deque()  # очередь пуста — нечего очищать

    service = SessionInterruptService(
        cancel_session_tasks=_cancel_session,
        persist_session=_persist,
    )

    report = await service.interrupt_session_runtime(
        session,
        owner_chat_id=77,
        reason="test_empty_queue",
    )

    # Несмотря на пустую очередь, состояние должно быть персистнуто, чтобы
    # свежезафиксированный resume_token пережил рестарт процесса.
    assert report.cleared_queue is False
    assert persisted == [(77, "s1")]


@pytest.mark.asyncio
async def test_session_interrupt_service_reports_partial_timeout_when_runtime_still_alive(tmp_path) -> None:
    async def _cancel_session(_session_uid: str, _timeout_s: float) -> int:
        return 1

    session = _build_session(tmp_path)
    await session.run_lock.acquire()
    session.child = _FakeChild(alive=True)
    session._active_execution_backend = "interactive"

    service = SessionInterruptService(
        cancel_session_tasks=_cancel_session,
        list_session_tasks=lambda _session_uid: ["orchestrator_final_send"],
    )

    report = await service.interrupt_session_runtime(
        session,
        owner_chat_id=None,
        reason="test_partial",
    )

    assert report.status == "partial_timeout"
    assert report.remaining_task_names == ("orchestrator_final_send",)
    assert report.backend_state == "interactive_active"
    assert report.cleared_busy_signals is False
    assert session.busy is True


@pytest.mark.asyncio
async def test_session_interrupt_service_treats_idle_tmux_as_completed(tmp_path) -> None:
    async def _cancel_session(_session_uid: str, _timeout_s: float) -> int:
        return 0

    session = _build_session(tmp_path)
    session.queue = deque()
    session.busy = False
    session.last_tick_ts = None
    session._active_execution_backend = "tmux"
    paths = TmuxExecutionBackend().paths(session)
    TmuxExecutionBackend._write_state(
        paths,
        {
            "state": "idle",
            "session_name": paths["session_name"],
            "pane_target": paths["pane_target"],
            "last_activity_at": time.time(),
        },
    )

    service = SessionInterruptService(
        cancel_session_tasks=_cancel_session,
        list_session_tasks=lambda _session_uid: [],
    )

    report = await service.interrupt_session_runtime(
        session,
        owner_chat_id=None,
        reason="test_tmux_idle",
    )

    assert report.status == "completed"
    assert report.backend_state == "tmux_idle"


@pytest.mark.asyncio
async def test_session_interrupt_service_treats_selected_unstarted_tmux_as_stopped(tmp_path) -> None:
    async def _cancel_session(_session_uid: str, _timeout_s: float) -> int:
        return 0

    session = _build_session(tmp_path)
    session.queue = deque()
    session.busy = False
    session.last_tick_ts = None
    session.tool.execution_backends = ["headless", "tmux"]
    session.tool.default_execution_backend = "tmux"

    service = SessionInterruptService(
        cancel_session_tasks=_cancel_session,
        list_session_tasks=lambda _session_uid: [],
    )

    report = await service.interrupt_session_runtime(
        session,
        owner_chat_id=None,
        reason="test_tmux_selected_unstarted",
    )

    assert report.status == "completed"
    assert report.backend_state == "stopped"
