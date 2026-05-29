import pytest
import types
from unittest.mock import MagicMock
from desktop.widgets.mode_panel import ModePanelWidget
from desktop.services.application_facade import ApplicationFacade, AppNotification
from desktop.services.theme_service import ThemeService


@pytest.fixture
def mock_facade():
    facade = MagicMock(spec=ApplicationFacade)
    facade.list_modes.return_value = ["mode1", "mode2"]
    facade.subscribe = MagicMock(return_value=lambda: None)
    facade.task_service = MagicMock()
    facade.task_service.list_active.return_value = []
    facade.theme_service = ThemeService()
    return facade


@pytest.fixture
def mock_session():
    return types.SimpleNamespace(
        id="test-session",
        conversation_scope=types.SimpleNamespace(session_uid="desktop:test-session"),
        modes=types.SimpleNamespace(active_mode="mode1", analyst_mode="spec"),
        active_cli="qwen",
        busy=False,
    )


def test_mode_panel_initial_load(qtbot, mock_facade):
    """Проверка начальной загрузки списка режимов."""
    panel = ModePanelWidget(mock_facade)
    qtbot.addWidget(panel)

    assert panel.mode_combo.count() == 3  # None + mode1 + mode2
    assert panel.mode_combo.itemText(0) == "None"
    assert panel.mode_combo.itemText(1) == "mode1"
    assert panel.mode_combo.itemText(2) == "mode2"


def test_mode_panel_set_session(qtbot, mock_facade, mock_session):
    """Проверка обновления панели при установке сессии."""
    panel = ModePanelWidget(mock_facade)
    qtbot.addWidget(panel)

    panel.set_session(mock_session)

    assert panel.isEnabled()
    assert panel.mode_combo.currentText() == "mode1"
    assert "CLI: qwen" in panel.cli_label.text()
    assert panel.status_text.text() == "Idle"


def test_mode_panel_change_mode_user(qtbot, mock_facade, mock_session):
    """Проверка смены режима пользователем через facade."""
    panel = ModePanelWidget(mock_facade, chat_id=123)
    qtbot.addWidget(panel)
    panel.set_session(mock_session)

    # Эмулируем выбор режима пользователем
    panel.mode_combo.setCurrentText("mode2")

    mock_facade.set_session_mode.assert_called_once_with("desktop:test-session", "mode2")


def test_mode_panel_disable_mode_user(qtbot, mock_facade, mock_session):
    """Проверка выключения режима пользователем (выбор 'None')."""
    panel = ModePanelWidget(mock_facade, chat_id=123)
    qtbot.addWidget(panel)
    panel.set_session(mock_session)

    # Эмулируем выбор 'None'
    panel.mode_combo.setCurrentText("None")

    mock_facade.set_session_mode.assert_called_once_with("desktop:test-session", None)


def test_mode_panel_status_updates(qtbot, mock_facade, mock_session):
    """Проверка обновления статуса (Idle/Working) по уведомлениям фасада."""
    # Сохраняем коллбэк подписки
    subscribe_callback = None

    def mock_subscribe(callback):
        nonlocal subscribe_callback
        subscribe_callback = callback
        return lambda: None

    mock_facade.subscribe.side_effect = mock_subscribe

    panel = ModePanelWidget(mock_facade)
    qtbot.addWidget(panel)
    panel.set_session(mock_session)

    assert subscribe_callback is not None

    # Simulate task started
    subscribe_callback(AppNotification(event="task:started", payload={"session_uid": "desktop:test-session"}))
    assert panel.status_text.text() == "Working"

    # Simulate task completed
    subscribe_callback(AppNotification(event="task:completed", payload={"session_uid": "desktop:test-session"}))
    assert panel.status_text.text() == "Completed"


def test_mode_panel_menu_button_toggles_open_close(qtbot, mock_facade, mock_session):
    """Повторный клик по Menu закрывает уже открытое меню."""
    subscribe_callback = None

    def mock_subscribe(callback):
        nonlocal subscribe_callback
        subscribe_callback = callback
        return lambda: None

    mock_facade.subscribe.side_effect = mock_subscribe

    panel = ModePanelWidget(mock_facade, chat_id=123)
    qtbot.addWidget(panel)
    panel.set_session(mock_session)

    # В синхронном тесте нет running loop; имитируем успешное планирование async-вызова.
    mock_facade.show_mode_menu = MagicMock(return_value=None)
    panel._schedule_async = MagicMock(side_effect=lambda coro_factory: coro_factory())

    panel._request_mode_menu()
    mock_facade.show_mode_menu.assert_called_once_with("desktop:test-session")

    subscribe_callback(
            AppNotification(
                event="ui:mode_menu",
                payload={"session_uid": "desktop:test-session", "text": "MENU", "rows": [[{"text": "A", "data": "x"}]]},
            )
        )

    panel._request_mode_menu()
    mock_facade.notify.assert_called_with("ui:mode_menu", session_uid="desktop:test-session", text="", rows=[])
