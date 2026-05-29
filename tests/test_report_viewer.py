import pytest
import os
import tempfile
import shutil
import types
from unittest.mock import MagicMock, patch
from PySide6.QtCore import Qt
from desktop.widgets.report_viewer import ReportViewerWidget
from desktop.services.application_facade import ApplicationFacade
from modes.sdk.runtime.contracts import ProjectPlan, DevTask


@pytest.fixture
def temp_workdir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def mock_facade(temp_workdir):
    facade = MagicMock(spec=ApplicationFacade)
    facade.subscribe = MagicMock(return_value=lambda: None)

    # Setup session with real temp_workdir (not MagicMock for workdir)
    session = MagicMock()
    session.id = "sess1"
    session.workdir = temp_workdir
    session.conversation_scope = types.SimpleNamespace(session_uid="desktop:sess1")

    facade.session_service = MagicMock()
    facade.session_service.get_session = MagicMock(side_effect=AssertionError("legacy get_session should not be used"))
    facade.session_service.get_session_by_uid = MagicMock(
        side_effect=lambda session_uid: session if str(session_uid) == "desktop:sess1" else None
    )

    task1 = DevTask(id="t1", title="Task 1", description="D1", acceptance_criteria=[], status="approved")
    plan = ProjectPlan(project_goal="Test Goal", tasks=[task1], created_at="2026-01-01", updated_at="2026-01-02", status="active")
    facade.get_manager_plan.return_value = plan

    return facade


def test_report_viewer_initial_load(qtbot, mock_facade):
    """Проверка инициализации и загрузки истории."""
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)

    # Create a dummy report file
    reports_dir = os.path.join(
        mock_facade.session_service.get_session_by_uid("desktop:sess1").workdir,
        ".cli-proxy",
        ".manager_reports",
    )
    os.makedirs(reports_dir, exist_ok=True)
    report_path = os.path.join(reports_dir, "old_report.md")
    with open(report_path, "w") as f:
        f.write("# Old Report")

    widget.set_session("desktop:sess1")

    assert widget.history_list.count() == 1
    assert widget.history_list.item(0).text() == "old_report.md"


def test_report_viewer_generate_md(qtbot, mock_facade):
    """Проверка генерации MD отчета."""
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)
    widget.set_session("desktop:sess1")

    # Click generate MD
    with patch("PySide6.QtWidgets.QMessageBox.information"):
        qtbot.mouseClick(widget.gen_md_btn, Qt.MouseButton.LeftButton)

    # Verify file created
    reports_dir = os.path.join(
        mock_facade.session_service.get_session_by_uid("desktop:sess1").workdir,
        ".cli-proxy",
        ".manager_reports",
    )
    files = os.listdir(reports_dir)
    assert any(f.startswith("report_") and f.endswith(".md") for f in files)
    assert widget.history_list.count() > 0


def test_report_viewer_selection(qtbot, mock_facade):
    """Проверка отображения при выборе отчета."""
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)
    widget.set_session("desktop:sess1")

    # Click generate MD first
    with patch("PySide6.QtWidgets.QMessageBox.information"):
        qtbot.mouseClick(widget.gen_md_btn, Qt.MouseButton.LeftButton)

    # Selection
    widget.history_list.setCurrentRow(0)

    # Content should be loaded into viewer
    assert "Test Goal" in widget.viewer.toHtml()


def test_report_viewer_uses_get_session_by_uid_only(qtbot, mock_facade):
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)

    widget.set_session("desktop:sess1")

    mock_facade.session_service.get_session_by_uid.assert_called_with("desktop:sess1")
