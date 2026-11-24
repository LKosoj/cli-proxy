import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.input_dispatch_models import DIRECT_TMUX_DEST_KEY
from app.services.input_dispatch_service import InputDispatchService
from modes.sdk.services.input_routing import ModeInputRoutingService
from modes.sdk.services.tasks import TaskService
from session import session_runtime_uid
from sessions.session_run_service import SessionRunService


_DEST = {"kind": "telegram", "chat_id": 42, "message_thread_id": 7}


def _setup(*raw_items, active_mode="", mode=None):
    queue = deque({"text": text, "dest": dict(_DEST), **fields} for text, fields in raw_items)
    session = SimpleNamespace(
        id="s1", chat_id=42, queue=queue, busy=False, run_lock=asyncio.Lock(),
        active_mode=active_mode, is_active_by_tick=lambda: True, tool=SimpleNamespace(name="codex"),
    )
    tasks = TaskService()
    registry = SimpleNamespace(get_active_mode_id=lambda _session: active_mode, get=lambda _mode: mode)
    app = SimpleNamespace(
        _shutdown_in_progress=False, config=SimpleNamespace(defaults=SimpleNamespace()),
        mode_tasks=tasks, mode_input_router=ModeInputRoutingService(mode_registry=registry), mode_registry=registry,
    )
    ui = SimpleNamespace(send_text=AsyncMock(), send_document=AsyncMock(return_value=True))
    dispatch = InputDispatchService(app, pending_input_ui=ui)
    app.input_dispatch_service = dispatch
    service = SessionRunService(
        bot_app=app, persist_sessions=Mock(), mode_tasks_list=tasks.list, mode_tasks_create=tasks.create,
        log_cli_dialog=Mock(), reset_session_fields_like_sessions_reset=Mock(),
    )
    service.start_prompt_task = Mock(return_value=True)
    app.session_management = service
    return app, session, service, ui


def _task(app, session):
    return app.mode_tasks.tasks[(session_runtime_uid(session), "__session__")][0].task


def _begin(app, session, service):
    assert service.start_queue_task(session, _DEST, object())
    return _task(app, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_tmux", [False, True])
async def test_queued_file_is_delivered_and_next_item_uses_normal_cli_dispatch(tmp_path, live_tmux):
    artifact = tmp_path / "report.txt"
    artifact.write_bytes(b"file contents")
    app, session, service, ui = _setup(
        ("скинь /tmp/report.txt", {}),
        ("изучи изображение", {"image_paths": ["/tmp/in.png"]}),
        ("запрос после CLI", {}),
    )
    app.artifact_intent_service = SimpleNamespace(
        classify=AsyncMock(return_value=SimpleNamespace(file_pattern="report.txt", confidence=1)),
        resolve=Mock(return_value=SimpleNamespace(resolved_path=artifact, error=None)),
    )
    app.orchestrator_chat_completion = object()
    app.input_dispatch_service._live_tmux_direct_available = AsyncMock(return_value=live_tmux)
    await _begin(app, session, service)

    document = ui.send_document.await_args.kwargs
    assert document["document"].read() == b"file contents"
    assert document["dest"]["message_thread_id"] == 7
    service.start_prompt_task.assert_called_once()
    _, text, dest, _context = service.start_prompt_task.call_args.args
    assert text == "изучи изображение"
    assert dest["image_paths"] == ["/tmp/in.png"]
    assert dest["cleanup_images"] is True
    assert bool(dest.get(DIRECT_TMUX_DEST_KEY)) is live_tmux
    assert [item["text"] for item in session.queue] == ["запрос после CLI"]


@pytest.mark.asyncio
async def test_queue_continues_after_reported_direct_cli_denial():
    app, session, service, ui = _setup(("denied", {}), ("allowed", {}))
    session.queue[0]["dest"]["user_id"] = 17
    app.access_policy_service = SimpleNamespace(is_direct_cli_allowed_for_chat=lambda user_id: user_id != 17)
    await _begin(app, session, service)

    ui.send_text.assert_awaited_once()
    service.start_prompt_task.assert_called_once()
    assert service.start_prompt_task.call_args.args[1] == "allowed"
    assert not session.queue


@pytest.mark.asyncio
async def test_queue_retains_input_while_asking_for_interactive_choice():
    app, session, service, ui = _setup(("waits for choice", {}), ("next", {}))
    dispatch = app.input_dispatch_service
    dispatch.choice_menu_blocking_text = AsyncMock(return_value="answer options")
    dispatch.show_choice_buttons = AsyncMock()
    await _begin(app, session, service)

    ui.send_text.assert_awaited_once()
    dispatch.show_choice_buttons.assert_awaited_once()
    service.start_prompt_task.assert_not_called()
    assert [item["text"] for item in session.queue] == ["waits for choice", "next"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancelled_queue_dispatch_restores_input_only_during_shutdown(shutdown):
    app, session, service, _ui = _setup(("queued", {}), ("next", {}))
    waiting = asyncio.Event()

    async def ready(**kwargs):
        waiting.set()
        await asyncio.Event().wait()

    app.input_dispatch_service.dispatch_ready_input = ready
    task = _begin(app, session, service)
    await waiting.wait()
    app._shutdown_in_progress = shutdown
    if not shutdown:
        session.queue.clear()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [item["text"] for item in session.queue] == (["queued", "next"] if shutdown else [])
    assert session._queue_dispatch_task is None


@pytest.mark.asyncio
async def test_queue_dispatch_cancelled_before_start_leaves_queue_untouched():
    app, session, service, _ui = _setup(("queued", {}))
    task = _begin(app, session, service)
    assert not service.start_queue_task(session, _DEST, object())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [item["text"] for item in session.queue] == ["queued"]


@pytest.mark.asyncio
async def test_mode_completion_drains_queued_images_through_normal_mode_analysis():
    mode = SimpleNamespace(
        handle_input=AsyncMock(), framework_sends_output=lambda: False,
        run_pipeline=AsyncMock(return_value="completed"),
    )
    mode.handle_input.return_value = None
    app, session, service, _ui = _setup(
        ("изучи изображение", {"image_paths": ["/tmp/in.png"]}), ("следующий запрос", {}),
        active_mode="agent", mode=mode,
    )
    app.mode_input_router._run_mode_image_analysis = AsyncMock(return_value=("codex", "описание изображения"))
    await service.run_mode_pipeline(session, "current", _DEST, object(), mode_id="agent")
    await _task(app, session)

    analysis = app.mode_input_router._run_mode_image_analysis.await_args.kwargs
    assert analysis["image_paths"] == ["/tmp/in.png"]
    assert "описание изображения" in mode.handle_input.await_args.args[0].text
    service.start_prompt_task.assert_not_called()
    assert [item["text"] for item in session.queue] == ["следующий запрос"]
