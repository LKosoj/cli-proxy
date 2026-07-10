from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import ConfigService, SessionService, TaskService, ThemeService
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, TmuxRecoveryRequest
from desktop.services.application_facade import ApplicationFacade


@pytest.mark.asyncio
async def test_application_facade_shutdown_closes_sessions_and_marks_shutdown() -> None:
    config_service = MagicMock(spec=ConfigService)
    session_service = MagicMock(spec=SessionService)
    task_service = MagicMock(spec=TaskService)
    task_service.log_bus = MagicMock()
    task_service.list_active.return_value = []
    task_service.cancel = AsyncMock(return_value=True)
    ui_state_service = MagicMock()

    session_one = MagicMock()
    session_one.id = "s1"
    session_one._preserve_tmux_on_shutdown = False
    session_two = MagicMock()
    session_two.id = "s2"
    session_two._preserve_tmux_on_shutdown = False

    manager = MagicMock()
    manager.sessions_by_chat = {
        1: {"s1": session_one, "s2": session_two},
    }
    session_service._manager = manager

    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        theme_service=ThemeService(),
        ui_state_service=ui_state_service,
    )

    await facade.shutdown()

    assert facade._shutdown_in_progress is True
    session_one.interrupt.assert_called_once()
    session_one.close.assert_called_once_with(preserve_tmux=True)
    session_two.interrupt.assert_called_once()
    session_two.close.assert_called_once_with(preserve_tmux=True)
    assert manager.sessions_by_chat[1] == {"s1": session_one, "s2": session_two}
    assert session_one._preserve_tmux_on_shutdown is True
    assert session_two._preserve_tmux_on_shutdown is True
    manager._persist_sessions.assert_called_once()
    ui_state_service.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_application_facade_schedules_existing_tmux_recovery(monkeypatch) -> None:
    config_service = MagicMock(spec=ConfigService)
    session_service = MagicMock(spec=SessionService)
    task_service = MagicMock(spec=TaskService)
    task_service.log_bus = MagicMock()
    task_service.create.return_value = SimpleNamespace(task_id="recovery-task")
    session = SimpleNamespace(
        id="desktop-s1",
        busy=False,
        tool=SimpleNamespace(name="grok"),
        cli=SimpleNamespace(active_cli="grok"),
        conversation_scope=SimpleNamespace(session_uid="desktop:desktop-s1"),
        _tmux_recovery_request_id=None,
        _active_execution_backend="none",
    )
    session_service.list_desktop_sessions.return_value = [session]
    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        theme_service=ThemeService(),
    )
    recovery = TmuxRecoveryRequest(
        request_id="recover-grok",
        started_at=10.0,
        offset=0,
        prompt="original",
        dest={"kind": "desktop", "session_uid": "desktop:desktop-s1"},
    )

    monkeypatch.setattr("desktop.services.application_facade.get_session_execution_backend", lambda _session: "tmux")

    async def _get_recovery(_backend, _session):
        return recovery

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _get_recovery)

    assert await facade._recover_tmux_sessions() == 1
    assert session.busy is True
    assert session._active_execution_backend == "tmux"
    task_service.create.assert_called_once()
    assert task_service.create.call_args.kwargs["name"] == "tmux_recovery:recover-grok"


@pytest.mark.asyncio
async def test_desktop_tmux_recovery_failure_does_not_continue_queue(monkeypatch) -> None:
    config_service = MagicMock(spec=ConfigService)
    session_service = MagicMock(spec=SessionService)
    task_service = MagicMock(spec=TaskService)
    task_service.log_bus = MagicMock()
    task_service.create.return_value = SimpleNamespace(task_id="recovery-task")

    async def _recover(_request):
        return "recovered answer"

    session = SimpleNamespace(
        id="desktop-s1",
        busy=False,
        tool=SimpleNamespace(name="grok"),
        cli=SimpleNamespace(active_cli="grok"),
        conversation_scope=SimpleNamespace(session_uid="desktop:desktop-s1"),
        _tmux_recovery_request_id=None,
        _active_execution_backend="none",
        recover_tmux_request=_recover,
    )
    session_service.list_desktop_sessions.return_value = [session]
    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        theme_service=ThemeService(),
    )
    facade._schedule_queue_kick = MagicMock()
    facade.notify = MagicMock(side_effect=RuntimeError("desktop delivery unavailable"))
    recovery = TmuxRecoveryRequest(
        request_id="recover-grok",
        started_at=10.0,
        offset=0,
        prompt="original",
        dest={"kind": "desktop", "session_uid": "desktop:desktop-s1"},
    )

    monkeypatch.setattr("desktop.services.application_facade.get_session_execution_backend", lambda _session: "tmux")

    async def _get_recovery(_backend, _session):
        return recovery

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _get_recovery)
    marked = MagicMock(return_value=True)
    monkeypatch.setattr(TmuxExecutionBackend, "mark_request_delivered", marked)

    assert await facade._recover_tmux_sessions() == 1
    runner = task_service.create.call_args.kwargs["runner"]
    assert await runner(None) == ""

    marked.assert_not_called()
    facade._schedule_queue_kick.assert_not_called()
