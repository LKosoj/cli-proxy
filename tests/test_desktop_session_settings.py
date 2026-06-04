import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from desktop.widgets.session_settings import SSHHostsPanel, SessionSettingsWidget
from PySide6.QtWidgets import QApplication

from app.services.ssh_config_loader import load_ssh_config, load_ssh_secrets, save_ssh_config
from config import SSHHostConfig
from i18n import t
from session import ModeState, session_runtime_uid


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_session_settings_widget_init():
    facade = MagicMock()
    facade.config.tools = {}

    widget = SessionSettingsWidget(facade)
    assert widget is not None
    assert widget.rc_check is not None
    assert widget.rc_host_combo is not None
    assert widget.rc_recheck_btn is not None
    assert widget.status_banner is not None
    assert widget.rc_group is not None
    assert widget.rc_effective_label is not None


def test_facade_update_session_setting_methods():
    from desktop.services.application_facade import ApplicationFacade
    facade = ApplicationFacade(
        config_service=MagicMock(),
        session_service=MagicMock(),
        task_service=MagicMock(),
    )
    assert hasattr(facade, "update_session_setting")
    assert hasattr(facade, "test_ssh_connection")
    assert hasattr(facade, "generate_ssh_key")
    assert hasattr(facade, "get_remote_control_settings")
    assert hasattr(facade, "update_remote_control")
    assert hasattr(facade, "recheck_remote_control")


def test_session_settings_rc_ui_update_remote():
    facade = MagicMock()
    facade.config.tools = {}

    widget = SessionSettingsWidget(facade)
    rc_settings = {
        "remote_control_enabled": True,
        "remote_control_host_alias": "prod",
        "remote_control_hosts": {
            "prod": {"remote_project_root": "/app"}
        },
        "effective": {
            "execution_target": "remote",
            "host_alias": "prod",
            "remote_project_root": "/app"
        }
    }
    widget._update_rc_ui(rc_settings)
    assert widget.rc_check.isChecked() is True
    assert widget.rc_host_combo.currentText() == "prod"
    assert t("desktop.sesssettings.exec_target_remote", "ru", host="prod", root="/app") == widget.status_banner.text()
    assert "prod" in widget.status_banner.text()
    assert "remote" in widget.rc_effective_label.text()
    assert "prod" in widget.rc_effective_label.text()


def test_session_settings_rc_ui_update_local():
    facade = MagicMock()
    facade.config.tools = {}

    widget = SessionSettingsWidget(facade)
    rc_settings = {
        "remote_control_enabled": False,
        "remote_control_host_alias": None,
        "remote_control_hosts": {},
        "effective": {
            "execution_target": "local",
            "host_alias": None,
            "remote_project_root": None
        }
    }
    widget._update_rc_ui(rc_settings)
    assert widget.rc_check.isChecked() is False
    assert t("desktop.files.exec_target_local", "ru") == widget.status_banner.text()
    assert "local" in widget.rc_effective_label.text()


def test_session_settings_rc_ui_shows_reason_when_hosts_are_not_eligible():
    facade = MagicMock()
    facade.config.tools = {}

    widget = SessionSettingsWidget(facade)
    rc_settings = {
        "remote_control_enabled": False,
        "remote_control_host_alias": None,
        "remote_control_hosts": {
            "Mb_test": {"remote_project_root": None},
        },
        "effective": {
            "execution_target": "local",
            "host_alias": None,
            "remote_project_root": None,
        },
    }
    widget._update_rc_ui(rc_settings)

    assert widget.rc_host_combo.count() == 1
    assert widget.rc_host_combo.currentData() is None
    assert widget.rc_host_combo.currentText() == t("desktop.sesssettings.no_eligible_hosts", "ru")
    assert widget.rc_host_combo.isEnabled() is False
    assert widget.rc_error_label.isHidden() is False
    assert "remote_project_root" in widget.rc_error_label.text()


def test_parity_desktop_has_all_miniapp_rc_features():
    """Parity checklist: Desktop has same RC features as MiniApp."""
    facade = MagicMock()
    facade.config.tools = {}
    widget = SessionSettingsWidget(facade)

    # SSH Remote toggle
    assert hasattr(widget, "ssh_check"), "Missing SSH Remote toggle"
    # Remote Control toggle
    assert hasattr(widget, "rc_check"), "Missing RC toggle"
    # Host selector
    assert hasattr(widget, "rc_host_combo"), "Missing host selector"
    # Recheck button
    assert hasattr(widget, "rc_recheck_btn"), "Missing recheck action"
    # Status banner
    assert hasattr(widget, "status_banner"), "Missing status banner"
    assert t("desktop.files.exec_target_local", "ru") == widget.status_banner.text()
    # Effective target inline
    assert hasattr(widget, "rc_effective_label"), "Missing effective label"
    # Error label
    assert hasattr(widget, "rc_error_label"), "Missing error label"

    # Backend contract parity
    from desktop.services.application_facade import ApplicationFacade
    for method in ["get_remote_control_settings", "update_remote_control", "recheck_remote_control"]:
        assert hasattr(ApplicationFacade, method), f"Missing backend method: {method}"


