import pytest
import tempfile
import shutil
import types
from unittest.mock import MagicMock, patch
from PySide6.QtCore import Qt
from desktop.widgets.report_viewer import ReportViewerWidget
from desktop.services.application_facade import ApplicationFacade


@pytest.fixture
def temp_workdir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def mock_facade(temp_workdir):
    facade = MagicMock(spec=ApplicationFacade)
    facade.subscribe = MagicMock(return_value=lambda: None)
    facade.ui_language = "ru"

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

    reports = [
        {
            "id": "old_report.md",
            "report_id": "old_report.md",
            "name": "old_report.md",
            "format": "md",
            "content": "# Old Report\n\nTest Goal",
        }
    ]

    def list_reports(session_uid):
        assert session_uid == "desktop:sess1"
        return [dict(item) for item in reports]

    def get_report(session_uid, report_id):
        assert session_uid == "desktop:sess1"
        for item in reports:
            if item["report_id"] == report_id:
                return dict(item)
        return None

    def save_report(session_uid):
        assert session_uid == "desktop:sess1"
        report = {
            "id": "manager_plan_20260101_120000.md",
            "report_id": "manager_plan_20260101_120000.md",
            "name": "manager_plan_20260101_120000.md",
            "format": "md",
            "content": "# Project Report: Test Goal",
        }
        reports.insert(0, report)
        return dict(report)

    facade.list_session_reports.side_effect = list_reports
    facade.get_session_report.side_effect = get_report
    facade.save_manager_plan_report.side_effect = save_report
    facade._reports = reports

    return facade


def test_report_viewer_initial_load(qtbot, mock_facade):
    """Проверка инициализации и загрузки истории."""
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)

    widget.set_session("desktop:sess1")

    assert widget.history_list.count() == 1
    assert widget.history_list.item(0).text() == "old_report.md"
    mock_facade.list_session_reports.assert_called_with("desktop:sess1")


def test_report_viewer_generate_md(qtbot, mock_facade):
    """Проверка генерации MD отчета."""
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)
    widget.set_session("desktop:sess1")

    # Click generate MD
    with patch("PySide6.QtWidgets.QMessageBox.information"):
        qtbot.mouseClick(widget.gen_md_btn, Qt.MouseButton.LeftButton)

    mock_facade.save_manager_plan_report.assert_called_once_with("desktop:sess1")
    assert widget.history_list.item(0).text() == "manager_plan_20260101_120000.md"


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
    mock_facade.get_session_report.assert_called_with(
        "desktop:sess1",
        "manager_plan_20260101_120000.md",
    )


def test_report_viewer_uses_get_session_by_uid_only(qtbot, mock_facade):
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)

    widget.set_session("desktop:sess1")

    mock_facade.session_service.get_session_by_uid.assert_called_with("desktop:sess1")


def test_report_viewer_exports_selected_markdown_to_local_pdf(qtbot, mock_facade, tmp_path):
    widget = ReportViewerWidget(mock_facade)
    qtbot.addWidget(widget)
    widget.set_session("desktop:sess1")
    widget.history_list.setCurrentRow(0)

    pdf_path = tmp_path / "selected.pdf"
    with patch("desktop.widgets.report_viewer.QPrinter", object()), \
            patch("desktop.widgets.report_viewer.QFileDialog.getSaveFileName", return_value=(str(pdf_path), "")), \
            patch.object(widget, "_export_to_pdf") as export_to_pdf, \
            patch("PySide6.QtWidgets.QMessageBox.information"):
        widget._generate_report("pdf")

    export_to_pdf.assert_called_once_with("# Old Report\n\nTest Goal", str(pdf_path))
    mock_facade.save_manager_plan_report.assert_not_called()
