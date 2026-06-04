"""Tests for Desktop Files/Editor parity: remote banner, diff dialog, force save."""

import asyncio
import inspect
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _build_bot_app(tmp_path):
    from bot import BotApp
    from config import (
        AppConfig,
        DefaultsConfig,
        MCPConfig,
        MiniAppConfig,
        TelegramConfig,
        ToolConfig,
    )

    cfg = AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=True),
    )
    return BotApp(cfg)


# ---------------------------------------------------------------------------
# RemoteModeBanner
# ---------------------------------------------------------------------------


class TestRemoteModeBanner:
    def test_init_shows_local(self):
        from desktop.widgets.remote_mode_banner import RemoteModeBanner
        from i18n import t
        banner = RemoteModeBanner()
        assert banner.text == t("desktop.files.exec_target_local", "ru")

    def test_update_to_remote(self):
        from desktop.widgets.remote_mode_banner import RemoteModeBanner
        banner = RemoteModeBanner()
        banner.update_state("remote", "prod", "/srv/app")
        assert "prod" in banner.text
        assert "/srv/app" in banner.text

    def test_update_back_to_local(self):
        from desktop.widgets.remote_mode_banner import RemoteModeBanner
        from i18n import t
        banner = RemoteModeBanner()
        banner.update_state("remote", "prod", "/srv/app")
        banner.update_state("local")
        assert banner.text == t("desktop.files.exec_target_local", "ru")
        assert "prod" not in banner.text


# ---------------------------------------------------------------------------
# ConflictDiffDialog
# ---------------------------------------------------------------------------


