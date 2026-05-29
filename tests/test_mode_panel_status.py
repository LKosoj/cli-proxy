import types

import pytest
from unittest.mock import MagicMock
from desktop.widgets.mode_panel import ModePanelWidget
from desktop.services.application_facade import ApplicationFacade, AppNotification
from desktop.services.theme_service import ThemeService
from session import session_runtime_uid


@pytest.fixture
def mock_facade():
    facade = MagicMock(spec=ApplicationFacade)
    facade.list_modes.return_value = ["mode1"]
    facade.subscribe = MagicMock(return_value=lambda: None)
    facade.task_service = MagicMock()
    facade.task_service.list_active.return_value = []
    facade.theme_service = ThemeService()
    return facade


def test_mode_panel_status_colors(qtbot, mock_facade):
    """Проверка цветовой индикации статусов."""
    panel = ModePanelWidget(mock_facade)
    qtbot.addWidget(panel)
    panel._active_session = types.SimpleNamespace(id="sess1")
    active_session_uid = session_runtime_uid(panel._active_session)

    # Get expected colors
    colors = mock_facade.theme_service.get_theme_colors()

    # Working (Started)
    panel._on_facade_notification(AppNotification(event="task:started", payload={"session_id": active_session_uid}))
    assert panel.status_text.text() == "Working"
    assert colors["warning"].lower() in panel.status_text.styleSheet().lower()

    # Completed
    panel._on_facade_notification(AppNotification(event="task:completed", payload={"session_id": active_session_uid}))
    assert panel.status_text.text() == "Completed"
    assert colors["success"].lower() in panel.status_text.styleSheet().lower()

    # Failed
    panel._on_facade_notification(AppNotification(event="task:failed", payload={"session_id": active_session_uid}))
    assert panel.status_text.text() == "Failed"
    assert colors["danger"].lower() in panel.status_text.styleSheet().lower()

    # Cancelled
    panel._on_facade_notification(AppNotification(event="task:cancelled", payload={"session_id": active_session_uid}))
    assert panel.status_text.text() == "Cancelled"
    assert colors["text_secondary"].lower() in panel.status_text.styleSheet().lower()


def test_mode_panel_history_tracking(qtbot, mock_facade):
    """Проверка отслеживания истории переходов."""
    panel = ModePanelWidget(mock_facade)
    qtbot.addWidget(panel)
    panel._active_session = types.SimpleNamespace(id="sess1")
    active_session_uid = session_runtime_uid(panel._active_session)

    panel._on_facade_notification(AppNotification(event="task:started", payload={"session_id": active_session_uid}))
    panel._on_facade_notification(AppNotification(event="task:completed", payload={"session_id": active_session_uid}))

    assert len(panel._status_history) >= 2
    assert "Working" in panel._status_history[-2]
    assert "Completed" in panel._status_history[-1]

    # Tooltip check
    tooltip = panel.status_text.toolTip()
    assert "Working" in tooltip
    assert "Completed" in tooltip
