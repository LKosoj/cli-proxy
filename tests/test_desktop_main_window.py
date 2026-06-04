import pytest
import types
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import re
from pathlib import Path
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QLabel
from desktop.main_window import MainWindow
from i18n import t
from desktop.services.application_facade import ApplicationFacade
from desktop.services.desktop_state_service import DesktopUiStateService
from modes.admin.state_store import AdminStateStore
from app.services import (
    ConfigService,
    SessionService,
    TaskService,
    ThemeService
)


def _session_with_uid(session_id: str, session_uid: str, **extra):
    payload = {
        "id": session_id,
        "conversation_scope": types.SimpleNamespace(session_uid=session_uid),
    }
    payload.update(extra)
    return types.SimpleNamespace(**payload)


@pytest.fixture
def mock_facade():
    config_service = MagicMock(spec=ConfigService)
    session_service = MagicMock(spec=SessionService)
    session_service._manager = MagicMock()
    task_service = MagicMock(spec=TaskService)
    task_service.log_bus = MagicMock()

    # SessionManagerWidget in MainWindow calls these on init
    session_service.list_sessions.return_value = []
    session_service._manager.active.return_value = None
    session_service.get_session = MagicMock(side_effect=AssertionError("legacy get_session should not be used"))
    session_service.get_session_by_uid = MagicMock(return_value=None)

    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        theme_service=ThemeService()
    )
    facade.try_queue_busy_input = AsyncMock(return_value=False)
    facade.run_admin_session_action = AsyncMock(return_value=True)
    return facade


@pytest.fixture
def mock_ui_state():
    ui_state_service = MagicMock(spec=DesktopUiStateService)
    ui_state_service.state = MagicMock()
    ui_state_service.state.window_geometry = None
    ui_state_service.state.window_state = None
    ui_state_service.state.active_tab = "chat"
    ui_state_service.state.theme = ""
    ui_state_service.state.splitter_sizes = [200, 600]

    # ensure_async expects a coroutine
    ui_state_service.save = AsyncMock()

    return ui_state_service


