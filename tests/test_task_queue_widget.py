import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from desktop.widgets.task_queue import TaskQueueWidget


@pytest.fixture
def mock_facade():
    facade = MagicMock()
    facade.list_active_tasks.return_value = []
    facade.subscribe.side_effect = lambda cb: (lambda: None)
    facade.set_task_priority.return_value = True
    facade.cancel_task = MagicMock()
    return facade


@pytest.mark.asyncio
async def test_task_queue_widget_renders_active_tasks(qtbot, mock_facade):
    rec1 = SimpleNamespace(
        task_id="abc123456",
        name="task-a",
        session_id="s1",
        priority=2,
        progress=0.4,
        stage="planning",
        created_at=0.0,
    )
    rec2 = SimpleNamespace(
        task_id="def987654",
        name="task-b",
        session_id="s1",
        priority=0,
        progress=0.9,
        stage="review",
        created_at=0.0,
    )
    mock_facade.list_active_tasks.return_value = [rec1, rec2]

    widget = TaskQueueWidget(mock_facade, session_uid="s1")
    qtbot.addWidget(widget)

    assert "2" in widget.summary_label.text()
    assert widget.rows_layout.count() >= 3  # 2 rows + stretch
    mock_facade.list_active_tasks.assert_called_with(session_uid="s1")


@pytest.mark.asyncio
async def test_task_queue_widget_priority_and_cancel_actions(qtbot, mock_facade):
    rec = SimpleNamespace(
        task_id="abc123456",
        name="task-a",
        session_id="s1",
        priority=1,
        progress=0.0,
        stage="",
        created_at=0.0,
    )
    mock_facade.list_active_tasks.return_value = [rec]

    widget = TaskQueueWidget(mock_facade, session_uid="s1")
    qtbot.addWidget(widget)

    # Directly invoke handlers to avoid brittle widget tree queries.
    widget._on_priority_changed("abc123456", 5)
    mock_facade.set_task_priority.assert_called_with("abc123456", 5)

    async def _cancel_task(*args, **kwargs):
        return True

    mock_facade.cancel_task = AsyncMock(side_effect=_cancel_task)

    def _ensure_async(coro, parent=None):
        return asyncio.get_running_loop().create_task(coro)

    with patch("desktop.widgets.task_queue.ensure_async", side_effect=_ensure_async):
        widget._on_cancel_clicked("abc123456")
        await asyncio.sleep(0)

    assert mock_facade.cancel_task.await_count == 1
