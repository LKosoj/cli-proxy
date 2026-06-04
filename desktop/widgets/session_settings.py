from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Optional, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLineEdit,
    QCheckBox,
    QSpinBox,
    QComboBox,
    QPushButton,
    QLabel,
    QScrollArea,
    QGroupBox,
    QListWidget,
    QStackedWidget,
    QMessageBox,
    QButtonGroup,
    QRadioButton,
    QDialog,
    QPlainTextEdit,
    QFrame,
    QInputDialog
)

from i18n import t
from session import session_runtime_uid
from utils.ui import ensure_async
from app.services.ssh_config_loader import (
    build_ssh_secret_env_name,
    load_ssh_config,
    save_ssh_config,
    save_ssh_secret
)
from sessions.session_state_access import (
    get_active_mode,
    is_orchestrator_enabled,
    is_ssh_remote_enabled
)

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from session import Session
    from config import SSHHostConfig


class SSHHostsPanel(QWidget):
    """Панель управления SSH-хостами проекта."""

    def __init__(self, facade: ApplicationFacade, parent=None):
        super().__init__(parent)
        self.facade = facade
        self._workdir: Optional[str] = None
        self._hosts: Dict[str, SSHHostConfig] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Левая панель: список хостов + кнопки
        left = QVBoxLayout()
        self.host_list = QListWidget()
        self.host_list.setFixedWidth(150)
        left.addWidget(self.host_list)

        btn_row = QHBoxLayout()
        lang = self.facade.ui_language
        self.add_btn = QPushButton(t("desktop.btn.add", lang))
        self.add_btn.clicked.connect(self._on_add_clicked)
        self.del_btn = QPushButton(t("desktop.btn.delete", lang))
        self.del_btn.clicked.connect(self._on_delete_clicked)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.del_btn)
        left.addLayout(btn_row)

        # Правая панель: форма
        self.form_stack = QStackedWidget()
        self.empty_label = QLabel(t("desktop.ssh.empty_hint", lang))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.form_stack.addWidget(self.empty_label)

        layout.addLayout(left, 1)
        layout.addWidget(self.form_stack, 3)

        self.host_list.currentRowChanged.connect(self._on_host_selection_changed)

    def retranslate_ui(self, lang: str) -> None:
        """Re-set static labels and re-render dynamic host forms in the new language."""
        self.add_btn.setText(t("desktop.btn.add", lang))
        self.del_btn.setText(t("desktop.btn.delete", lang))
        self.empty_label.setText(t("desktop.ssh.empty_hint", lang))
        if self._workdir:
            self.load_hosts(self._workdir)

    def load_hosts(self, workdir: str):
        """Загрузить хосты из ssh.yaml и заполнить список."""
        self._workdir = workdir
        self._hosts = load_ssh_config(workdir)
        self.host_list.clear()

        # Clear stack except empty label
        while self.form_stack.count() > 1:
            widget = self.form_stack.widget(1)
            self.form_stack.removeWidget(widget)
            widget.deleteLater()

        for alias, cfg in self._hosts.items():
            self.host_list.addItem(alias)
            form = self._create_ssh_host_form(alias, cfg)
            self.form_stack.addWidget(form)

        if self.host_list.count() > 0:
            self.host_list.setCurrentRow(0)
        else:
            self.form_stack.setCurrentIndex(0)

    def _on_host_selection_changed(self, index: int):
        if index < 0:
            self.form_stack.setCurrentIndex(0)
        else:
            self.form_stack.setCurrentIndex(index + 1)

    def _create_ssh_host_form(self, alias: str, cfg: SSHHostConfig) -> QWidget:
        lang = self.facade.ui_language
        container = QWidget()
        layout = QFormLayout(container)

        host_edit = QLineEdit(cfg.host)
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(cfg.port)
        user_edit = QLineEdit(cfg.user)

        auth_combo = QComboBox()
        auth_combo.addItems(["key", "password"])
        auth_combo.setCurrentText(cfg.auth)

        key_file_edit = QLineEdit(cfg.key_file or "")
        key_pass_env_edit = QLineEdit(cfg.key_passphrase_env or "")

        pwd_env_edit = QLineEdit(cfg.password_env or "")
        pwd_val_edit = QLineEdit()
        pwd_val_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pwd_val_edit.setPlaceholderText(t("desktop.ssh.pwd_keep_existing", lang))

        sudo_check = QCheckBox(t("desktop.ssh.enable_sudo", lang))
        sudo_check.setChecked(cfg.sudo)
        sudo_pass_env_edit = QLineEdit(cfg.sudo_password_env or "")
        sudo_pass_val_edit = QLineEdit()
        sudo_pass_val_edit.setEchoMode(QLineEdit.EchoMode.Password)

        remote_root_edit = QLineEdit(cfg.remote_project_root or "")
        remote_root_edit.setPlaceholderText(t("desktop.ssh.remote_root_placeholder", lang))

        roles_edit = QLineEdit(", ".join(cfg.roles))
        desc_edit = QLineEdit(cfg.description)
        timeout_spin = QSpinBox()
        timeout_spin.setRange(1, 3600 * 24)
        timeout_spin.setValue(cfg.idle_timeout_sec)

        layout.addRow(t("desktop.ssh.label_host", lang), host_edit)
        layout.addRow(t("desktop.ssh.label_port", lang), port_spin)
        layout.addRow(t("desktop.ssh.label_user", lang), user_edit)
        layout.addRow(t("desktop.ssh.label_auth", lang), auth_combo)

        # Key fields
        key_group = QGroupBox(t("desktop.ssh.group_key", lang))
        key_layout = QFormLayout(key_group)
        key_layout.addRow(t("desktop.ssh.label_key_file", lang), key_file_edit)
        key_layout.addRow(t("desktop.ssh.label_passphrase_env", lang), key_pass_env_edit)
        layout.addRow(key_group)

        # Password fields
        pwd_group = QGroupBox(t("desktop.ssh.group_password", lang))
        pwd_layout = QFormLayout(pwd_group)
        pwd_layout.addRow(t("desktop.ssh.label_password_env", lang), pwd_env_edit)
        pwd_layout.addRow(t("desktop.ssh.label_value", lang), pwd_val_edit)
        layout.addRow(pwd_group)

        # Sudo fields
        sudo_group = QGroupBox(t("desktop.ssh.group_sudo", lang))
        sudo_layout = QFormLayout(sudo_group)
        sudo_layout.addRow(sudo_check)
        sudo_layout.addRow(t("desktop.ssh.label_sudo_pass_env", lang), sudo_pass_env_edit)
        sudo_layout.addRow(t("desktop.ssh.label_value", lang), sudo_pass_val_edit)
        layout.addRow(sudo_group)

        layout.addRow(t("desktop.ssh.label_remote_root", lang), remote_root_edit)
        layout.addRow(t("desktop.ssh.label_roles", lang), roles_edit)
        layout.addRow(t("desktop.ssh.label_description", lang), desc_edit)
        layout.addRow(t("desktop.ssh.label_idle_timeout", lang), timeout_spin)

        def update_visibility():
            is_key = auth_combo.currentText() == "key"
            key_group.setVisible(is_key)
            pwd_group.setVisible(not is_key)

            is_sudo = sudo_check.isChecked()
            sudo_pass_env_edit.setEnabled(is_sudo)
            sudo_pass_val_edit.setEnabled(is_sudo)

        auth_combo.currentIndexChanged.connect(update_visibility)
        sudo_check.toggled.connect(update_visibility)
        update_visibility()

        # Action buttons
        btn_layout = QHBoxLayout()
        save_btn = QPushButton(t("desktop.btn.save_host", lang))
        save_btn.clicked.connect(lambda: self._save_host(
            alias, host_edit, port_spin, user_edit, auth_combo,
            key_file_edit, key_pass_env_edit, pwd_env_edit, pwd_val_edit,
            sudo_check, sudo_pass_env_edit, sudo_pass_val_edit,
            roles_edit, desc_edit, timeout_spin, remote_root_edit
        ))

        test_btn = QPushButton(t("desktop.btn.test_connection", lang))
        test_btn.clicked.connect(lambda: self._on_test_clicked(alias))

        keygen_btn = QPushButton(t("desktop.btn.generate_key", lang))
        keygen_btn.clicked.connect(lambda: self._on_keygen_clicked(alias))

        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(test_btn)
        btn_layout.addWidget(keygen_btn)
        layout.addRow(btn_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    def _on_add_clicked(self):
        lang = self.facade.ui_language
        alias, ok = QInputDialog.getText(
            self,
            t("desktop.ssh.dialog_add_title", lang),
            t("desktop.ssh.dialog_add_prompt", lang),
        )
        if not ok or not alias.strip():
            return

        alias = alias.strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", alias):
            QMessageBox.warning(
                self,
                t("desktop.ssh.invalid_alias_title", lang),
                t("desktop.ssh.invalid_alias_msg", lang),
            )
            return

        if alias in self._hosts:
            QMessageBox.warning(
                self,
                t("desktop.ssh.duplicate_alias_title", lang),
                t("desktop.ssh.duplicate_alias_msg", lang, alias=alias),
            )
            return

        from config import SSHHostConfig
        new_cfg = SSHHostConfig(host="", user="")
        self._hosts[alias] = new_cfg

        self.host_list.addItem(alias)
        form = self._create_ssh_host_form(alias, new_cfg)
        self.form_stack.addWidget(form)
        self.host_list.setCurrentRow(self.host_list.count() - 1)

    def _on_delete_clicked(self):
        row = self.host_list.currentRow()
        if row < 0:
            return
        alias = self.host_list.item(row).text()
        lang = self.facade.ui_language

        reply = QMessageBox.question(
            self,
            t("desktop.ssh.confirm_delete_title", lang),
            t("desktop.ssh.confirm_delete_msg", lang, alias=alias),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if alias in self._hosts:
                del self._hosts[alias]
                save_ssh_config(self._workdir, self._hosts)
                self.load_hosts(self._workdir)

    def _save_host(self, alias, host_edit, port_spin, user_edit, auth_combo,
                   key_file_edit, key_pass_env_edit, pwd_env_edit, pwd_val_edit,
                   sudo_check, sudo_pass_env_edit, sudo_pass_val_edit,
                   roles_edit, desc_edit, timeout_spin, remote_root_edit):
        from config import SSHHostConfig
        password_value = pwd_val_edit.text()
        sudo_password_value = sudo_pass_val_edit.text()
        password_env = pwd_env_edit.text().strip() or None
        sudo_password_env = sudo_pass_env_edit.text().strip() or None
        if auth_combo.currentText() == "password" and password_value and not password_env:
            password_env = build_ssh_secret_env_name(alias)
        if sudo_check.isChecked() and sudo_password_value and not sudo_password_env:
            sudo_password_env = build_ssh_secret_env_name(alias, sudo=True)

        lang = self.facade.ui_language
        remote_root = remote_root_edit.text().strip() or None
        if remote_root and not remote_root.startswith("/"):
            QMessageBox.warning(
                self,
                t("desktop.ssh.validation_error_title", lang),
                t("desktop.ssh.validation_remote_root_msg", lang),
            )
            return

        cfg = SSHHostConfig(
            host=host_edit.text().strip(),
            user=user_edit.text().strip(),
            auth=auth_combo.currentText(),
            port=port_spin.value(),
            key_file=key_file_edit.text().strip() or None,
            key_passphrase_env=key_pass_env_edit.text().strip() or None,
            password_env=password_env,
            sudo=sudo_check.isChecked(),
            sudo_password_env=sudo_password_env,
            idle_timeout_sec=timeout_spin.value(),
            roles=[r.strip() for r in roles_edit.text().split(",") if r.strip()],
            description=desc_edit.text().strip(),
            remote_project_root=remote_root,
        )

        if not cfg.host or not cfg.user:
            QMessageBox.warning(
                self,
                t("desktop.ssh.validation_error_title", lang),
                t("desktop.ssh.validation_host_user_required", lang),
            )
            return

        self._hosts[alias] = cfg
        save_ssh_config(self._workdir, self._hosts)

        # Save secrets if provided
        if password_value and cfg.password_env:
            save_ssh_secret(self._workdir, cfg.password_env, password_value)
            pwd_val_edit.clear()

        if sudo_password_value and cfg.sudo_password_env:
            save_ssh_secret(self._workdir, cfg.sudo_password_env, sudo_password_value)
            sudo_pass_val_edit.clear()

        QMessageBox.information(
            self,
            t("desktop.ssh.success_title", lang),
            t("desktop.ssh.host_saved_msg", lang, alias=alias),
        )

    def _on_test_clicked(self, alias: str):
        async def _test():
            lang = self.facade.ui_language
            try:
                result = await self.facade.test_ssh_connection(self._workdir, alias)
                if result.ok:
                    QMessageBox.information(
                        self,
                        t("desktop.ssh.test_result_title", lang),
                        t("desktop.ssh.test_ok_msg", lang, info=result.server_info),
                    )
                else:
                    QMessageBox.warning(
                        self,
                        t("desktop.ssh.test_result_title", lang),
                        t("desktop.ssh.test_fail_msg", lang, message=result.message),
                    )
            except Exception as e:
                QMessageBox.critical(
                    self,
                    t("desktop.ssh.error_title", lang),
                    t("desktop.ssh.test_exception_msg", lang, error=e),
                )

        ensure_async(_test(), parent=self)

    def _on_keygen_clicked(self, alias: str):
        lang = self.facade.ui_language
        reply = QMessageBox.question(
            self,
            t("desktop.ssh.confirm_keygen_title", lang),
            t("desktop.ssh.confirm_keygen_msg", lang, alias=alias),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        async def _keygen():
            lang = self.facade.ui_language
            try:
                result = await self.facade.generate_ssh_key(self._workdir, alias)

                # Update UI and config
                self._hosts[alias].key_file = os.path.relpath(result.private_path, self._workdir)
                self._hosts[alias].auth = "key"
                save_ssh_config(self._workdir, self._hosts)
                self.load_hosts(self._workdir)

                # Show public key
                msg = QDialog(self)
                msg.setWindowTitle(t("desktop.ssh.keygen_result_title", lang))
                msg_layout = QVBoxLayout(msg)
                msg_layout.addWidget(QLabel(t("desktop.ssh.keygen_copy_hint", lang)))
                text_edit = QPlainTextEdit(result.public_key_text)
                text_edit.setReadOnly(True)
                msg_layout.addWidget(text_edit)

                btns = QPushButton(t("desktop.ssh.keygen_close_btn", lang))
                btns.clicked.connect(msg.accept)
                msg_layout.addWidget(btns)
                msg.exec()

            except Exception as e:
                QMessageBox.critical(
                    self,
                    t("desktop.ssh.error_title", lang),
                    t("desktop.ssh.keygen_fail_msg", lang, error=e),
                )

        ensure_async(_keygen(), parent=self)


class SessionSettingsWidget(QWidget):
    """Панель настроек активной сессии."""

    settingChanged = Signal(str, str, object)  # session_uid, key, value

    def __init__(self, facade: ApplicationFacade, parent=None):
        super().__init__(parent)
        self.facade = facade
        self._session_uid: Optional[str] = None
        self._session: Optional[Session] = None
        self._loading = False
        self._rc_has_selectable_hosts = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        lang = self.facade.ui_language

        self.status_banner = QLabel(t("desktop.files.exec_target_local", lang))
        self.status_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_banner.setStyleSheet(
                "font-weight: bold; padding: 8px; background-color: #444; border-radius: 4px;"
            )
        layout.addWidget(self.status_banner)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.container = QWidget()
        self.form_layout = QFormLayout(self.container)

        # Name
        self.name_edit = QLineEdit()
        self.name_edit.editingFinished.connect(self._on_name_changed)
        self.name_label = QLabel(t("desktop.settings.session_name", lang))
        self.form_layout.addRow(self.name_label, self.name_edit)

        # CLI
        self.cli_group = QButtonGroup(self)
        self.cli_layout = QHBoxLayout()
        self.cli_row_label = QLabel(t("desktop.settings.cli", lang))
        self.form_layout.addRow(self.cli_row_label, self.cli_layout)

        # Mode
        self.mode_combo = QComboBox()
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.mode_label = QLabel(t("desktop.settings.active_mode", lang))
        self.form_layout.addRow(self.mode_label, self.mode_combo)

        # SSH
        self.ssh_check = QCheckBox(t("desktop.settings.ssh_enable", lang))
        self.ssh_check.toggled.connect(self._on_ssh_toggled)
        self.ssh_label = QLabel("")
        ssh_header = QHBoxLayout()
        ssh_header.addWidget(self.ssh_check)
        ssh_header.addWidget(self.ssh_label)
        self.ssh_row_label = QLabel(t("desktop.settings.ssh_remote", lang))
        self.form_layout.addRow(self.ssh_row_label, ssh_header)

        # SSH Hosts
        self.ssh_hosts_group = QGroupBox(t("desktop.settings.ssh_hosts_group", lang))
        ssh_hosts_layout = QVBoxLayout(self.ssh_hosts_group)
        self.ssh_hosts_panel = SSHHostsPanel(self.facade)
        ssh_hosts_layout.addWidget(self.ssh_hosts_panel)
        self.form_layout.addRow(self.ssh_hosts_group)
        self.ssh_hosts_group.setVisible(False)

        # Remote Control
        self.rc_group = QGroupBox(t("desktop.settings.rc_group", lang))
        rc_layout = QFormLayout(self.rc_group)
        self.rc_check = QCheckBox(t("desktop.settings.rc_enable", lang))
        self.rc_check.toggled.connect(self._on_rc_toggled)
        self.rc_host_combo = QComboBox()
        self.rc_host_combo.currentIndexChanged.connect(self._on_rc_host_changed)

        self.rc_recheck_btn = QPushButton(t("desktop.sesssettings.recheck", lang))
        self.rc_recheck_btn.clicked.connect(self._on_rc_recheck_clicked)

        self.rc_error_label = QLabel()
        self.rc_error_label.setStyleSheet("color: red;")
        self.rc_error_label.setWordWrap(True)
        self.rc_error_label.setVisible(False)

        self.rc_check_label = QLabel(t("desktop.settings.rc_label", lang))
        rc_layout.addRow(self.rc_check_label, self.rc_check)

        host_row = QHBoxLayout()
        host_row.addWidget(self.rc_host_combo)
        host_row.addWidget(self.rc_recheck_btn)
        self.rc_host_label = QLabel(t("desktop.settings.rc_target_host", lang))
        rc_layout.addRow(self.rc_host_label, host_row)

        self.rc_effective_label = QLabel(t("desktop.sesssettings.effective_local", lang))
        self.rc_effective_label.setStyleSheet("color: #888;")
        self.rc_effective_row_label = QLabel(t("desktop.settings.rc_effective_target", lang))
        rc_layout.addRow(self.rc_effective_row_label, self.rc_effective_label)

        rc_layout.addRow(self.rc_error_label)

        self.form_layout.addRow(self.rc_group)
        self.rc_group.setVisible(False)

        # Orchestrator
        self.orch_check = QCheckBox(t("desktop.settings.orchestrator_enable", lang))
        self.orch_check.toggled.connect(self._on_orch_toggled)
        self.orch_row_label = QLabel(t("desktop.settings.orchestrator", lang))
        self.form_layout.addRow(self.orch_row_label, self.orch_check)

        # App language selector
        self.lang_label = QLabel(t("desktop.settings.lang_label", lang))
        self.lang_combo = QComboBox()
        _LANG_ITEMS = [("ru", "Русский"), ("en", "English"), ("zh", "中文"), ("de", "Deutsch")]
        for code, label in _LANG_ITEMS:
            self.lang_combo.addItem(label, code)
        self.lang_save_btn = QPushButton(t("desktop.btn.save", lang))
        self.lang_save_btn.clicked.connect(self._on_language_save)
        lang_row = QHBoxLayout()
        lang_row.addWidget(self.lang_combo)
        lang_row.addWidget(self.lang_save_btn)
        self.form_layout.addRow(self.lang_label, lang_row)

        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

        self.setEnabled(False)

    def set_session(self, session: Optional[Session]):
        """Обновить виджет для активной сессии."""
        self._session = session
        if not session:
            self._session_uid = None
            self.setEnabled(False)
            return

        self._loading = True
        self._session_uid = session_runtime_uid(session)
        self.setEnabled(True)

        # Name
        session_name = getattr(session, "name", "")
        if type(session_name).__name__ == "MagicMock" or type(session_name).__name__ == "Mock":
            session_name = ""
        self.name_edit.setText(str(session_name) if session_name else "")

        # CLI
        self._rebuild_cli_buttons()

        # Mode
        self._rebuild_mode_combo()

        # Orchestrator
        self.orch_check.setChecked(is_orchestrator_enabled(session, False))

        # SSH
        ssh_enabled = is_ssh_remote_enabled(session, False)
        self.ssh_check.setChecked(ssh_enabled)

        from app.services.ssh_config_loader import ssh_remote_available, load_ssh_config
        available = ssh_remote_available(session.workdir)
        self.ssh_check.setVisible(available)
        self.ssh_hosts_group.setVisible(available and ssh_enabled)

        lang = self.facade.ui_language
        if available:
            hosts = load_ssh_config(session.workdir)
            self.ssh_label.setText(t("desktop.sesssettings.hosts_count", lang, n=len(hosts)))
            if ssh_enabled:
                self.ssh_hosts_panel.load_hosts(session.workdir)

            rc_settings = self.facade.get_remote_control_settings(self._session_uid)
            if rc_settings:
                self._update_rc_ui(rc_settings)
            else:
                self.rc_group.setVisible(False)
        else:
            self.ssh_label.setText(t("desktop.sesssettings.no_hosts", lang))
            self.rc_group.setVisible(False)
            self.status_banner.setText(t("desktop.files.exec_target_local", lang))
            self.status_banner.setStyleSheet(
                "font-weight: bold; padding: 8px; background-color: #444; border-radius: 4px;"
            )

        is_busy = getattr(session, "busy", False)
        self.name_edit.setEnabled(not is_busy)
        self.mode_combo.setEnabled(not is_busy)
        self.orch_check.setEnabled(not is_busy)
        self.ssh_check.setEnabled(not is_busy)
        self.rc_check.setEnabled(not is_busy)
        self.rc_host_combo.setEnabled(not is_busy and self._rc_has_selectable_hosts)
        for btn in self.cli_group.buttons():
            btn.setEnabled(not is_busy)

        # Sync language combo with current ui_language
        current_lang = self.facade.ui_language
        idx = self.lang_combo.findData(current_lang)
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)

        self._loading = False

    def _update_rc_ui(self, rc_settings: dict):
        lang = self.facade.ui_language
        self.rc_group.setVisible(True)
        rc_enabled = rc_settings.get("remote_control_enabled", False)
        self.rc_check.setChecked(rc_enabled)

        hosts = rc_settings.get("remote_control_hosts", {})
        self.rc_host_combo.blockSignals(True)
        self.rc_host_combo.clear()

        valid_hosts = []
        for alias, cfg in hosts.items():
            if cfg.get("remote_project_root"):
                valid_hosts.append(alias)

        self._rc_has_selectable_hosts = bool(valid_hosts)
        if self._rc_has_selectable_hosts:
            for alias in valid_hosts:
                self.rc_host_combo.addItem(alias, alias)
        elif hosts:
            self.rc_host_combo.addItem(t("desktop.sesssettings.no_eligible_hosts", lang), None)

        current_alias = rc_settings.get("remote_control_host_alias")
        if current_alias in valid_hosts:
            self.rc_host_combo.setCurrentText(current_alias)
        self.rc_host_combo.blockSignals(False)
        self.rc_host_combo.setEnabled(self._rc_has_selectable_hosts)

        effective = rc_settings.get("effective", {})
        target = effective.get("execution_target", "local")

        if target == "remote":
            host_alias = effective.get("host_alias", "unknown")
            root = effective.get("remote_project_root", "unknown")
            self.status_banner.setText(t("desktop.sesssettings.exec_target_remote", lang, host=host_alias, root=root))
            self.status_banner.setStyleSheet(
                "font-weight: bold; padding: 8px; background-color: #005A9E; color: white; border-radius: 4px;"
            )
            self.rc_effective_label.setText(f"remote → {host_alias}:{root}")
            self.rc_effective_label.setStyleSheet("color: #005A9E; font-weight: bold;")
        else:
            self.status_banner.setText(t("desktop.files.exec_target_local", lang))
            self.status_banner.setStyleSheet(
                "font-weight: bold; padding: 8px; background-color: #444; border-radius: 4px;"
            )
            self.rc_effective_label.setText(t("desktop.sesssettings.effective_local", lang))
            self.rc_effective_label.setStyleSheet("color: #888;")

        if hosts and not self._rc_has_selectable_hosts:
            self.rc_error_label.setStyleSheet("color: red;")
            self.rc_error_label.setText(t("desktop.sesssettings.rc_root_required", lang))
            self.rc_error_label.setVisible(True)
        else:
            self.rc_error_label.setVisible(False)
            self.rc_error_label.setText("")

    def _on_rc_toggled(self, checked: bool):
        if self._loading or not self._session_uid:
            return

        async def _update():
            result = await self.facade.update_remote_control(self._session_uid, enabled=checked)
            self._handle_rc_result(result)
        ensure_async(_update(), parent=self)

    def _on_rc_host_changed(self, index: int):
        if self._loading or not self._session_uid:
            return
        alias = self.rc_host_combo.currentData()
        if not alias:
            return

        async def _update():
            result = await self.facade.update_remote_control(self._session_uid, host_alias=alias)
            self._handle_rc_result(result)
        ensure_async(_update(), parent=self)

    def _on_rc_recheck_clicked(self):
        if self._loading or not self._session_uid:
            return

        async def _recheck():
            lang = self.facade.ui_language
            result = await self.facade.recheck_remote_control(self._session_uid)
            if not result.get("ok"):
                error = result.get("error", "Unknown error")
                self.rc_error_label.setText(t("desktop.sesssettings.recheck_failed", lang, error=error))
                self.rc_error_label.setVisible(True)
            else:
                pf = result.get("preflight", {})
                if not pf.get("ok"):
                    self.rc_error_label.setText(t("desktop.sesssettings.preflight_failed", lang, error=pf.get('error')))
                    self.rc_error_label.setVisible(True)
                else:
                    self.rc_error_label.setText(t("desktop.sesssettings.preflight_passed", lang))
                    self.rc_error_label.setStyleSheet("color: green;")
                    self.rc_error_label.setVisible(True)

            rc_settings = self.facade.get_remote_control_settings(self._session_uid)
            if rc_settings:
                effective = rc_settings.get("effective", {})
                target = effective.get("execution_target", "local")
                if target == "remote":
                    host_alias = effective.get("host_alias", "unknown")
                    root = effective.get("remote_project_root", "unknown")
                    self.status_banner.setText(t("desktop.sesssettings.exec_target_remote", lang, host=host_alias, root=root))
                    self.status_banner.setStyleSheet(
                        "font-weight: bold; padding: 8px; background-color: #005A9E; color: white; border-radius: 4px;"
                    )
                else:
                    self.status_banner.setText(t("desktop.files.exec_target_local", lang))
                    self.status_banner.setStyleSheet(
                        "font-weight: bold; padding: 8px; background-color: #444; border-radius: 4px;"
                    )

        ensure_async(_recheck(), parent=self)

    def _handle_rc_result(self, result: dict):
        lang = self.facade.ui_language
        if not result.get("ok"):
            self.rc_error_label.setStyleSheet("color: red;")
            self.rc_error_label.setText(
                t("desktop.sesssettings.rc_error", lang, error=result.get("error", ""))
            )
            self.rc_error_label.setVisible(True)
        else:
            pf = result.get("preflight")
            if pf and not pf.get("ok"):
                self.rc_error_label.setStyleSheet("color: red;")
                self.rc_error_label.setText(
                    t("desktop.sesssettings.preflight_failed", lang, error=pf.get("error"))
                )
                self.rc_error_label.setVisible(True)
            else:
                self.rc_error_label.setVisible(False)

        if self._session_uid:
            rc_settings = self.facade.get_remote_control_settings(self._session_uid)
            if rc_settings:
                self._loading = True
                self._update_rc_ui(rc_settings)
                self._loading = False

                if not result.get("ok") or (result.get("preflight") and not result.get("preflight").get("ok")):
                    self.rc_error_label.setVisible(True)

    def _rebuild_cli_buttons(self):
        # Clear existing
        for i in reversed(range(self.cli_layout.count())):
            item = self.cli_layout.itemAt(i)
            if item.widget():
                item.widget().setParent(None)

        active_cli = getattr(self._session, "active_cli", "")
        tools = getattr(self.facade.config, "tools", {})
        for name, tool_cfg in tools.items():
            if not tool_cfg.enabled:
                continue
            rb = QRadioButton(name)
            if name == active_cli:
                rb.setChecked(True)
            rb.clicked.connect(lambda checked=False, n=name: self._on_cli_changed(n))
            self.cli_group.addButton(rb)
            self.cli_layout.addWidget(rb)
        self.cli_layout.addStretch()

    def _rebuild_mode_combo(self):
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItem("None", None)

        active_mode = get_active_mode(self._session)
        modes = self.facade.list_modes()
        for m in modes:
            self.mode_combo.addItem(m, m)
            if m == active_mode:
                self.mode_combo.setCurrentText(m)
        self.mode_combo.blockSignals(False)

    def _on_name_changed(self):
        if self._loading or not self._session_uid:
            return
        new_name = self.name_edit.text().strip()
        ensure_async(self.facade.update_session_setting(self._session_uid, "name", new_name), parent=self)

    def _on_cli_changed(self, cli_name: str):
        if self._loading or not self._session_uid:
            return
        ensure_async(self.facade.update_session_setting(self._session_uid, "active_cli", cli_name), parent=self)

    def _on_mode_changed(self, index: int):
        if self._loading or not self._session_uid:
            return
        mode_id = self.mode_combo.currentData()
        ensure_async(self.facade.update_session_setting(self._session_uid, "active_mode", mode_id), parent=self)

    def _on_ssh_toggled(self, checked: bool):
        if self._loading or not self._session_uid:
            return
        self.ssh_hosts_group.setVisible(checked)
        if checked:
            self.ssh_hosts_panel.load_hosts(self._session.workdir)
        ensure_async(self.facade.update_session_setting(self._session_uid, "ssh_remote_enabled", checked), parent=self)

    def _on_orch_toggled(self, checked: bool):
        if self._loading or not self._session_uid:
            return
        ensure_async(self.facade.update_session_setting(self._session_uid, "orchestrator_enabled", checked), parent=self)

    def _on_language_save(self):
        lang = self.lang_combo.currentData()
        if not lang:
            return
        if self.facade.config and hasattr(self.facade.config, "defaults"):
            self.facade.config.defaults.default_language = lang

        async def _save():
            await self.facade.config_service.set_default_language(lang)
            self.facade.notify("ui:language_changed", lang=lang)

        ensure_async(_save(), parent=self)

    def retranslate_ui(self, lang: str) -> None:
        self.lang_label.setText(t("desktop.settings.lang_label", lang))
        self.lang_save_btn.setText(t("desktop.btn.save", lang))
        self.name_label.setText(t("desktop.settings.session_name", lang))
        self.cli_row_label.setText(t("desktop.settings.cli", lang))
        self.mode_label.setText(t("desktop.settings.active_mode", lang))
        self.ssh_check.setText(t("desktop.settings.ssh_enable", lang))
        self.ssh_row_label.setText(t("desktop.settings.ssh_remote", lang))
        self.ssh_hosts_group.setTitle(t("desktop.settings.ssh_hosts_group", lang))
        self.rc_group.setTitle(t("desktop.settings.rc_group", lang))
        self.rc_check.setText(t("desktop.settings.rc_enable", lang))
        self.rc_check_label.setText(t("desktop.settings.rc_label", lang))
        self.rc_host_label.setText(t("desktop.settings.rc_target_host", lang))
        self.rc_effective_row_label.setText(t("desktop.settings.rc_effective_target", lang))
        self.rc_recheck_btn.setText(t("desktop.sesssettings.recheck", lang))
        self.orch_check.setText(t("desktop.settings.orchestrator_enable", lang))
        self.orch_row_label.setText(t("desktop.settings.orchestrator", lang))
        if hasattr(self.ssh_hosts_panel, "retranslate_ui"):
            self.ssh_hosts_panel.retranslate_ui(lang)
        # Refresh dynamic-state strings (rc_effective_label, status_banner)
        if self._session_uid:
            rc_settings = self.facade.get_remote_control_settings(self._session_uid)
            if rc_settings:
                self._loading = True
                self._update_rc_ui(rc_settings)
                self._loading = False
        else:
            self.status_banner.setText(t("desktop.files.exec_target_local", lang))
            self.rc_effective_label.setText(t("desktop.sesssettings.effective_local", lang))