@pytest.fixture(autouse=True)
def mock_widgets():
    from PySide6.QtWidgets import QWidget

    class MockGitPanel(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def set_session(self, session):
            pass

    class MockModePanel(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def set_session(self, session):
            pass

    from PySide6.QtCore import Signal

    class MockConfigEditor(QWidget):
        configSaved = Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()

        def load_config(self):
            pass

    class MockRunsPanel(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.session_uid = None

        def set_session_id(self, session_uid):
            self.session_uid = session_uid

    class MockFilesPanel(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.session = None
            self.refresh_called = False

        def set_session(self, session):
            self.session = session

        def refresh(self):
            self.refresh_called = True

    class MockStatusPanel(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.session = None
            self.session_uid = None
            self.mode_session = None

        def set_session(self, session, session_uid=""):
            self.session = session
            self.session_uid = session_uid

        def refresh_mode(self, session):
            self.mode_session = session

    class MockSchedulerPanel(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.context_session_uid = None

        def set_context_session(self, session_uid):
            self.context_session_uid = session_uid

    # We patch these widgets to avoid their internal async/timer logic during MainWindow tests
    with patch("desktop.main_window.LogViewerWidget", side_effect=lambda *args, **kwargs: QWidget()), \
         patch("desktop.main_window.GitPanelWidget", side_effect=lambda *args, **kwargs: MockGitPanel()), \
         patch("desktop.main_window.FilesPanelWidget", side_effect=lambda *args, **kwargs: MockFilesPanel()), \
         patch("desktop.main_window.RunOperationsPanelWidget", side_effect=lambda *args, **kwargs: MockRunsPanel()), \
         patch("desktop.main_window.SchedulerPanelWidget", side_effect=lambda *args, **kwargs: MockSchedulerPanel()), \
         patch("desktop.main_window.StatusPanelWidget", side_effect=lambda *args, **kwargs: MockStatusPanel()), \
         patch("desktop.main_window.ModePanelWidget", side_effect=lambda *args, **kwargs: MockModePanel()), \
         patch("desktop.main_window.ConfigEditorWidget", side_effect=lambda *args, **kwargs: MockConfigEditor()):
        yield


@pytest.mark.asyncio
async def test_main_window_init(qtbot, mock_facade, mock_ui_state):
    """Проверка инициализации MainWindow и взаимодействия с сервисами."""
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    assert window.windowTitle() == "Gemini CLI"
    assert window.content_stack.count() == 9
    assert set(window._tab_widgets) == {
        "chat",
        "settings",
        "files",
        "logs",
        "status",
        "scheduler",
        "session_settings",
        "reports",
        "plugins",
    }
    assert window.nav_chat.isChecked()
    assert window.nav_config.toolTip() == t("desktop.nav.settings", "ru")
    assert window.nav_files.toolTip() == t("desktop.nav.files", "ru")
    assert window.nav_status.toolTip() == t("desktop.nav.status", "ru")
    assert window.nav_scheduler.toolTip() == t("desktop.nav.scheduler", "ru")
    assert window.nav_session_settings.toolTip() == t("desktop.nav.session_settings", "ru")

    # Test tab switching
    window.nav_config.click()
    assert window.content_stack.currentWidget() == window.settings_page
    assert mock_ui_state.save.called

    # Verify save call on switch
    args, kwargs = mock_ui_state.save.call_args
    assert kwargs['active_tab'] == "settings"


def test_main_window_exposes_miniapp_navigation_tabs(qtbot, mock_facade, mock_ui_state):
    """Desktop keeps top-level equivalents for MiniApp navigation tabs."""
    mock_facade.mode_registry_service = MagicMock()
    mock_facade.mode_registry_service.get.side_effect = (
        lambda mode_id: object() if str(mode_id) == "admin" else None
    )
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    index_html = Path(__file__).parents[1] / "miniapp" / "static" / "index.html"
    miniapp_tabs = set(re.findall(r'data-tab="([^"]+)"', index_html.read_text(encoding="utf-8")))
    tab_map = {
        "config": "settings",
        "settings": "session_settings",
        "tasks": "chat",
    }
    expected_desktop_tabs = {tab_map.get(tab, tab) for tab in miniapp_tabs}

    assert expected_desktop_tabs.issubset(set(window._tab_widgets))


@pytest.mark.asyncio
async def test_main_window_restore_state(qtbot, mock_facade, mock_ui_state):
    """Проверка восстановления состояния окна."""
    mock_ui_state.state.active_tab = "logs"
    mock_ui_state.state.session_panel_visible = False

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    assert window.content_stack.currentWidget() == window.logs_page
    assert window.nav_logs.isChecked()
    assert window.toggle_sessions_btn.isChecked() is False
    mock_ui_state.save.assert_not_called()


@pytest.mark.asyncio
async def test_main_window_restore_state_falls_back_to_theme_service_without_config_theme(
    qtbot,
    mock_facade,
    mock_ui_state,
):
    mock_ui_state.state.theme = ""
    mock_facade.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
    mock_facade.theme_service._current_theme_name = "dark"
    mock_facade.set_theme = MagicMock(return_value=True)

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    mock_facade.set_theme.assert_any_call("dark")


@pytest.mark.asyncio
async def test_main_window_config_saved_refreshes_modes_session_and_theme_without_config_theme_attr(
    qtbot,
    mock_facade,
    mock_ui_state,
):
    mock_ui_state.state.theme = "dark"
    mock_facade.config = types.SimpleNamespace(defaults=types.SimpleNamespace())
    mock_facade.reload = AsyncMock(return_value=MagicMock())
    mock_facade.set_theme = MagicMock(return_value=True)

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    window._active_session_uid = "desktop:s1"
    window.mode_panel = MagicMock()
    window._on_session_selected = MagicMock()
    window._apply_theme = MagicMock()
    window.logger.exception = MagicMock()

    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._on_config_saved()
        await asyncio.sleep(0)

    mock_facade.reload.assert_awaited_once()
    mock_facade.set_theme.assert_called_with("dark")
    window._apply_theme.assert_called_once()
    window.mode_panel.load_modes.assert_called_once()
    window._on_session_selected.assert_called_once_with("desktop:s1")
    window.logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_main_window_config_saved_still_refreshes_modes_and_session_when_theme_restore_fails(
    qtbot,
    mock_facade,
    mock_ui_state,
):
    mock_ui_state.state.theme = "dark"
    mock_facade.reload = AsyncMock(return_value=MagicMock())
    mock_facade.set_theme = MagicMock(return_value=True)

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    window._active_session_uid = "desktop:s1"
    window.mode_panel = MagicMock()
    window._on_session_selected = MagicMock()
    window.logger.exception = MagicMock()
    mock_facade.set_theme.side_effect = RuntimeError("theme boom")

    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._on_config_saved()
        await asyncio.sleep(0)

    mock_facade.reload.assert_awaited_once()
    window.mode_panel.load_modes.assert_called_once()
    window._on_session_selected.assert_called_once_with("desktop:s1")
    assert window.logger.exception.call_count == 1
    assert "Failed to re-apply theme after config save" in str(window.logger.exception.call_args.args[0])


@pytest.mark.asyncio
async def test_main_window_shows_admin_tab_when_admin_mode_enabled(qtbot, mock_facade, mock_ui_state):
    """Вкладка Admin появляется только когда admin mode доступен в registry."""
    mock_ui_state.state.active_tab = "admin"
    mock_facade.mode_registry_service = MagicMock()
    mock_facade.mode_registry_service.get.side_effect = (
        lambda mode_id: object() if str(mode_id) == "admin" else None
    )

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    assert window.nav_admin is not None
    assert window.nav_admin.toolTip() == t("desktop.nav.admin", "ru")
    assert window.admin_page is not None
    assert window.content_stack.count() == 10
    assert window.content_stack.currentWidget() == window.admin_page
    assert window.nav_admin.isChecked()


@pytest.mark.asyncio
async def test_main_window_admin_tab_session_selector_updates_internal_state(
    qtbot,
    mock_facade,
    mock_ui_state,
    tmp_path,
):
    """Селектор сессий в AdminPanel переключает локальный state и не скрывает вкладку."""
    session_a = _session_with_uid("s-admin-1", "desktop:s-admin-1", name="Admin A", workdir=str(tmp_path / "admin-a"))
    session_b = _session_with_uid("s-admin-2", "desktop:s-admin-2", name="Admin B", workdir=str(tmp_path / "admin-b"))
    sessions = {
        session_a.conversation_scope.session_uid: session_a,
        session_b.conversation_scope.session_uid: session_b,
    }

    mock_facade.mode_registry_service = MagicMock()
    mock_facade.mode_registry_service.get.side_effect = (
        lambda mode_id: object() if str(mode_id) == "admin" else None
    )
    mock_facade.session_service.list_desktop_sessions.return_value = [session_a, session_b]
    mock_facade.session_service._manager.active.return_value = session_a
    mock_facade.session_service.get_session_by_uid.side_effect = lambda session_uid: sessions.get(str(session_uid))

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    window._switch_tab("admin")

    assert window.admin_page is not None
    assert window.admin_page.session_selector.count() == 2
    assert window.admin_page.active_session_uid == session_a.conversation_scope.session_uid
    first_item = window.session_manager.session_list.item(0)
    first_widget = window.session_manager.session_list.itemWidget(first_item)
    first_label = first_widget.findChild(QLabel, "session_item_name")
    assert first_label is not None
    assert first_label.text() == f"cli | {session_a.id} | {session_a.name}"

    window.admin_page.session_selector.setCurrentIndex(1)

    assert window.admin_page.active_session_uid == session_b.conversation_scope.session_uid
    assert window.admin_page.session_selector.currentData() == session_b.conversation_scope.session_uid
    assert window.content_stack.currentWidget() == window.admin_page
    assert window.nav_admin is not None
    assert window.nav_admin.isChecked()

    window._on_session_selected(session_a.conversation_scope.session_uid)

    assert window.admin_page.active_session_uid == session_a.conversation_scope.session_uid
    assert window.admin_page.session_selector.currentData() == session_a.conversation_scope.session_uid
    assert window.content_stack.currentWidget() == window.admin_page


def test_main_window_session_manager_uses_topic_title_in_list(
    qtbot,
    mock_facade,
    mock_ui_state,
    tmp_path,
):
    session = _session_with_uid(
        "s-admin-1",
        "desktop:s-admin-1",
        name="Admin A",
        workdir=str(tmp_path / "admin-a"),
    )

    mock_facade.mode_registry_service = MagicMock()
    mock_facade.mode_registry_service.get.side_effect = lambda _mode_id: None
    mock_facade.session_service.list_desktop_sessions.return_value = [session]
    mock_facade.session_service._manager.active.return_value = session
    mock_facade.session_service.get_session_by_uid.side_effect = (
        lambda session_uid: session if str(session_uid) == session.conversation_scope.session_uid else None
    )

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    item = window.session_manager.session_list.item(0)
    widget = window.session_manager.session_list.itemWidget(item)
    label = widget.findChild(QLabel, "session_item_name")
    assert label is not None
    assert label.text() == f"cli | {session.id} | {session.name}"


@pytest.mark.asyncio
async def test_main_window_admin_tab_shows_disabled_state_for_disabled_session(
    qtbot,
    mock_facade,
    mock_ui_state,
    tmp_path,
):
    """Для выключенной admin-сессии показывается disabled-экран с кнопками действий."""
    state_path = tmp_path / "admin-state.sqlite3"
    store = AdminStateStore(str(state_path))

    session_disabled = _session_with_uid(
        "s-admin-disabled",
        "desktop:s-admin-disabled",
        name="Admin Disabled",
        workdir=str(tmp_path / "admin-disabled"),
    )
    session_enabled = _session_with_uid(
        "s-admin-enabled",
        "desktop:s-admin-enabled",
        name="Admin Enabled",
        workdir=str(tmp_path / "admin-enabled"),
    )
    sessions = {
        session_disabled.conversation_scope.session_uid: session_disabled,
        session_enabled.conversation_scope.session_uid: session_enabled,
    }

    store.upsert_session_state(session_disabled.conversation_scope.session_uid, chat_id=1, enabled=False)
    store.upsert_session_state(session_enabled.conversation_scope.session_uid, chat_id=1, enabled=True)

    mock_facade.config = types.SimpleNamespace(
        defaults=types.SimpleNamespace(state_path=str(state_path))
    )
    mock_facade.mode_registry_service = MagicMock()
    mock_facade.mode_registry_service.get.side_effect = (
        lambda mode_id: object() if str(mode_id) == "admin" else None
    )
    mock_facade.session_service.list_desktop_sessions.return_value = [session_disabled, session_enabled]
    mock_facade.session_service._manager.active.return_value = session_disabled
    mock_facade.session_service.get_session_by_uid.side_effect = lambda session_uid: sessions.get(str(session_uid))

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    window._switch_tab("admin")

    assert window.admin_page is not None
    assert window.admin_page.state_stack.currentWidget() == window.admin_page.disabled_page
    assert window.admin_page.disabled_title_label.text() == t("desktop.admin.label.disabled_title", "ru")
    assert window.admin_page.enable_button.text() == t("desktop.admin.btn.enable", "ru")
    assert window.admin_page.rescan_button.text() == t("desktop.admin.btn.rescan", "ru")

    window.admin_page.session_selector.setCurrentIndex(1)

    assert window.admin_page.state_stack.currentWidget() == window.admin_page.enabled_page
    assert window.content_stack.currentWidget() == window.admin_page

    window.admin_page.session_selector.setCurrentIndex(0)

    assert window.admin_page.state_stack.currentWidget() == window.admin_page.disabled_page
    assert window.content_stack.currentWidget() == window.admin_page


@pytest.mark.asyncio
async def test_main_window_admin_buttons_dispatch_actions_for_selected_session(
    qtbot,
    mock_facade,
    mock_ui_state,
    tmp_path,
):
    session = _session_with_uid("s-admin-1", "desktop:s-admin-1", name="Admin A", workdir=str(tmp_path / "admin-a"))
    mock_facade.mode_registry_service = MagicMock()
    mock_facade.mode_registry_service.get.side_effect = (
        lambda mode_id: object() if str(mode_id) == "admin" else None
    )
    mock_facade.session_service.list_desktop_sessions.return_value = [session]
    mock_facade.session_service._manager.active.return_value = session
    mock_facade.session_service.get_session_by_uid.side_effect = (
        lambda session_uid: session if str(session_uid) == session.conversation_scope.session_uid else None
    )

    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    window._switch_tab("admin")

    assert window.admin_page is not None
    window.admin_page.rescan_button.click()
    await asyncio.sleep(0)

    pass


@pytest.mark.asyncio
async def test_main_window_session_selection_handling(qtbot, mock_facade, mock_ui_state):
    """Проверка обработки сигнала выбора сессии в MainWindow."""
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    # Эмулируем выбор сессии в SessionManagerWidget.
    # Signal is emitted synchronously — no need for waitSignal which can
    # produce spurious "Failed to disconnect" warnings on fast signals.
    window.session_manager.sessionSelected.emit("test-session-123")

    # Проверяем обновления в UI
    assert "test-session-123" in window.statusBar().currentMessage()
    assert "test-session-123" in window.chat_view.history_browser.toPlainText()


@pytest.mark.asyncio
async def test_main_window_task_integration(qtbot, mock_facade, mock_ui_state):
    """Проверка интеграции с событиями TaskService через Facade и синхронизации виджетов."""
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "test-session"
    window._on_session_selected(session_id)

    # Simulate task started
    mock_facade.notify("task:started", session_id=session_id, task_id="t1", name="Long Run")
    assert window.chat_view.send_button.isEnabled() is False  # Loading state

    # TaskProgressWidget should also show the task
    assert "t1" in window.task_progress._task_containers
    assert window.task_progress._task_labels["t1"].text() == "Long Run"

    # Simulate progress update
    mock_facade.notify("task:updated", task_id="t1", progress=0.75, stage="working")
    assert window.task_progress._task_bars["t1"].value() == 75

    # Simulate task completed
    mock_facade.notify("task:completed", session_id=session_id, task_id="t1")
    assert window.chat_view.send_button.isEnabled() is True  # Ready state

    # TaskProgressWidget should be empty after delay (our enhanced widget removes tasks after 2s)
    # In test environment, we need to process Qt events manually to trigger the QTimer
    from PySide6.QtCore import QCoreApplication

    # Wait for the QTimer.singleShot(2000) to execute in the test environment
    # We'll manually process events after sufficient time has passed
    await asyncio.sleep(2.1)  # Wait for the 2-second delay in the widget
    QCoreApplication.processEvents()  # Process the queued removal action
    await asyncio.sleep(0.1)  # Allow the removal to happen
    QCoreApplication.processEvents()  # Process any remaining events

    assert "t1" not in window.task_progress._task_containers


@pytest.mark.asyncio
async def test_main_window_manage_tasks_progress_message(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "test-session"
    window._on_session_selected(session_id)

    mock_facade.notify(
        "ui:manage_tasks_progress",
        session_id=session_id,
        tasks=[{"id": "t1", "content": "Inspect", "status": "pending"}],
        progress={"total": 1, "closed": 0, "open": 1},
    )
    assert window.chat_view._progress_message_id is not None
    assert "План выполнения" in window.chat_view.history_browser.toPlainText()
    assert "Inspect" in window.chat_view.history_browser.toPlainText()

    mock_facade.notify(
        "ui:manage_tasks_progress",
        session_id=session_id,
        tasks=[{"id": "t1", "content": "Inspect", "status": "in_progress"}],
        progress={"total": 1, "closed": 0, "open": 1},
    )
    assert "[~] t1: Inspect" in window.chat_view.history_browser.toPlainText()

    mock_facade.notify("ui:manage_tasks_progress_clear", session_id=session_id)
    assert window.chat_view._progress_message_id is None


@pytest.mark.asyncio
async def test_main_window_stages_free_session_input_instead_of_running_it(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "test-session"
    window._on_session_selected(session_id)
    mock_facade.handle_dialog_message = AsyncMock(return_value=None)
    mock_facade.stage_session_input = AsyncMock(return_value=None)
    mock_facade.run_session_input = AsyncMock(return_value="SHOULD_NOT_BE_CALLED")

    # В тестовой среде qasync-цикл может не быть активен; подменяем ensure_async,
    # чтобы корутины действительно запускались в текущем asyncio loop.
    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._on_message_sent("Hello")

    # Даем asyncio шанс выполнить созданную задачу.
    await asyncio.sleep(0)

    await asyncio.wait_for(window._active_run_task, timeout=2.0)
    mock_facade.stage_session_input.assert_awaited_once_with(
        session_id,
        "Hello",
        prepared_attachments=None,
    )
    assert window.facade.run_session_input.await_count == 0
    assert "Hello" in window.chat_view.history_browser.toPlainText()


@pytest.mark.asyncio
async def test_main_window_renders_and_clears_transient_assistant_preview(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "preview-session"
    window._on_session_selected(session_id)

    mock_facade.notify("ui:assistant_preview", session_id=session_id, text="streamed draft")

    assert window.chat_view.assistant_preview_browser.isHidden() is False
    assert "streamed draft" in window.chat_view.assistant_preview_browser.toPlainText()

    mock_facade.notify("ui:assistant_preview_clear", session_id=session_id)

    assert window.chat_view.assistant_preview_browser.isHidden() is True


@pytest.mark.asyncio
async def test_main_window_session_busy_init(qtbot, mock_facade, mock_ui_state):
    """Проверка инициализации состояния занятости при выборе сессии."""
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "busy-session"
    # Эмулируем наличие активных задач
    mock_facade.task_service.list_active.return_value = [MagicMock()]

    window._on_session_selected(session_id)

    assert window.chat_view.send_button.isEnabled() is False
    assert "active tasks running" in window.chat_view.history_browser.toPlainText()


@pytest.mark.asyncio
async def test_main_window_uses_universal_mode_menu_on_mode_change(qtbot, mock_facade, mock_ui_state):
    """UI mode panel строится универсально через facade.show_mode_menu без hardcode по mode_id."""
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session = MagicMock()
    type(session).id = "s-mode"
    session.id = "s-mode"
    session.conversation_scope = types.SimpleNamespace(session_uid="desktop:s-mode")
    session.modes = types.SimpleNamespace(active_mode="agent", analyst_mode="spec")
    session.active_cli = "dummy"
    session.busy = False

    mock_facade.session_service.get_session_by_uid.return_value = session
    mock_facade.show_mode_menu = AsyncMock(return_value=True)

    # Patch ensure_async to run coroutines in current loop.
    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._mode_supports_menu = MagicMock(return_value=True)
        window._on_session_selected("desktop:s-mode")
        await asyncio.sleep(0)
        mock_facade.show_mode_menu.assert_called_with("desktop:s-mode")

    # При отключении режима меню очищается.
    window.mode_menu.clear = MagicMock()
    session.modes.active_mode = None
    mock_facade.notify("ui:mode_changed", session_uid="desktop:s-mode", mode_id=None)
    window.mode_menu.clear.assert_called_once()


@pytest.mark.asyncio
async def test_main_window_uses_get_session_by_uid_on_session_selection(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session = types.SimpleNamespace(
        id="s1",
        conversation_scope=types.SimpleNamespace(session_uid="desktop:s1"),
        modes=types.SimpleNamespace(active_mode=None),
        active_cli="dummy",
        busy=False,
        workdir=".",
    )
    mock_facade.session_service.get_session_by_uid.return_value = session

    window._on_session_selected("desktop:s1")

    mock_facade.session_service.get_session_by_uid.assert_called_with("desktop:s1")
    assert getattr(window, "_active_session_uid") == "desktop:s1"


@pytest.mark.asyncio
async def test_main_window_reselection_updates_current_session_uid_for_followup_actions(
    qtbot,
    mock_facade,
    mock_ui_state,
):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    def _session(session_id: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            id=session_id,
            conversation_scope=types.SimpleNamespace(session_uid=f"desktop:{session_id}"),
            modes=types.SimpleNamespace(active_mode=None),
            active_cli="dummy",
            busy=False,
            workdir=".",
        )

    sessions = {
        "desktop:s1": _session("s1"),
        "desktop:s2": _session("s2"),
    }
    mock_facade.session_service.get_session_by_uid.side_effect = lambda session_uid: sessions.get(str(session_uid))
    mock_facade.task_service.list_active.return_value = []
    mock_facade.task_service.cancel_session = AsyncMock()

    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    window._on_session_selected("desktop:s1")
    assert window._current_session_uid == "desktop:s1"

    window._on_session_selected("desktop:s2")
    assert window._current_session_uid == "desktop:s2"

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._on_task_cancelled()

    await asyncio.sleep(0)
    mock_facade.task_service.cancel_session.assert_awaited_once_with("desktop:s2")


@pytest.mark.asyncio
async def test_main_window_handles_validation_not_run_event(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "sess-validation"
    window._on_session_selected(session_id)

    before = window.chat_view.history_browser.toPlainText()
    mock_facade.notify("ui:validation_status", session_id=session_id, status="not_run", report={"status": "not_run"})
    after = window.chat_view.history_browser.toPlainText()

    assert "[validation] not_run" in after
    assert after != before


@pytest.mark.asyncio
async def test_main_window_resolves_pending_ask_without_run_session_input(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "ask-session"
    window._on_session_selected(session_id)

    mock_facade.resolve_analyst_question = MagicMock(return_value=True)
    mock_facade.run_session_input = AsyncMock(return_value="SHOULD_NOT_BE_CALLED")
    mock_facade.handle_dialog_message = AsyncMock(return_value=None)

    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    mock_facade.notify(
        "ui:ask_question",
        session_id=session_id,
        question_id="q-1",
        question="Как продолжить?",
        options=["Вариант A", "Вариант B"],
    )

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._on_message_sent("2")
    await asyncio.wait_for(window._active_run_task, timeout=2.0)

    mock_facade.resolve_analyst_question.assert_called_once_with("q-1", "Вариант B")
    assert mock_facade.run_session_input.await_count == 0
    assert "Принял ответ: Вариант B" in window.chat_view.history_browser.toPlainText()


@pytest.mark.asyncio
async def test_main_window_rejects_short_text_alias_for_button_only_question(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    session_id = "ask-session"
    window._on_session_selected(session_id)

    mock_facade.resolve_analyst_question = MagicMock(return_value=True)
    mock_facade.run_session_input = AsyncMock(return_value="SHOULD_NOT_BE_CALLED")
    mock_facade.handle_dialog_message = AsyncMock(return_value=None)

    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    mock_facade.notify(
        "ui:ask_question",
        session_id=session_id,
        question_id="q-1",
        question="Как продолжить?",
        options=["Продолжить остановленный план", "Начать новый план", "Отмена"],
        allow_custom=False,
    )

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._on_message_sent("продолжить")
    await asyncio.wait_for(window._active_run_task, timeout=2.0)

    mock_facade.resolve_analyst_question.assert_not_called()
    assert mock_facade.run_session_input.await_count == 0
    assert "Ответ не распознан. Выберите кнопку или введите полный текст варианта." in window.chat_view.history_browser.toPlainText()


@pytest.mark.asyncio
async def test_main_window_preserves_pending_question_across_session_switch_and_refreshes_buttons(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    window._on_session_selected("s1")
    mock_facade.notify(
        "ui:ask_question",
        session_uid="s1",
        question_id="q-1",
        question="Как продолжить?",
        options=["Вариант A", "Вариант B"],
    )
    assert window.chat_view._ask_options_layout.count() > 0
    assert "s1" in window._pending_ask_by_session

    window._on_session_selected("s2")
    assert window.chat_view._ask_options_layout.count() == 0
    assert "s1" in window._pending_ask_by_session

    window._on_session_selected("s1")
    assert window.chat_view._ask_options_layout.count() > 0


@pytest.mark.asyncio
async def test_main_window_close_event_starts_facade_shutdown(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    completed = []

    async def _save(**kwargs):
        completed.append(("save", kwargs))

    async def _shutdown():
        completed.append(("shutdown", None))

    mock_facade.shutdown = AsyncMock(side_effect=_shutdown)
    mock_ui_state.save = AsyncMock(side_effect=_save)
    window.toggle_git_btn.setChecked(True)
    window.toggle_sessions_btn.setChecked(True)
    window.command_palette.search_input.setText("last query")
    window.context_panel.show()
    expected_context_panel_visible = bool(window.context_panel.isVisible())

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    assert window._close_task is not None

    await asyncio.wait_for(window._close_task, timeout=2.0)

    assert mock_facade.shutdown.await_count == 1
    assert mock_ui_state.save.await_count == 1
    _, kwargs = mock_ui_state.save.await_args
    assert kwargs["context_panel_tool"] == "git"
    assert kwargs["context_panel_visible"] is expected_context_panel_visible
    assert kwargs["session_panel_visible"] is True
    assert kwargs["command_palette_last_query"] == "last query"
    assert isinstance(kwargs["splitter_sizes"], list)
    assert completed[0][0] == "save"
    assert completed[1][0] == "shutdown"
    assert window._close_finalized is True


@pytest.mark.asyncio
async def test_main_window_close_event_still_runs_shutdown_when_save_fails(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    completed = []

    async def _save(**_kwargs):
        completed.append(("save", None))
        raise RuntimeError("save boom")

    async def _shutdown():
        completed.append(("shutdown", None))

    mock_facade.shutdown = AsyncMock(side_effect=_shutdown)
    mock_ui_state.save = AsyncMock(side_effect=_save)

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    assert window._close_task is not None

    await asyncio.wait_for(window._close_task, timeout=2.0)

    assert mock_ui_state.save.await_count == 1
    assert mock_facade.shutdown.await_count == 1
    assert completed == [("save", None), ("shutdown", None)]
    assert window._close_finalized is True


@pytest.mark.asyncio
async def test_main_window_close_event_drains_background_tasks_before_finalize(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)

    finished = asyncio.Event()

    async def _background():
        await asyncio.sleep(0.01)
        finished.set()

    task = asyncio.create_task(_background())
    window._background_tasks.add(task)
    task.add_done_callback(lambda current: window._background_tasks.discard(current))

    mock_facade.shutdown = AsyncMock()
    mock_ui_state.save = AsyncMock()

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    assert window._close_task is not None

    await asyncio.wait_for(window._close_task, timeout=2.0)

    assert finished.is_set() is True
    assert task.done() is True
    assert mock_facade.shutdown.await_count == 1


def test_main_window_close_event_keeps_window_open_when_no_async_or_fallback_loop(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    mock_facade.shutdown = AsyncMock()

    def _drop_async(coro, parent=None):
        _ = parent
        coro.close()
        return None

    event = QCloseEvent()
    with patch("desktop.main_window.ensure_async", side_effect=_drop_async), patch(
        "desktop.main_window.asyncio.get_event_loop",
        side_effect=RuntimeError("no loop"),
    ):
        window.closeEvent(event)

    assert event.isAccepted() is False
    assert window._close_finalized is False
    assert window._closing_in_progress is False
    assert mock_ui_state.save.await_count == 0
    assert mock_facade.shutdown.await_count == 0


def test_main_window_runs_panel_toggle_opens_context_widget(qtbot, mock_facade, mock_ui_state):
    def _drop_async(coro, parent=None):
        _ = parent
        coro.close()
        return None

    with patch("desktop.main_window.ensure_async", side_effect=_drop_async):
        window = MainWindow(mock_facade, mock_ui_state)
        qtbot.addWidget(window)

        window._show_context_panel("runs", persist=False)

        assert window.context_panel.isHidden() is False
        assert window.context_stack.currentWidget() == window.context_run_operations
        assert window.toggle_runs_btn.isChecked() is True
        assert window.toggle_git_btn.isChecked() is False
        assert window.toggle_tasks_btn.isChecked() is False
        window.close()


@pytest.mark.asyncio
async def test_main_window_palette_limits_appends_report_to_chat(qtbot, mock_facade, mock_ui_state):
    window = MainWindow(mock_facade, mock_ui_state)
    qtbot.addWidget(window)
    window._active_session_uid = "desktop:s1"
    window.chat_view.append_message = MagicMock()
    window._persist_chat_message = MagicMock()
    mock_facade.describe_active_cli_limits = AsyncMock(return_value="LIMITS REPORT")

    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda current: parent._background_tasks.discard(current))
        return task

    with patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window._handle_palette_command("session:limits")
        await asyncio.sleep(0)

    mock_facade.describe_active_cli_limits.assert_awaited_once()
    window.chat_view.append_message.assert_called_once_with("agent", "LIMITS REPORT")
    window._persist_chat_message.assert_called_once_with("desktop:s1", "agent", "LIMITS REPORT")


@pytest.mark.asyncio
async def test_application_facade_limits_passes_enabled_supported_cli_names(mock_facade) -> None:
    sessions = [types.SimpleNamespace(id="desktop:s1")]
    mock_facade.session_service.list_desktop_sessions.return_value = sessions
    mock_facade.config = types.SimpleNamespace(
        tools={
            "claude": types.SimpleNamespace(enabled=True),
            "codex": types.SimpleNamespace(enabled=True),
            "gemini": types.SimpleNamespace(enabled=True),
            "grok": types.SimpleNamespace(enabled=True),
            "qwen": types.SimpleNamespace(enabled=False),
            "backup": types.SimpleNamespace(enabled=True),
        }
    )
    mock_facade.cli_limits_service = types.SimpleNamespace(
        SUPPORTED_CLI_NAMES=("claude", "codex", "gemini", "grok", "qwen"),
        describe_for_sessions=AsyncMock(return_value="LIMITS REPORT"),
    )

    text = await mock_facade.describe_active_cli_limits()

    assert text == "LIMITS REPORT"
    mock_facade.cli_limits_service.describe_for_sessions.assert_awaited_once_with(
        sessions,
        available_clis=["claude", "codex", "gemini", "grok"],
    )