def test_get_remote_control_settings_matches_miniapp_shape(tmp_path):
    from desktop.services.application_facade import ApplicationFacade

    config_service = MagicMock()
    session_service = MagicMock()
    task_service = MagicMock()

    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
    )
    session = _make_desktop_remote_session(tmp_path, enabled=True, host_alias="prod")
    session_service.get_session_by_uid.return_value = session

    save_ssh_config(str(tmp_path), {
        "prod": SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/prod"),
    })

    payload = facade.get_remote_control_settings(session_runtime_uid(session))

    assert payload is not None
    assert payload["remote_control_enabled"] is True
    assert payload["remote_control_host_alias"] == "prod"
    assert payload["remote_control_hosts"]["prod"]["host"] == "10.0.0.1"
    assert payload["remote_control_hosts"]["prod"]["remote_project_root"] == "/srv/prod"
    assert payload["effective"] == {
        "execution_target": "remote",
        "host_alias": "prod",
        "remote_project_root": "/srv/prod",
        "git_available": True,
    }


def test_session_settings_recheck_updates_error_and_banner():
    facade = MagicMock()
    facade.config.tools = {}
    facade.recheck_remote_control = AsyncMock(return_value={
        "ok": True,
        "preflight": {
            "ok": False,
            "error": "refused",
        },
    })
    facade.get_remote_control_settings.return_value = {
        "remote_control_enabled": True,
        "remote_control_host_alias": "prod",
        "remote_control_hosts": {
            "prod": {"remote_project_root": "/srv/prod"},
        },
        "effective": {
            "execution_target": "remote",
            "host_alias": "prod",
            "remote_project_root": "/srv/prod",
        },
    }

    widget = SessionSettingsWidget(facade)
    widget._session_uid = "chat:1:s1"

    with patch(
        "desktop.widgets.session_settings.ensure_async",
        side_effect=lambda coro, parent=None: asyncio.run(coro),
    ):
        widget._on_rc_recheck_clicked()

    facade.recheck_remote_control.assert_awaited_once_with("chat:1:s1")
    assert widget.rc_error_label.isHidden() is False
    assert widget.rc_error_label.text() == t("desktop.sesssettings.preflight_failed", "ru", error="refused")
    assert t("desktop.sesssettings.exec_target_remote", "ru", host="prod", root="/srv/prod") == widget.status_banner.text()
    assert "prod" in widget.status_banner.text()
    assert "/srv/prod" in widget.status_banner.text()


def test_ssh_hosts_panel_autogenerates_password_env_and_remote_root(tmp_path):
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QLineEdit,
        QSpinBox,
    )

    panel = SSHHostsPanel(MagicMock())
    panel._workdir = str(tmp_path)

    host_edit = QLineEdit("83.69.203.41")
    port_spin = QSpinBox()
    port_spin.setRange(1, 65535)
    port_spin.setValue(37121)
    user_edit = QLineEdit("la")
    auth_combo = QComboBox()
    auth_combo.addItems(["key", "password"])
    auth_combo.setCurrentText("password")
    key_file_edit = QLineEdit("")
    key_pass_env_edit = QLineEdit("")
    pwd_env_edit = QLineEdit("")
    pwd_val_edit = QLineEdit("secret-text")
    sudo_check = QCheckBox()
    sudo_pass_env_edit = QLineEdit("")
    sudo_pass_val_edit = QLineEdit("")
    roles_edit = QLineEdit("")
    desc_edit = QLineEdit("Production")
    timeout_spin = QSpinBox()
    timeout_spin.setRange(1, 3600 * 24)
    timeout_spin.setValue(1200)
    remote_root_edit = QLineEdit("/srv/app")

    with patch("desktop.widgets.session_settings.QMessageBox.information"), patch(
        "desktop.widgets.session_settings.QMessageBox.warning",
    ):
        panel._save_host(
            "Mb_test",
            host_edit,
            port_spin,
            user_edit,
            auth_combo,
            key_file_edit,
            key_pass_env_edit,
            pwd_env_edit,
            pwd_val_edit,
            sudo_check,
            sudo_pass_env_edit,
            sudo_pass_val_edit,
            roles_edit,
            desc_edit,
            timeout_spin,
            remote_root_edit,
        )

    hosts = load_ssh_config(str(tmp_path))
    assert hosts["Mb_test"].password_env == "SSH_MB_TEST_PASSWORD"
    assert hosts["Mb_test"].remote_project_root == "/srv/app"
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets["SSH_MB_TEST_PASSWORD"] == "secret-text"


