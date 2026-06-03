from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Any, Dict, List

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QFormLayout,
    QLineEdit,
    QCheckBox,
    QSpinBox,
    QDoubleSpinBox,
    QPushButton,
    QLabel,
    QScrollArea,
    QGroupBox,
    QPlainTextEdit,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QMessageBox,
    QDialog,
    QDialogButtonBox,
    QFrame
)

from utils.ui import ensure_async

if TYPE_CHECKING:
    from app.services.config_service import ConfigDraftSaveResult, ConfigService
    from config import ToolConfig, AppConfig


DESKTOP_EDITABLE_CONFIG_FIELDS = frozenset({
    "telegram.token",
    "telegram.whitelist_chat_ids",
    "telegram.admlist_chat_ids",
    "telegram.connect_timeout_sec",
    "telegram.read_timeout_sec",
    "defaults.workdir",
    "defaults.idle_timeout_sec",
    "defaults.pending_input_confirmation_enabled",
    "defaults.cli_json_stream_archive_enabled",
    "defaults.assistant_preview_enabled",
    "defaults.memory_events_enabled",
    "defaults.memory_native_cli_hooks_enabled",
    "defaults.memory_outcomes_enabled",
    "defaults.memory_dreaming_enabled",
    "defaults.memory_events_retention_days",
    "defaults.memory_events_max_payload_chars",
    "defaults.memory_events_redaction_enabled",
    "defaults.memory_dreaming_batch_size",
    "defaults.openai_api_key",
    "defaults.openai_model",
    "defaults.zai_api_key",
    "defaults.github_token",
    "defaults.gemini_oauth_client_secret",
    "defaults.log_path",
    "tools.*.enabled",
    "tools.*.mode",
    "tools.*.cmd",
    "tools.*.headless_cmd",
    "tools.*.prompt_regex",
    "mcp.enabled",
    "mcp.host",
    "mcp.port",
    "miniapp.enabled",
    "miniapp.bind_host",
    "miniapp.bind_port",
    "miniapp.public_url",
})


DESKTOP_UNSUPPORTED_CONFIG_FIELDS = frozenset({
    "tools.*.resume_cmd",
    "tools.*.image_cmd",
    "tools.*.interactive_cmd",
    "tools.*.resume_regex",
    "tools.*.help_cmd",
    "tools.*.env",
    "tools.*.auto_commands",
    "tools.*.separate_stderr",
    "tools.*.no_session_persistence_on_fresh",
    "mcp_clients.*.name",
    "mcp_clients.*.enabled",
    "mcp_clients.*.transport",
    "mcp_clients.*.cmd",
    "mcp_clients.*.url",
    "mcp_clients.*.cwd",
    "mcp_clients.*.env",
    "mcp_clients.*.headers",
    "mcp_clients.*.timeout_ms",
    "presets.*.name",
    "presets.*.prompt",
    "security.rate_limits.default.limit",
    "security.rate_limits.default.window_sec",
    "security.rate_limits.default.burst_limit",
    "security.rate_limits.default.burst_window_sec",
    "security.rate_limits.policies.*.limit",
    "security.rate_limits.policies.*.window_sec",
    "security.rate_limits.policies.*.burst_limit",
    "security.rate_limits.policies.*.burst_window_sec",
    # i18n fields are managed via the dedicated language selector / language API
    # and auto-detection, not the generic config editor.
    "defaults.default_language",
    "telegram.user_languages",
    "defaults.clarification_keywords_by_lang",
})