class TestConflictDiffDialog:
    def test_dialog_init(self):
        from desktop.widgets.remote_mode_banner import ConflictDiffDialog
        dlg = ConflictDiffDialog(
            path="file.py",
            expected_revision="aaa111",
            current_revision="bbb222",
            diff_unified="--- a\n+++ b\n",
        )
        assert dlg.windowTitle() == "File Conflict: file.py"
        assert dlg.force_accepted is False

    def test_force_accepted_default_false(self):
        from desktop.widgets.remote_mode_banner import ConflictDiffDialog
        dlg = ConflictDiffDialog("f.py", "a", "b", "diff")
        assert dlg.force_accepted is False

    def test_force_save_confirmation_accepts_dialog(self):
        from desktop.widgets.remote_mode_banner import ConflictDiffDialog

        dlg = ConflictDiffDialog(
            path="file.py",
            expected_revision="a" * 64,
            current_revision="b" * 64,
            diff_unified="--- yours\n+++ current\n",
        )
        with patch(
            "desktop.widgets.remote_mode_banner.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dlg._on_force()

        assert dlg.force_accepted is True
        assert dlg.result() == dlg.DialogCode.Accepted


# ---------------------------------------------------------------------------
# Git panel remote banner
# ---------------------------------------------------------------------------


class TestGitPanelRemoteBanner:
    def test_git_panel_has_remote_banner(self):
        from desktop.widgets.git_panel import GitPanelWidget
        from i18n import t
        facade = MagicMock()
        panel = GitPanelWidget(facade)
        assert hasattr(panel, "remote_banner")
        assert panel.remote_banner.text == t("desktop.files.exec_target_local", "ru")

    def test_git_panel_banner_updates_on_set_session(self):
        from desktop.widgets.git_panel import GitPanelWidget
        facade = MagicMock()
        facade.get_remote_control_settings.return_value = {
            "effective": {
                "execution_target": "remote",
                "host_alias": "staging",
                "remote_project_root": "/opt/proj",
            }
        }
        panel = GitPanelWidget(facade)

        session = MagicMock()
        session.workdir = "/tmp/test"
        panel.set_session(session)
        assert "staging" in panel.remote_banner.text
        assert "/opt/proj" in panel.remote_banner.text

    def test_git_panel_banner_local_when_no_rc(self):
        from desktop.widgets.git_panel import GitPanelWidget
        facade = MagicMock()
        facade.get_remote_control_settings.return_value = None
        panel = GitPanelWidget(facade)

        session = MagicMock()
        session.workdir = "/tmp/test"
        panel.set_session(session)
        from i18n import t
        assert panel.remote_banner.text == t("desktop.files.exec_target_local", "ru")


# ---------------------------------------------------------------------------
# Facade force_save_file
# ---------------------------------------------------------------------------


class TestFacadeForceSave:
    def test_facade_has_force_save_file(self):
        from desktop.services.application_facade import ApplicationFacade
        assert hasattr(ApplicationFacade, "force_save_file")


# ---------------------------------------------------------------------------
# Parity checklist
# ---------------------------------------------------------------------------


class TestDesktopFilesEditorParity:
    """Verify Desktop has all MiniApp Files/Editor remote features."""

    def test_remote_banner_widget_exists(self):
        from desktop.widgets.remote_mode_banner import RemoteModeBanner
        banner = RemoteModeBanner()
        assert banner is not None

    def test_conflict_diff_dialog_exists(self):
        from desktop.widgets.remote_mode_banner import ConflictDiffDialog
        dlg = ConflictDiffDialog("f", "a", "b", "d")
        assert dlg is not None

    def test_facade_has_force_save(self):
        from desktop.services.application_facade import ApplicationFacade
        assert callable(getattr(ApplicationFacade, "force_save_file", None))

    def test_git_panel_has_banner(self):
        from desktop.widgets.git_panel import GitPanelWidget
        facade = MagicMock()
        panel = GitPanelWidget(facade)
        assert hasattr(panel, "remote_banner")

    def test_backend_contract_same_as_miniapp(self):
        """Desktop uses the shared session files service contract."""
        from app.services.session_files_service import SessionFilesService
        import inspect
        for method in ("tree", "read", "write", "create", "delete", "download", "search", "meta"):
            assert callable(getattr(SessionFilesService, method, None))
        sig = inspect.signature(SessionFilesService.write)
        assert "force" in sig.parameters

    def test_desktop_remote_runtime_supports_tree_read_create_write_delete(self, tmp_path):
        from app.services.ssh_config_loader import save_ssh_config
        from app.services.session_files_service import SessionFilesService
        from config import SSHHostConfig
        from session import session_runtime_uid
        from tests.test_remote_files_provider import FakeRemoteFS, FakeSSHService

        app = _build_bot_app(tmp_path)
        try:
            remote_fs = FakeRemoteFS()
            remote_fs.add_dir("/srv/app")
            remote_fs.add_dir("/srv/app/docs")
            remote_fs.add_file("/srv/app/hello.txt", "hello remote")
            app.ssh_service = FakeSSHService(remote_fs)

            session = app.manager.create(1, "dummy", str(tmp_path))
            session.modes.ssh_remote_enabled = True
            session.modes.remote_control_enabled = True
            session.modes.remote_control_host_alias = "prod"

            save_ssh_config(str(tmp_path), {
                "prod": SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/app"),
            })

            svc = SessionFilesService(app)
            session_uid = session_runtime_uid(session)

            tree = svc.tree(1, session_uid, ".")
            if inspect.isawaitable(tree):
                tree = asyncio.run(tree)
            assert [(item["name"], item["is_dir"]) for item in tree["items"]] == [
                ("docs", True),
                ("hello.txt", False),
            ]

            read = svc.read(1, session_uid, "hello.txt")
            if inspect.isawaitable(read):
                read = asyncio.run(read)
            assert read["content"] == "hello remote"

            created_dir = svc.create(1, session_uid, "newdir", "dir")
            if inspect.isawaitable(created_dir):
                created_dir = asyncio.run(created_dir)
            assert created_dir == {"ok": True}
            created_file = svc.create(1, session_uid, "newdir/note.txt", "file")
            if inspect.isawaitable(created_file):
                created_file = asyncio.run(created_file)
            assert created_file == {"ok": True}
            written = svc.write(1, session_uid, "newdir/note.txt", "desktop parity\n", None)
            assert written["ok"] is True
            reread = svc.read(1, session_uid, "newdir/note.txt")
            if inspect.isawaitable(reread):
                reread = asyncio.run(reread)
            assert reread["content"] == "desktop parity\n"

            deleted_file = svc.delete(1, session_uid, "newdir/note.txt")
            if inspect.isawaitable(deleted_file):
                deleted_file = asyncio.run(deleted_file)
            assert deleted_file == {"ok": True}
            deleted_dir = svc.delete(1, session_uid, "newdir")
            if inspect.isawaitable(deleted_dir):
                deleted_dir = asyncio.run(deleted_dir)
            assert deleted_dir == {"ok": True}
            assert Path("/srv/app/newdir/note.txt").as_posix() not in remote_fs.files
            assert Path("/srv/app/newdir").as_posix() not in remote_fs.dirs
        finally:
            app.shutdown_html_process_pool()

    def test_facade_force_save_file_without_bot_app_returns_error_shape(self):
        from desktop.services.application_facade import ApplicationFacade

        facade = ApplicationFacade(
            config_service=MagicMock(),
            session_service=MagicMock(),
            task_service=MagicMock(),
            logger=MagicMock(),
        )

        result = facade.force_save_file("missing-session", 1, "force.txt", "content")

        assert result == {"ok": False, "error": "bot_app not available"}

    def test_facade_force_save_file_domain_error_returns_error_shape(self, tmp_path):
        from desktop.services.application_facade import ApplicationFacade
        from session import session_runtime_uid

        app = _build_bot_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            session_uid = session_runtime_uid(session)
            logger = MagicMock()
            facade = ApplicationFacade(
                config_service=MagicMock(),
                session_service=MagicMock(),
                task_service=MagicMock(),
                logger=logger,
            )
            facade._bot_app = app

            result = facade.force_save_file(session_uid, 1, "missing.txt", "content")

            assert result == {"ok": False, "error": "file not found"}
            logger.warning.assert_called_once()
            assert logger.warning.call_args.args[0] == (
                "desktop force_save_file rejected session_uid=%s path=%s error=%s"
            )
        finally:
            app.shutdown_html_process_pool()

    def test_facade_force_save_file_remote_updates_content_and_logs_audit(self, tmp_path):
        from app.services.ssh_config_loader import save_ssh_config
        from config import SSHHostConfig
        from desktop.services.application_facade import ApplicationFacade
        from session import session_runtime_uid
        from tests.test_remote_files_provider import FakeRemoteFS, FakeSSHService

        app = _build_bot_app(tmp_path)
        try:
            remote_fs = FakeRemoteFS()
            remote_fs.add_dir("/srv/app")
            remote_fs.add_file("/srv/app/force.txt", "remote v1")
            app.ssh_service = FakeSSHService(remote_fs)

            session = app.manager.create(1, "dummy", str(tmp_path))
            session.modes.ssh_remote_enabled = True
            session.modes.remote_control_enabled = True
            session.modes.remote_control_host_alias = "prod"
            session_uid = session_runtime_uid(session)

            save_ssh_config(str(tmp_path), {
                "prod": SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/app"),
            })

            logger = MagicMock()
            facade = ApplicationFacade(
                config_service=MagicMock(),
                session_service=MagicMock(),
                task_service=MagicMock(),
                logger=logger,
            )
            facade._bot_app = app

            result = facade.force_save_file(session_uid, 1, "force.txt", "forced remote\n")

            assert result["ok"] is True
            assert result["forced"] is True
            assert remote_fs.file_content("/srv/app/force.txt") == b"forced remote\n"

            calls = [
                call for call in logger.info.call_args_list
                if call.args and call.args[0] == "remote_file_force_saved"
            ]
            assert calls
            extra = calls[-1].kwargs["extra"]
            assert extra["surface"] == "desktop"
            assert extra["provider"] == "remote"
            assert extra["action"] == "remote_file_force_saved"
            assert extra["path"] == "force.txt"
            assert extra["status"] == "ok"
        finally:
            app.shutdown_html_process_pool()
