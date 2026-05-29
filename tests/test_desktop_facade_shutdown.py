from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services import ConfigService, SessionService, TaskService, ThemeService
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
    session_two = MagicMock()
    session_two.id = "s2"

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
    session_one.close.assert_called_once()
    session_two.interrupt.assert_called_once()
    session_two.close.assert_called_once()
    assert manager.sessions_by_chat[1] == {}
    manager._persist_sessions.assert_called_once()
    ui_state_service.shutdown.assert_called_once()
