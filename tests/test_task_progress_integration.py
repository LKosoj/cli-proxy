import pytest
from unittest.mock import MagicMock
from desktop.widgets.task_progress import TaskProgressWidget
from desktop.services.application_facade import ApplicationFacade, AppNotification


@pytest.fixture
def mock_facade():
    facade = MagicMock(spec=ApplicationFacade)
    # Store the callback to simulate notifications
    facade._cb = None

    def subscribe(cb):
        facade._cb = cb
        return lambda: None
    facade.subscribe.side_effect = subscribe
    return facade


def test_task_progress_facade_integration(qtbot, mock_facade):
    """
    Проверка полной интеграции с фасадом: от начала задачи до завершения.
    Гарантирует, что виджет правильно реагирует на последовательность сигналов.
    """
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    assert mock_facade._cb is not None

    # 1. Задача началась
    mock_facade._cb(AppNotification(
        event="task:started",
        payload={"task_id": "task_1", "name": "Deep Research"}
    ))

    assert not widget.title_label.isHidden()
    assert "task_1" in widget._task_containers
    assert widget._task_labels["task_1"].text() == "Deep Research"

    # 2. Прогресс обновился
    mock_facade._cb(AppNotification(
        event="task:updated",
        payload={"task_id": "task_1", "progress": 0.42, "stage": "analyzing"}
    ))

    assert widget._task_bars["task_1"].value() == 42
    assert "analyzing" in widget._task_labels["task_1"].text()

    # 3. Задача завершилась (failed)
    mock_facade._cb(AppNotification(
        event="task:failed",
        payload={"task_id": "task_1", "error": "API limit reached"}
    ))

    assert "task_1" not in widget._task_containers
    assert widget.title_label.isHidden()


def test_task_progress_session_filtering(qtbot, mock_facade):
    """
    Проверка фильтрации задач по сессии.
    """
    widget = TaskProgressWidget(mock_facade)
    qtbot.addWidget(widget)

    # Устанавливаем активную сессию s1
    widget.set_session_id("s1")

    # 1. Задача из s1 - должна появиться
    mock_facade._cb(AppNotification(
        event="task:started",
        payload={"task_id": "t1", "session_id": "s1", "name": "Task S1"}
    ))
    assert "t1" in widget._task_containers

    # 2. Задача из s2 - должна быть проигнорирована
    mock_facade._cb(AppNotification(
        event="task:started",
        payload={"task_id": "t2", "session_id": "s2", "name": "Task S2"}
    ))
    assert "t2" not in widget._task_containers

    # 3. Переключаем на s2 - старые должны исчезнуть
    widget.set_session_id("s2")
    assert "t1" not in widget._task_containers

    # 4. Теперь задача из s2 должна появиться
    mock_facade._cb(AppNotification(
        event="task:started",
        payload={"task_id": "t2", "session_id": "s2", "name": "Task S2"}
    ))
    assert "t2" in widget._task_containers
