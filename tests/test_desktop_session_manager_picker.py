"""Tests for ProjectPickerDialog and _registered_projects / _pick_workdir."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QDialog

from desktop.widgets.session_manager import (
    ProjectPickerDialog,
    SessionManagerWidget,
    _BROWSE_SENTINEL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_widget(tmp_path, user_workdirs=None):
    """Create a minimal SessionManagerWidget with a mocked facade."""
    facade = MagicMock()
    facade.ui_language = "ru"
    facade.session_service.list_sessions.return_value = []
    facade.session_service._manager.active.return_value = None
    facade.subscribe.return_value = lambda: None

    cfg = MagicMock()
    cfg.tools = {"codex": MagicMock()}
    cfg.defaults.workdir = ""
    cfg.telegram.user_workdirs = user_workdirs or {}
    facade.config_service.config = cfg

    widget = SessionManagerWidget.__new__(SessionManagerWidget)
    widget.facade = facade
    widget.session_service = facade.session_service
    widget.logger = MagicMock()
    return widget


# ---------------------------------------------------------------------------
# _registered_projects
# ---------------------------------------------------------------------------

class TestRegisteredProjects:
    def test_empty_when_no_config(self):
        facade = MagicMock()
        facade.config_service.config = None
        widget = SessionManagerWidget.__new__(SessionManagerWidget)
        widget.facade = facade
        widget.logger = MagicMock()
        assert widget._registered_projects() == []

    def test_empty_when_no_telegram(self):
        facade = MagicMock()
        cfg = MagicMock()
        cfg.telegram = None
        facade.config_service.config = cfg
        widget = SessionManagerWidget.__new__(SessionManagerWidget)
        widget.facade = facade
        widget.logger = MagicMock()
        assert widget._registered_projects() == []

    def test_returns_existing_dirs(self, tmp_path):
        dir_a = tmp_path / "projectA"
        dir_b = tmp_path / "projectB"
        dir_a.mkdir()
        dir_b.mkdir()

        widget = _make_widget(tmp_path, user_workdirs={1: [str(dir_a), str(dir_b)]})
        result = widget._registered_projects()
        assert os.path.realpath(str(dir_a)) in result
        assert os.path.realpath(str(dir_b)) in result

    def test_skips_nonexistent_dirs(self, tmp_path):
        nonexistent = str(tmp_path / "does_not_exist")
        widget = _make_widget(tmp_path, user_workdirs={1: [nonexistent]})
        assert widget._registered_projects() == []

    def test_deduplicates_across_users(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        widget = _make_widget(tmp_path, user_workdirs={
            1: [str(shared)],
            2: [str(shared)],
        })
        result = widget._registered_projects()
        assert result.count(os.path.realpath(str(shared))) == 1

    def test_accepts_string_value(self, tmp_path):
        dir_a = tmp_path / "projectA"
        dir_a.mkdir()
        widget = _make_widget(tmp_path, user_workdirs={1: str(dir_a)})
        result = widget._registered_projects()
        assert os.path.realpath(str(dir_a)) in result


# ---------------------------------------------------------------------------
# ProjectPickerDialog
# ---------------------------------------------------------------------------

class TestProjectPickerDialog:
    def test_chosen_path_none_before_accept(self, qtbot):
        dlg = ProjectPickerDialog(["/tmp"], "ru")
        qtbot.addWidget(dlg)
        assert dlg.chosen_path() is None

    def test_browse_sentinel_on_browse(self, qtbot):
        dlg = ProjectPickerDialog(["/tmp"], "en")
        qtbot.addWidget(dlg)
        dlg._accept_browse()
        assert dlg.chosen_path() == ProjectPickerDialog.BROWSE_SENTINEL

    def test_accept_selection_sets_path(self, qtbot):
        dlg = ProjectPickerDialog(["/tmp/a", "/tmp/b"], "ru")
        qtbot.addWidget(dlg)
        dlg._list.setCurrentRow(1)
        dlg._accept_selection()
        assert dlg.chosen_path() == "/tmp/b"

    def test_no_selection_does_not_accept(self, qtbot):
        dlg = ProjectPickerDialog(["/tmp"], "ru")
        qtbot.addWidget(dlg)
        dlg._list.clearSelection()
        dlg._list.setCurrentRow(-1)
        dlg._accept_selection()
        # Dialog not accepted, path stays None
        assert dlg.chosen_path() is None

    def test_title_uses_locale(self, qtbot):
        from i18n import t
        dlg = ProjectPickerDialog(["/tmp"], "en")
        qtbot.addWidget(dlg)
        assert dlg.windowTitle() == t("desktop.sessmgr.pick_title", "en")


# ---------------------------------------------------------------------------
# _pick_workdir
# ---------------------------------------------------------------------------

class TestPickWorkdir:
    def test_falls_through_to_filedialog_when_no_projects(self, tmp_path, qtbot):
        widget = _make_widget(tmp_path, user_workdirs={})
        target = str(tmp_path / "chosen")
        os.makedirs(target, exist_ok=True)

        with patch(
            "desktop.widgets.session_manager.QFileDialog.getExistingDirectory",
            return_value=target,
        ):
            result = widget._pick_workdir("ru", "")
        assert result == os.path.abspath(target)

    def test_returns_none_when_filedialog_cancelled(self, tmp_path, qtbot):
        widget = _make_widget(tmp_path, user_workdirs={})
        with patch(
            "desktop.widgets.session_manager.QFileDialog.getExistingDirectory",
            return_value="",
        ):
            result = widget._pick_workdir("ru", "")
        assert result is None

    def test_returns_selected_project_without_filedialog(self, tmp_path, qtbot):
        dir_a = tmp_path / "projectA"
        dir_a.mkdir()
        widget = _make_widget(tmp_path, user_workdirs={1: [str(dir_a)]})

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = QDialog.DialogCode.Accepted
        mock_dlg.chosen_path.return_value = str(dir_a)

        with patch(
            "desktop.widgets.session_manager.ProjectPickerDialog",
            return_value=mock_dlg,
        ), patch(
            "desktop.widgets.session_manager.QFileDialog.getExistingDirectory"
        ) as mock_fd:
            result = widget._pick_workdir("ru", "")

        mock_fd.assert_not_called()
        assert result == os.path.abspath(str(dir_a))

    def test_opens_filedialog_when_browse_sentinel(self, tmp_path, qtbot):
        dir_a = tmp_path / "projectA"
        dir_a.mkdir()
        chosen_via_fd = str(tmp_path / "other")
        os.makedirs(chosen_via_fd, exist_ok=True)
        widget = _make_widget(tmp_path, user_workdirs={1: [str(dir_a)]})

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = QDialog.DialogCode.Accepted
        mock_dlg.chosen_path.return_value = _BROWSE_SENTINEL

        with patch(
            "desktop.widgets.session_manager.ProjectPickerDialog",
            return_value=mock_dlg,
        ), patch(
            "desktop.widgets.session_manager.QFileDialog.getExistingDirectory",
            return_value=chosen_via_fd,
        ):
            result = widget._pick_workdir("ru", "")

        assert result == os.path.abspath(chosen_via_fd)

    def test_cancel_picker_returns_none(self, tmp_path, qtbot):
        dir_a = tmp_path / "projectA"
        dir_a.mkdir()
        widget = _make_widget(tmp_path, user_workdirs={1: [str(dir_a)]})

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = QDialog.DialogCode.Rejected

        with patch(
            "desktop.widgets.session_manager.ProjectPickerDialog",
            return_value=mock_dlg,
        ):
            result = widget._pick_workdir("ru", "")
        assert result is None

    def test_root_dir_validation_via_filedialog(self, tmp_path, qtbot):
        """Выбор директории вне root_dir через QFileDialog → None + warning."""
        widget = _make_widget(tmp_path, user_workdirs={})
        root = str(tmp_path / "root")
        outside = str(tmp_path / "outside")
        os.makedirs(root, exist_ok=True)
        os.makedirs(outside, exist_ok=True)

        with patch(
            "desktop.widgets.session_manager.QFileDialog.getExistingDirectory",
            return_value=outside,
        ), patch(
            "desktop.widgets.session_manager.QMessageBox.warning"
        ) as mock_warn:
            result = widget._pick_workdir("ru", root)

        assert result is None
        mock_warn.assert_called_once()
