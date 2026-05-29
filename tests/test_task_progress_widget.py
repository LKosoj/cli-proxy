import pytest
from unittest.mock import MagicMock
from desktop.widgets.task_progress import TaskProgressWidget
from desktop.services.application_facade import AppNotification


@pytest.fixture
def mock_facade():
    facade = MagicMock()
    # Mock subscribe to capture the callback
    facade.subscribe = MagicMock(return_value=lambda: None)
    return facade


def test_task_progress_widget_initial_state(qtbot, mock_facade):
    """Проверка начального состояния виджета."""
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    assert widget.title_label.isHidden()
    assert len(widget._task_containers) == 0


def test_task_progress_widget_handles_task_started(qtbot, mock_facade):
    """Проверка реакции на начало задачи."""
    # Capture callback
    callback = None

    def save_callback(cb):
        nonlocal callback
        callback = cb
        return lambda: None
    mock_facade.subscribe.side_effect = save_callback

    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    # Simulate task:started
    notification = AppNotification(event="task:started", payload={"task_id": "t1", "name": "Test Task"})
    callback(notification)

    assert not widget.title_label.isHidden()
    assert "t1" in widget._task_containers
    assert widget._task_labels["t1"].text() == "Test Task"
    assert widget._task_bars["t1"].value() == 0


def test_task_progress_widget_handles_task_updated(qtbot, mock_facade):
    """Проверка обновления прогресса и стадии."""
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    # Access the stored callback directly if possible, or just call _on_facade_notification
    widget._on_facade_notification(AppNotification(event="task:started", payload={"task_id": "t1", "name": "Test Task"}))

    # Update progress
    widget._on_facade_notification(AppNotification(
        event="task:updated",
        payload={"task_id": "t1", "progress": 0.5, "stage": "processing"}
    ))

    assert widget._task_bars["t1"].value() == 50
    assert "processing" in widget._task_labels["t1"].text()


def test_task_progress_widget_handles_task_completion(qtbot, mock_facade):
    """Проверка удаления задачи при завершении."""
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    widget._on_facade_notification(AppNotification(event="task:started", payload={"task_id": "t1", "name": "Test Task"}))
    assert "t1" in widget._task_containers

    # Complete
    widget._on_facade_notification(AppNotification(event="task:completed", payload={"task_id": "t1"}))
    assert "t1" not in widget._task_containers
    assert widget.title_label.isHidden()


def test_task_progress_widget_handles_task_failure(qtbot, mock_facade):
    """Проверка удаления задачи при ошибке."""
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    widget._on_facade_notification(AppNotification(event="task:started", payload={"task_id": "t1", "name": "Task"}))
    assert "t1" in widget._task_containers

    # Fail
    widget._on_facade_notification(AppNotification(event="task:failed", payload={"task_id": "t1", "error": "err"}))
    assert "t1" not in widget._task_containers
    assert widget.title_label.isHidden()


def test_task_progress_widget_handles_task_cancellation(qtbot, mock_facade):
    """Проверка удаления задачи при отмене."""
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    widget._on_facade_notification(AppNotification(event="task:started", payload={"task_id": "t1", "name": "Task"}))
    assert "t1" in widget._task_containers

    # Cancel
    widget._on_facade_notification(AppNotification(event="task:cancelled", payload={"task_id": "t1", "reason": "user"}))
    assert "t1" not in widget._task_containers
    assert widget.title_label.isHidden()


def test_task_progress_widget_handles_multiple_tasks(qtbot, mock_facade):
    """Проверка работы с несколькими задачами одновременно."""
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    widget._on_facade_notification(AppNotification(event="task:started", payload={"task_id": "t1", "name": "Task 1"}))
    widget._on_facade_notification(AppNotification(event="task:started", payload={"task_id": "t2", "name": "Task 2"}))

    assert len(widget._task_containers) == 2
    assert not widget.title_label.isHidden()

    widget._on_facade_notification(AppNotification(event="task:completed", payload={"task_id": "t1"}))
    assert len(widget._task_containers) == 1
    assert not widget.title_label.isHidden()

    widget._on_facade_notification(AppNotification(event="task:failed", payload={"task_id": "t2"}))
    assert len(widget._task_containers) == 0
    assert widget.title_label.isHidden()
