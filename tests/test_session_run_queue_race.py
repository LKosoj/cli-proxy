"""
Unit tests for queue race condition fix in SessionRunService.

Verifies:
- Each queued item is processed exactly once in the normal flow.
- On task-start failure the item is rolled back into the queue head (not lost).
- FIFO order is preserved across multiple queued items.
"""
from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sessions.session_run_service import SessionRunService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(*, busy: bool = False) -> Any:
    session = SimpleNamespace(
        id="test-session",
        name="test",
        chat_id=42,
        busy=busy,
        started_at=0.0,
        last_output_ts=0.0,
        last_tick_ts=None,
        last_tick_value=None,
        last_assistant_text_ts=None,
        last_assistant_text_value=None,
        tick_seen=0,
        assistant_preview_message_id=None,
        assistant_preview_last_value=None,
        headless_forced_stop=None,
        run_lock=asyncio.Lock(),
        send_lock=asyncio.Lock(),
        queue=deque(),
        config=None,
        conversation_scope=None,
        state_summary=None,
        state_updated_at=None,
    )
    return session


def _make_service(
    *,
    tasks: list | None = None,
    start_task_ok: bool = True,
) -> SessionRunService:
    if tasks is None:
        tasks = []

    created_coros: list = []

    def _mode_tasks_create(*, session_id, mode_id, coro, name):
        created_coros.append((name, coro))
        tasks.append(name)
        coro.close()  # prevent ResourceWarning; tests only verify scheduling, not execution

    def _mode_tasks_list(*, session_id, mode_id):
        return []

    bot_app = MagicMock()
    bot_app._shutdown_in_progress = False
    # Set config to None so hook_config resolves to None and the hook path is skipped.
    bot_app.config = None

    svc = SessionRunService(
        bot_app=bot_app,
        persist_sessions=MagicMock(),
        persist_session=None,
        mode_tasks_list=_mode_tasks_list,
        mode_tasks_create=_mode_tasks_create,
        log_cli_dialog=MagicMock(),
        reset_session_fields_like_sessions_reset=MagicMock(),
    )
    # Expose created_coros for inspection
    svc._test_created_coros = created_coros  # type: ignore[attr-defined]
    return svc


# ---------------------------------------------------------------------------
# Test 1 — normal flow: item processed exactly once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_item_processed_exactly_once_on_normal_flow() -> None:
    """
    After run_prompt finishes normally, the single queued item is popped
    and a follow-up task is scheduled — item should NOT remain in the queue.
    """
    session = _make_session()
    tasks_started: list[str] = []
    svc = _make_service(tasks=tasks_started)

    queue_item = {"text": "queued-prompt", "dest": {"kind": "telegram", "chat_id": 42}}
    session.queue.append(queue_item)

    dest = {"kind": "telegram", "chat_id": 42}

    with (
        patch("sessions.session_run_service.get_task_bearing_cli_hook_service", return_value=None),
        patch("sessions.session_run_service.switch_session_active_cli_if_needed", return_value=SimpleNamespace(switched=False)),
        patch("sessions.session_run_service.consume_session_cli_switch_notice_text", return_value=None),
        patch("sessions.session_run_service.clear_session_ticks"),
        patch("sessions.session_run_service.clear_runtime_progress"),
        patch("sessions.session_run_service.assistant_preview_enabled", return_value=False),
        patch("sessions.session_run_service.assistant_preview_supported_dest", return_value=False),
        patch("sessions.session_run_service.bind_session_log_context") as mock_ctx,
    ):
        mock_ctx.return_value.__enter__ = MagicMock(return_value=None)
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Patch session.run_prompt to return quickly
        session.run_prompt = AsyncMock(return_value="done output")  # type: ignore[attr-defined]

        # _send_output_task would schedule another task; keep it simple
        svc.bot_app.send_output = AsyncMock()

        await svc.run_prompt(session, "first-prompt", dest, context=object())

    # The queue should be empty (item was popped)
    assert len(session.queue) == 0, f"Queue should be empty, got: {list(session.queue)}"

    # A follow-up task was scheduled
    followup_names = [n for n in tasks_started if "queue_next" in n]
    assert len(followup_names) == 1, f"Expected exactly one queue_next task, got: {tasks_started}"


