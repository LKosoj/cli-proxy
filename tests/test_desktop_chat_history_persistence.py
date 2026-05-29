import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from desktop.main_window import MainWindow
from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService


@pytest.fixture(autouse=True)
def _patch_widgets():
    # Avoid exercising full widget internals here.
    class _SessionManagerStub(QWidget):
        sessionSelected = Signal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.chat_id = 0

    class _ConfigEditorStub(QWidget):
        configSaved = Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()

        def load_config(self):
            return None

    class _FilesPanelStub(QWidget):
        def set_session(self, _session):
            return None

        def refresh(self):
            return None

    class _StatusPanelStub(QWidget):
        def set_session(self, _session, _session_uid=""):
            return None

        def refresh_mode(self, _session):
            return None

    class _SchedulerPanelStub(QWidget):
        def set_context_session(self, _session_uid):
            return None

    with patch("desktop.main_window.LogViewerWidget", side_effect=lambda *a, **k: QWidget()), \
         patch("desktop.main_window.GitPanelWidget", side_effect=lambda *a, **k: QWidget()), \
         patch("desktop.main_window.FilesPanelWidget", side_effect=lambda *a, **k: _FilesPanelStub()), \
         patch("desktop.main_window.ModePanelWidget", side_effect=lambda *a, **k: QWidget()), \
         patch("desktop.main_window.ModeMenuWidget", side_effect=lambda *a, **k: QWidget()), \
         patch("desktop.main_window.SchedulerPanelWidget", side_effect=lambda *a, **k: _SchedulerPanelStub()), \
         patch("desktop.main_window.StatusPanelWidget", side_effect=lambda *a, **k: _StatusPanelStub()), \
         patch("desktop.main_window.ConfigEditorWidget", side_effect=lambda *a, **k: _ConfigEditorStub()), \
         patch("desktop.main_window.SessionManagerWidget", side_effect=lambda *a, **k: _SessionManagerStub()):
        yield


@pytest.mark.asyncio
async def test_main_window_persists_chat_history(qtbot):
    facade = ApplicationFacade(
        config_service=MagicMock(spec=ConfigService),
        session_service=MagicMock(spec=SessionService),
        task_service=MagicMock(spec=TaskService),
    )
    ui_state_service = MagicMock()
    ui_state_service.state = MagicMock(
        window_geometry=None,
        window_state=None,
        active_tab="chat",
        splitter_sizes=[200, 600],
        chat_history={},
    )
    ui_state_service.save = AsyncMock()

    window = MainWindow(facade, ui_state_service)
    qtbot.addWidget(window)

    window._persist_chat_message("s1", "user", "hello")
    assert "s1" in ui_state_service.state.chat_history
    assert ui_state_service.state.chat_history["s1"][-1]["text"] == "hello"


@pytest.mark.asyncio
async def test_main_window_persists_attachments_meta(qtbot):
    facade = ApplicationFacade(
        config_service=MagicMock(spec=ConfigService),
        session_service=MagicMock(spec=SessionService),
        task_service=MagicMock(spec=TaskService),
    )
    ui_state_service = MagicMock()
    ui_state_service.state = MagicMock(
        window_geometry=None,
        window_state=None,
        active_tab="chat",
        splitter_sizes=[200, 600],
        chat_history={},
    )
    ui_state_service.save = AsyncMock()

    window = MainWindow(facade, ui_state_service)
    qtbot.addWidget(window)

    window._persist_chat_message("s1", "user", "", attachments=[{"kind": "image", "name": "a.png"}])
    entry = ui_state_service.state.chat_history["s1"][-1]
    assert entry["attachments"][0]["name"] == "a.png"