class _FakeDesktopSSHService:
    def __init__(self, *, error: str | None = None) -> None:
        self._error = error
        self.calls = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((str(workdir), str(host_alias), str(command), int(timeout_sec)))
        if self._error:
            raise ConnectionError(self._error)
        return types.SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)


def _make_desktop_remote_session(tmp_path, *, enabled=False, host_alias=None):
    session = types.SimpleNamespace(
        id="s1",
        name="Desktop Session",
        chat_id=1,
        workdir=str(tmp_path),
        busy=False,
        run_lock=None,
        conversation_scope=types.SimpleNamespace(chat_id=1, session_uid="chat:1:s1"),
        modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=enabled,
            remote_control_host_alias=host_alias,
        ),
    )
    session.is_active_by_tick = lambda: False
    return session


@pytest.mark.asyncio
async def test_facade_update_remote_control_logs_audit_events(tmp_path):
    from desktop.services.application_facade import ApplicationFacade

    config_service = MagicMock()
    session_service = MagicMock()
    session_service._manager = MagicMock()
    task_service = MagicMock()
    logger = MagicMock()

    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        logger=logger,
    )
    facade.ssh_service = _FakeDesktopSSHService()
    session = _make_desktop_remote_session(tmp_path)
    session_service.get_session_by_uid.return_value = session

    save_ssh_config(str(tmp_path), {
        "prod": SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/prod"),
        "staging": SSHHostConfig(host="10.0.0.2", user="deploy", remote_project_root="/srv/staging"),
    })

    uid = session_runtime_uid(session)
    result = await facade.update_remote_control(uid, host_alias="prod", enabled=True)
    assert result["ok"] is True
    result = await facade.update_remote_control(uid, host_alias="staging")
    assert result["ok"] is True
    result = await facade.update_remote_control(uid, enabled=False)
    assert result["ok"] is True

    info_calls = {
        call.args[0]: call.kwargs["extra"]
        for call in logger.info.call_args_list
        if call.args and call.args[0] in {
            "remote_control_enabled",
            "remote_control_host_changed",
            "remote_control_disabled",
        }
    }

    enabled_extra = info_calls["remote_control_enabled"]
    assert enabled_extra["actor"] == "desktop:default"
    assert enabled_extra["session_uid"] == uid
    assert enabled_extra["surface"] == "desktop"
    assert enabled_extra["provider"] == "local"
    assert enabled_extra["host"] == "10.0.0.1"
    assert enabled_extra["remote_project_root"] == "/srv/prod"
    assert enabled_extra["result"] == "ok"

    changed_extra = info_calls["remote_control_host_changed"]
    assert changed_extra["host"] == "10.0.0.2"
    assert changed_extra["remote_project_root"] == "/srv/staging"
    assert changed_extra["result"] == "ok"

    disabled_extra = info_calls["remote_control_disabled"]
    assert disabled_extra["host"] == "10.0.0.2"
    assert disabled_extra["remote_project_root"] == "/srv/staging"
    assert disabled_extra["result"] == "ok"


@pytest.mark.asyncio
async def test_facade_recheck_remote_control_logs_preflight_failure(tmp_path):
    from desktop.services.application_facade import ApplicationFacade

    config_service = MagicMock()
    session_service = MagicMock()
    session_service._manager = MagicMock()
    task_service = MagicMock()
    logger = MagicMock()

    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        logger=logger,
    )
    facade.ssh_service = _FakeDesktopSSHService(error="refused")
    session = _make_desktop_remote_session(tmp_path, enabled=True, host_alias="prod")
    session_service.get_session_by_uid.return_value = session

    save_ssh_config(str(tmp_path), {
        "prod": SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/prod"),
    })

    uid = session_runtime_uid(session)
    result = await facade.recheck_remote_control(uid)
    assert result["ok"] is True
    assert result["preflight"]["ok"] is False

    calls = [
        call for call in logger.info.call_args_list
        if call.args and call.args[0] == "remote_control_preflight_failed"
    ]
    assert calls
    extra = calls[-1].kwargs["extra"]
    assert extra["actor"] == "desktop:default"
    assert extra["session_uid"] == uid
    assert extra["surface"] == "desktop"
    assert extra["provider"] == "local"
    assert extra["host"] == "10.0.0.1"
    assert extra["remote_project_root"] == "/srv/prod"
    assert extra["result"] == "error"
    assert "refused" in extra["reason"]