# ---------------------------------------------------------------------------
# Test 2 — rollback: item returned to queue head on task-start rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_item_rolled_back_when_task_start_rejected() -> None:
    """
    If start_prompt_task returns False (e.g. shutting down), the item must be
    rolled back to the front of the queue so it is not lost.
    """
    session = _make_session()
    svc = _make_service()

    queue_item = {"text": "queued-prompt", "dest": {"kind": "telegram", "chat_id": 42}}
    session.queue.append(queue_item)

    dest = {"kind": "telegram", "chat_id": 42}

    with (
        patch("sessions.session_run_service.get_task_bearing_cli_hook_service", return_value=None),
        patch("sessions.session_run_service.switch_session_active_cli_if_needed", return_value=SimpleNamespace(switched=False)),
        patch("sessions.session_run_service.consume_session_cli_switch_notice_text", return_value=None),
        patch("sessions.session_run_service.clear_session_ticks"),
        patch("sessions.session_run_service.clear_runtime_progress"),
        patch("sessions.session_run_service.assistant_preview_enabled", return_value=False),
        patch("sessions.session_run_service.assistant_preview_supported_dest", return_value=False),
        patch("sessions.session_run_service.bind_session_log_context") as mock_ctx,
    ):
        mock_ctx.return_value.__enter__ = MagicMock(return_value=None)
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        session.run_prompt = AsyncMock(return_value="done output")  # type: ignore[attr-defined]
        svc.bot_app.send_output = AsyncMock()

        # Force start_prompt_task to return False (simulate shutdown rejection)
        with patch.object(svc, "start_prompt_task", return_value=False):
            await svc.run_prompt(session, "first-prompt", dest, context=object())

    # Item must be back in queue (rolled back via appendleft)
    assert len(session.queue) == 1, f"Queue should have 1 item after rollback, got: {list(session.queue)}"
    assert session.queue[0] == queue_item


# ---------------------------------------------------------------------------
# Test 3 — FIFO order preserved for multiple queued items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_fifo_order_preserved() -> None:
    """
    After processing, the next item dispatched is the one at the front of the
    queue (FIFO), and remaining items stay in original order.
    """
    session = _make_session()
    tasks_started: list[str] = []
    svc = _make_service(tasks=tasks_started)

    items = [
        {"text": f"msg-{i}", "dest": {"kind": "telegram", "chat_id": 42}}
        for i in range(3)
    ]
    for item in items:
        session.queue.append(item)

    dest = {"kind": "telegram", "chat_id": 42}

    with (
        patch("sessions.session_run_service.get_task_bearing_cli_hook_service", return_value=None),
        patch("sessions.session_run_service.switch_session_active_cli_if_needed", return_value=SimpleNamespace(switched=False)),
        patch("sessions.session_run_service.consume_session_cli_switch_notice_text", return_value=None),
        patch("sessions.session_run_service.clear_session_ticks"),
        patch("sessions.session_run_service.clear_runtime_progress"),
        patch("sessions.session_run_service.assistant_preview_enabled", return_value=False),
        patch("sessions.session_run_service.assistant_preview_supported_dest", return_value=False),
        patch("sessions.session_run_service.bind_session_log_context") as mock_ctx,
    ):
        mock_ctx.return_value.__enter__ = MagicMock(return_value=None)
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        session.run_prompt = AsyncMock(return_value="done")  # type: ignore[attr-defined]
        svc.bot_app.send_output = AsyncMock()

        await svc.run_prompt(session, "initial-prompt", dest, context=object())

    # Exactly one item was dequeued (the first one)
    assert len(session.queue) == 2, f"Expected 2 remaining, got: {list(session.queue)}"
    # Remaining items preserve FIFO order (msg-1, msg-2)
    remaining_texts = [q["text"] for q in list(session.queue)]
    assert remaining_texts == ["msg-1", "msg-2"], f"FIFO violated: {remaining_texts}"

    # A queue_next task was scheduled for msg-0
    followup_coroutines = [
        name for name, _coro in svc._test_created_coros  # type: ignore[attr-defined]
        if "queue_next" in name
    ]
    assert len(followup_coroutines) == 1


@pytest.mark.asyncio
async def test_cli_switch_failure_resets_busy_and_keeps_queue() -> None:
    session = _make_session()
    queue_item = {"text": "queued-prompt", "dest": {"kind": "telegram", "chat_id": 42}}
    session.queue.append(queue_item)
    svc = _make_service()
    svc.bot_app._send_message = AsyncMock()

    with (
        patch(
            "sessions.session_run_service.switch_session_active_cli_if_needed",
            new=AsyncMock(side_effect=RuntimeError("tmux close failed")),
        ),
        patch("sessions.session_run_service.clear_session_ticks"),
        patch("sessions.session_run_service.clear_runtime_progress"),
        patch("sessions.session_run_service.bind_session_log_context") as mock_ctx,
    ):
        mock_ctx.return_value.__enter__ = MagicMock(return_value=None)
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        session.run_prompt = AsyncMock(return_value="must not run")  # type: ignore[attr-defined]

        await svc.run_prompt(
            session,
            "first-prompt",
            {"kind": "telegram", "chat_id": 42},
            context=object(),
        )

    assert session.busy is False
    assert list(session.queue) == [queue_item]
    session.run_prompt.assert_not_awaited()
    svc.bot_app._send_message.assert_awaited_once()