class DiffDialog(QDialog):
    """Dialog to display unified diff before saving."""

    def __init__(self, diff_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Changes")
        self.setMinimumSize(800, 600)

        layout = QVBoxLayout(self)

        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setPlainText(diff_text)
        self.diff_view.setFont(self._get_mono_font())
        layout.addWidget(self.diff_view)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _get_mono_font(self):
        from PySide6.QtGui import QFont
        font = QFont("Courier New", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        return font


class ConfigEditorWidget(QWidget):
    """Widget for editing application configuration."""

    configSaved = Signal()
    loadFinished = Signal()
    saveFinished = Signal()

    def __init__(
        self,
        config_service: ConfigService,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.config_service = config_service
        self.logger = logging.getLogger(__name__)

        self._current_config: Optional[AppConfig] = None
        self._current_revision: Optional[str] = None
        self._widgets: Dict[str, Any] = {}

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("Save Configuration")
        self.save_btn.setObjectName("save_btn")
        self.save_btn.clicked.connect(self._on_save_clicked)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setObjectName("reload_btn")
        self.reload_btn.clicked.connect(self.load_config)

        btn_layout.addStretch()
        btn_layout.addWidget(self.reload_btn)
        btn_layout.addWidget(self.save_btn)
        layout.addLayout(btn_layout)

    def load_config(self):
        """Load configuration from service and update UI."""
        async def _load():
            try:
                cfg = await self.config_service.load()
                self._current_config = cfg
                self._current_revision = await self.config_service.current_revision(cfg)
                self._update_ui()
                self.loadFinished.emit()
            except Exception as e:
                self.logger.exception("Failed to load config")
                QMessageBox.critical(self, "Error", f"Failed to load config: {e}")
                self.loadFinished.emit()

        ensure_async(_load(), parent=self)

    def _update_ui(self):
        """Rebuild UI tabs based on current config."""
        if not self._current_config:
            return

        self.tabs.clear()
        self._widgets = {}

        self.tabs.addTab(self._create_telegram_tab(), "Telegram")
        self.tabs.addTab(self._create_defaults_tab(), "Defaults")
        self.tabs.addTab(self._create_tools_tab(), "Tools")
        self.tabs.addTab(self._create_mcp_tab(), "MCP")
        self.tabs.addTab(self._create_miniapp_tab(), "MiniApp")

    def _create_scroll_area(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    def _create_telegram_tab(self) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)

        cfg = self._current_config.telegram

        self._widgets["telegram.token"] = self._add_line_edit(layout, "Bot Token", cfg.token, is_secret=True)
        self._widgets["telegram.whitelist_chat_ids"] = self._add_list_edit(layout, "Whitelist IDs", cfg.whitelist_chat_ids)
        self._widgets["telegram.admlist_chat_ids"] = self._add_list_edit(layout, "Admin IDs", cfg.admlist_chat_ids)

        layout.addRow(QLabel("<br/><b>Network Settings</b>"), QLabel(""))
        self._widgets["telegram.connect_timeout_sec"] = self._add_double_spin(layout, "Connect Timeout (s)", cfg.connect_timeout_sec)
        self._widgets["telegram.read_timeout_sec"] = self._add_double_spin(layout, "Read Timeout (s)", cfg.read_timeout_sec)

        return self._create_scroll_area(container)

    def _create_defaults_tab(self) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)

        cfg = self._current_config.defaults

        self._widgets["defaults.workdir"] = self._add_line_edit(layout, "Default Workdir", cfg.workdir)
        self._widgets["defaults.idle_timeout_sec"] = self._add_spin(layout, "Idle Timeout (s)", cfg.idle_timeout_sec, 0, 3600)
        self._widgets["defaults.pending_input_confirmation_enabled"] = self._add_check(
            layout,
            "Pending Input Confirmation",
            getattr(cfg, "pending_input_confirmation_enabled", True),
        )
        self._widgets["defaults.cli_json_stream_archive_enabled"] = self._add_check(
            layout,
            "Archive CLI JSON Streams",
            getattr(cfg, "cli_json_stream_archive_enabled", False),
        )
        self._widgets["defaults.assistant_preview_enabled"] = self._add_check(
            layout,
            "Assistant Live Preview",
            getattr(cfg, "assistant_preview_enabled", False),
        )
        layout.addRow(QLabel("<br/><b>Memory Learning</b>"), QLabel(""))
        self._widgets["defaults.memory_events_enabled"] = self._add_check(
            layout,
            "Memory Events",
            getattr(cfg, "memory_events_enabled", False),
        )
        self._widgets["defaults.memory_native_cli_hooks_enabled"] = self._add_check(
            layout,
            "Native CLI Memory Hooks",
            getattr(cfg, "memory_native_cli_hooks_enabled", False),
        )
        self._widgets["defaults.memory_outcomes_enabled"] = self._add_check(
            layout,
            "Memory Outcomes",
            getattr(cfg, "memory_outcomes_enabled", False),
        )
        self._widgets["defaults.memory_dreaming_enabled"] = self._add_check(
            layout,
            "Memory Dreaming",
            getattr(cfg, "memory_dreaming_enabled", False),
        )
        self._widgets["defaults.memory_events_retention_days"] = self._add_spin(
            layout,
            "Memory Events Retention (days)",
            getattr(cfg, "memory_events_retention_days", 30),
            1,
            3650,
        )
        self._widgets["defaults.memory_events_max_payload_chars"] = self._add_spin(
            layout,
            "Memory Event Payload Limit",
            getattr(cfg, "memory_events_max_payload_chars", 6000),
            1,
            100000,
        )
        self._widgets["defaults.memory_events_redaction_enabled"] = self._add_check(
            layout,
            "Memory Event Redaction",
            getattr(cfg, "memory_events_redaction_enabled", True),
        )
        self._widgets["defaults.memory_dreaming_batch_size"] = self._add_spin(
            layout,
            "Memory Dreaming Batch Size",
            getattr(cfg, "memory_dreaming_batch_size", 20),
            1,
            1000,
        )

        layout.addRow(QLabel("<br/><b>API Keys & Providers</b>"), QLabel(""))
        self._widgets["defaults.openai_api_key"] = self._add_line_edit(layout, "OpenAI API Key", cfg.openai_api_key, is_secret=True)
        self._widgets["defaults.openai_model"] = self._add_line_edit(layout, "OpenAI Model", cfg.openai_model)
        self._widgets["defaults.zai_api_key"] = self._add_line_edit(layout, "ZAI API Key", cfg.zai_api_key, is_secret=True)
        self._widgets["defaults.github_token"] = self._add_line_edit(layout, "GitHub Token", cfg.github_token, is_secret=True)
        self._widgets["defaults.gemini_oauth_client_secret"] = self._add_line_edit(
            layout,
            "Gemini OAuth Client Secret",
            getattr(cfg, "gemini_oauth_client_secret", None),
            is_secret=True,
        )

        layout.addRow(QLabel("<br/><b>Logs</b>"), QLabel(""))
        self._widgets["defaults.log_path"] = self._add_line_edit(layout, "Log Path", cfg.log_path)

        return self._create_scroll_area(container)

    def _create_tools_tab(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)

        self.tool_list = QListWidget()
        self.tool_stack = QStackedWidget()

        for name, tool_cfg in self._current_config.tools.items():
            item = QListWidgetItem(name)
            self.tool_list.addItem(item)
            self.tool_stack.addWidget(self._create_tool_form(name, tool_cfg))

        self.tool_list.currentRowChanged.connect(self.tool_stack.setCurrentIndex)
        if self.tool_list.count() > 0:
            self.tool_list.setCurrentRow(0)

        layout.addWidget(self.tool_list, 1)
        layout.addWidget(self.tool_stack, 3)

        return container

    def _create_tool_form(self, name: str, cfg: ToolConfig) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)

        prefix = f"tools.{name}."
        self._widgets[f"{prefix}enabled"] = self._add_check(layout, "Enabled", cfg.enabled)
        self._widgets[f"{prefix}mode"] = self._add_line_edit(layout, "Mode", cfg.mode)
        self._widgets[f"{prefix}cmd"] = self._add_list_edit(layout, "Command", cfg.cmd)
        self._widgets[f"{prefix}headless_cmd"] = self._add_list_edit(layout, "Headless Command", cfg.headless_cmd or [])
        self._widgets[f"{prefix}prompt_regex"] = self._add_line_edit(layout, "Prompt Regex", cfg.prompt_regex or "")

        return self._create_scroll_area(container)

    def _create_mcp_tab(self) -> QWidget:
        container = QWidget()
        v_layout = QVBoxLayout(container)

        mcp_group = QGroupBox("MCP Server Settings")
        mcp_layout = QFormLayout(mcp_group)
        cfg = self._current_config.mcp
        self._widgets["mcp.enabled"] = self._add_check(mcp_layout, "Enabled", cfg.enabled)
        self._widgets["mcp.host"] = self._add_line_edit(mcp_layout, "Host", cfg.host)
        self._widgets["mcp.port"] = self._add_spin(mcp_layout, "Port", cfg.port, 1, 65535)
        v_layout.addWidget(mcp_group)

        clients_group = QGroupBox("MCP Clients (Active)")
        clients_layout = QVBoxLayout(clients_group)
        self.mcp_clients_list = QListWidget()
        for client in self._current_config.mcp_clients:
            status = "Enabled" if client.enabled else "Disabled"
            self.mcp_clients_list.addItem(f"{client.name} [{status}] ({client.transport})")
        clients_layout.addWidget(self.mcp_clients_list)
        v_layout.addWidget(clients_group)

        v_layout.addStretch()
        return self._create_scroll_area(container)

    def _create_miniapp_tab(self) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        cfg = self._current_config.miniapp

        self._widgets["miniapp.enabled"] = self._add_check(layout, "Enabled", cfg.enabled)
        self._widgets["miniapp.bind_host"] = self._add_line_edit(layout, "Bind Host", cfg.bind_host)
        self._widgets["miniapp.bind_port"] = self._add_spin(layout, "Bind Port", cfg.bind_port, 1, 65535)
        self._widgets["miniapp.public_url"] = self._add_line_edit(layout, "Public URL", cfg.public_url)

        return self._create_scroll_area(container)

    def _add_line_edit(self, layout, label, value, is_secret=False) -> QLineEdit:
        widget = QLineEdit()
        if value is not None:
            widget.setText(str(value))
        if is_secret:
            widget.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow(label, widget)
        return widget

    def _add_check(self, layout, label, value) -> QCheckBox:
        widget = QCheckBox()
        widget.setChecked(bool(value))
        layout.addRow(label, widget)
        return widget

    def _add_spin(self, layout, label, value, min_v=0, max_v=999999) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(min_v, max_v)
        widget.setValue(int(value or 0))
        layout.addRow(label, widget)
        return widget

    def _add_double_spin(self, layout, label, value) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setRange(0, 9999)
        widget.setValue(float(value or 0))
        layout.addRow(label, widget)
        return widget

    def _add_list_edit(self, layout, label, values) -> QPlainTextEdit:
        widget = QPlainTextEdit()
        widget.setMaximumHeight(80)
        if values:
            widget.setPlainText("\n".join(str(v) for v in values))
        layout.addRow(label, widget)
        return widget

    def _collect_config(self) -> Optional[AppConfig]:
        """Collect data from UI widgets into a new AppConfig instance."""
        if not self._current_config:
            # Если конфигурация еще не была загружена из сервиса, сбор данных невозможен.
            # Возвращаем None, чтобы предотвратить сохранение некорректного состояния.
            return None

        from copy import deepcopy
        new_cfg = deepcopy(self._current_config)
        if hasattr(new_cfg.defaults, "theme"):
            delattr(new_cfg.defaults, "theme")

        try:
            # Telegram
            new_cfg.telegram.token = self._widgets["telegram.token"].text()
            new_cfg.telegram.whitelist_chat_ids = self._parse_int_list(self._widgets["telegram.whitelist_chat_ids"].toPlainText())
            new_cfg.telegram.admlist_chat_ids = self._parse_int_list(self._widgets["telegram.admlist_chat_ids"].toPlainText())
            new_cfg.telegram.connect_timeout_sec = self._widgets["telegram.connect_timeout_sec"].value()
            new_cfg.telegram.read_timeout_sec = self._widgets["telegram.read_timeout_sec"].value()

            # Defaults
            new_cfg.defaults.workdir = self._widgets["defaults.workdir"].text()
            new_cfg.defaults.idle_timeout_sec = self._widgets["defaults.idle_timeout_sec"].value()
            new_cfg.defaults.pending_input_confirmation_enabled = self._widgets[
                "defaults.pending_input_confirmation_enabled"
            ].isChecked()
            new_cfg.defaults.cli_json_stream_archive_enabled = self._widgets[
                "defaults.cli_json_stream_archive_enabled"
            ].isChecked()
            new_cfg.defaults.assistant_preview_enabled = self._widgets[
                "defaults.assistant_preview_enabled"
            ].isChecked()
            new_cfg.defaults.memory_events_enabled = self._widgets[
                "defaults.memory_events_enabled"
            ].isChecked()
            new_cfg.defaults.memory_native_cli_hooks_enabled = self._widgets[
                "defaults.memory_native_cli_hooks_enabled"
            ].isChecked()
            new_cfg.defaults.memory_outcomes_enabled = self._widgets[
                "defaults.memory_outcomes_enabled"
            ].isChecked()
            new_cfg.defaults.memory_dreaming_enabled = self._widgets[
                "defaults.memory_dreaming_enabled"
            ].isChecked()
            new_cfg.defaults.memory_events_retention_days = self._widgets[
                "defaults.memory_events_retention_days"
            ].value()
            new_cfg.defaults.memory_events_max_payload_chars = self._widgets[
                "defaults.memory_events_max_payload_chars"
            ].value()
            new_cfg.defaults.memory_events_redaction_enabled = self._widgets[
                "defaults.memory_events_redaction_enabled"
            ].isChecked()
            new_cfg.defaults.memory_dreaming_batch_size = self._widgets[
                "defaults.memory_dreaming_batch_size"
            ].value()
            new_cfg.defaults.openai_api_key = self._widgets["defaults.openai_api_key"].text()
            new_cfg.defaults.openai_model = self._widgets["defaults.openai_model"].text()
            new_cfg.defaults.zai_api_key = self._widgets["defaults.zai_api_key"].text()
            new_cfg.defaults.github_token = self._widgets["defaults.github_token"].text()
            new_cfg.defaults.gemini_oauth_client_secret = self._widgets[
                "defaults.gemini_oauth_client_secret"
            ].text()
            new_cfg.defaults.log_path = self._widgets["defaults.log_path"].text()

            # Tools
            for name in new_cfg.tools:
                prefix = f"tools.{name}."
                new_cfg.tools[name].enabled = self._widgets[f"{prefix}enabled"].isChecked()
                new_cfg.tools[name].mode = self._widgets[f"{prefix}mode"].text()
                new_cfg.tools[name].cmd = self._parse_str_list(self._widgets[f"{prefix}cmd"].toPlainText())
                new_cfg.tools[name].headless_cmd = self._parse_str_list(self._widgets[f"{prefix}headless_cmd"].toPlainText())
                new_cfg.tools[name].prompt_regex = self._widgets[f"{prefix}prompt_regex"].text() or None

            # MCP
            new_cfg.mcp.enabled = self._widgets["mcp.enabled"].isChecked()
            new_cfg.mcp.host = self._widgets["mcp.host"].text()
            new_cfg.mcp.port = self._widgets["mcp.port"].value()

            # MiniApp
            new_cfg.miniapp.enabled = self._widgets["miniapp.enabled"].isChecked()
            new_cfg.miniapp.bind_host = self._widgets["miniapp.bind_host"].text()
            new_cfg.miniapp.bind_port = self._widgets["miniapp.bind_port"].value()
            new_cfg.miniapp.public_url = self._widgets["miniapp.public_url"].text()

            return new_cfg
        except Exception as e:
            self.logger.exception("Failed to collect config from UI")
            QMessageBox.critical(self, "Error", f"Failed to collect config: {e}")
            # Возвращаем None для индикации ошибки сбора данных вызывающему методу _on_save_clicked,
            # чтобы предотвратить попытку сохранения поврежденных данных.
            return None

    def _parse_int_list(self, text: str) -> List[int]:
        result = []
        for line in text.splitlines():
            s = line.strip()
            if s:
                try:
                    result.append(int(s))
                except ValueError:
                    continue
        return result

    def _parse_str_list(self, text: str) -> List[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _validate(self) -> bool:
        """Validate input fields."""
        errors = []

        token = self._widgets["telegram.token"].text().strip()
        if not token:
            errors.append("Telegram token is required.")

        workdir = self._widgets["defaults.workdir"].text().strip()
        if not workdir:
            errors.append("Default workdir is required.")

        if self._widgets["mcp.enabled"].isChecked():
            if not self._widgets["mcp.host"].text().strip():
                errors.append("MCP host is required when MCP is enabled.")

        # Warning for env vars
        for key, widget in self._widgets.items():
            if isinstance(widget, QLineEdit):
                val = widget.text().strip()
                if val.startswith("$"):
                    # Users might intentionally use env vars like "$VAR".
                    # No special handling is required in Desktop config editor.
                    self.logger.debug("Config editor preserves env-placeholder text field=%s", key)

        if errors:
            QMessageBox.warning(self, "Validation Error", "\n".join(errors))
            return False

        return True

    def _save_result_message(self, result: ConfigDraftSaveResult) -> tuple[str, str]:
        if not result.ok:
            lines = ["Configuration was not saved."]
            if result.errors:
                lines.append("")
                lines.append("Errors:")
                lines.extend(f"- {error}" for error in result.errors)
            return "Save Failed", "\n".join(lines)

        lines = [f"Changed: {'yes' if result.changed else 'no'}"]
        if result.backup_path:
            lines.append(f"Backup created: {result.backup_path}")
        if result.restart_required:
            lines.append("Restart required: " + ", ".join(result.restart_required))
        else:
            lines.append("Restart required: none")
        if result.reloadable:
            lines.append("Reloadable: " + ", ".join(result.reloadable))
        else:
            lines.append("Reloadable: none")
        if not result.changed:
            lines.insert(0, "Configuration is already up to date.")
            return "No Changes", "\n".join(lines)
        lines.insert(0, "Configuration saved.")
        return "Success", "\n".join(lines)

    @Slot()
    def _on_save_clicked(self):
        if not self._validate():
            return

        new_cfg = self._collect_config()
        if not new_cfg:
            return

        async def _save():
            try:
                diff = await self.config_service.diff_against_disk(new_cfg)

                if diff.strip():
                    dialog = DiffDialog(diff, self)
                    if dialog.exec() != QDialog.Accepted:
                        return

                result = await self.config_service.save_config_draft_with_revision(
                    new_cfg,
                    expected_revision=self._current_revision,
                )
                title, message = self._save_result_message(result)
                if result.ok:
                    self._current_revision = result.revision
                    QMessageBox.information(self, title, message)
                    if result.changed:
                        self.configSaved.emit()
                else:
                    QMessageBox.warning(self, title, message)
            except Exception as e:
                self.logger.exception("Failed to save config")
                QMessageBox.critical(self, "Error", f"Failed to save config: {e}")
            finally:
                self.saveFinished.emit()

        ensure_async(_save(), parent=self)
