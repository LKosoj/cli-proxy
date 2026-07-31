from __future__ import annotations

import json
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
    QFrame,
    QComboBox,
    QSplitter,
)
from PySide6.QtCore import Qt

from i18n import t
from modes.sdk.runtime.json_normalizer import loads_safe
from utils.ui import ensure_async

if TYPE_CHECKING:
    from app.services.config_service import ConfigDraftSaveResult, ConfigService
    from config import ToolConfig, AppConfig
    from desktop.services.application_facade import ApplicationFacade


DESKTOP_EDITABLE_CONFIG_FIELDS = frozenset({
    # Telegram
    "telegram.token",
    "telegram.whitelist_chat_ids",
    "telegram.admlist_chat_ids",
    "telegram.connection_pool_size",
    "telegram.connect_timeout_sec",
    "telegram.read_timeout_sec",
    "telegram.write_timeout_sec",
    "telegram.pool_timeout_sec",
    "telegram.polling_timeout_sec",
    "telegram.poll_interval_sec",
    "telegram.user_workdirs",
    "telegram.user_modes",
    # Defaults – general
    "defaults.workdir",
    "defaults.idle_timeout_sec",
    "defaults.codex_jsonl_fallback_sec",
    "defaults.summary_max_chars",
    "defaults.html_filename_prefix",
    "defaults.state_path",
    "defaults.desktop_state_path",
    "defaults.toolhelp_path",
    "defaults.log_path",
    "defaults.image_temp_dir",
    "defaults.image_max_mb",
    # Defaults – API keys
    "defaults.openai_api_key",
    "defaults.openai_model",
    "defaults.openai_big_model",
    "defaults.openai_base_url",
    "defaults.zai_api_key",
    "defaults.tavily_api_key",
    "defaults.jina_api_key",
    "defaults.github_token",
    "defaults.gemini_oauth_client_secret",
    # Defaults – memory
    "defaults.memory_max_kb",
    "defaults.memory_compact_target_kb",
    "defaults.memory_events_enabled",
    "defaults.memory_native_cli_hooks_enabled",
    "defaults.memory_outcomes_enabled",
    "defaults.memory_dreaming_enabled",
    "defaults.memory_events_retention_days",
    "defaults.memory_events_max_payload_chars",
    "defaults.memory_events_redaction_enabled",
    "defaults.memory_dreaming_batch_size",
    # Defaults – agent behaviour
    "defaults.clarification_enabled",
    "defaults.pending_input_confirmation_enabled",
    "defaults.default_cli",
    "defaults.default_execution_backend",
    "defaults.clarification_keywords",
    "defaults.cli_json_stream_archive_enabled",
    "defaults.assistant_preview_enabled",
    "defaults.codebase_mapper_usage",
    "defaults.run_artifacts_enabled",
    "defaults.run_artifacts_retention_days",
    "defaults.run_doctor_enabled",
    "defaults.run_boundary_validation_enabled",
    "defaults.run_metrics_enabled",
    "defaults.llm_trace_enabled",
    "defaults.tool_disclosure",
    "defaults.context_window_tokens",
    "defaults.context_reserve_tokens",
    "defaults.summarization_threshold",
    # Defaults – skills
    "defaults.skill_discovery_mode",
    "defaults.skill_install_policy",
    "defaults.skill_registry_paths",
    "defaults.skill_allowlisted_sources",
    # Defaults – manager / analyst / webmaster
    "defaults.manager_max_tasks",
    "defaults.manager_max_attempts",
    "defaults.manager_decompose_timeout_sec",
    "defaults.manager_dev_timeout_sec",
    "defaults.manager_review_timeout_sec",
    "defaults.manager_dev_report_max_chars",
    "defaults.manager_auto_resume",
    "defaults.manager_auto_commit",
    "defaults.manager_response_archive",
    "defaults.analyst_use_cli_timeout_sec",
    "defaults.webmaster_use_cli_timeout_sec",
    "defaults.webmaster_validation_max_fix_iterations",
    # Defaults – CLI routing
    "defaults.cli_routing",
    # Tools
    "tools.*.enabled",
    "tools.*.mode",
    "tools.*.cmd",
    "tools.*.headless_cmd",
    "tools.*.execution_backends",
    "tools.*.default_execution_backend",
    "tools.*.tmux_user",
    "tools.*.prompt_regex",
    "tools.*.resume_cmd",
    "tools.*.image_cmd",
    "tools.*.interactive_cmd",
    "tools.*.interactive_resume_cmd",
    "tools.*.resume_regex",
    "tools.*.help_cmd",
    "tools.*.env",
    "tools.*.auto_commands",
    "tools.*.separate_stderr",
    "tools.*.no_session_persistence_on_fresh",
    # MCP server
    "mcp.enabled",
    "mcp.host",
    "mcp.port",
    "mcp.token",
    # MCP clients
    "mcp_clients.*.name",
    "mcp_clients.*.enabled",
    "mcp_clients.*.transport",
    "mcp_clients.*.cmd",
    "mcp_clients.*.url",
    "mcp_clients.*.cwd",
    "mcp_clients.*.env",
    "mcp_clients.*.headers",
    "mcp_clients.*.timeout_ms",
    # Presets
    "presets.*.name",
    "presets.*.prompt",
    # MiniApp
    "miniapp.enabled",
    "miniapp.bind_host",
    "miniapp.bind_port",
    "miniapp.base_path",
    "miniapp.public_url",
    "miniapp.max_edit_file_size_kb",
    "miniapp.enable_delete",
    # Thread mode
    "thread_mode.enabled",
    "thread_mode.mode",
    "thread_mode.topics_chat_id",
    "thread_mode.topic_title_prefix",
    "thread_mode.inactivity_ttl_sec",
    # Webhooks
    "webhooks.enabled",
    "webhooks.path",
    "webhooks.public_base_url",
    "webhooks.secret_token",
    "webhooks.request_timeout_sec",
    "webhooks.max_payload_bytes",
    # Scheduler
    "scheduler.enabled",
    "scheduler.timezone",
    "scheduler.tick_interval_sec",
    "scheduler.max_concurrent_jobs",
    "scheduler.job_timeout_sec",
    "scheduler.misfire_grace_sec",
    # Security
    "security.rate_limits.enabled",
    "security.rate_limits.backend",
    "security.rate_limits.sqlite_path",
    "security.rate_limits.default",
    "security.rate_limits.policies",
    # Lint Evolution
    "lint_evolution.enabled",
    "lint_evolution.level1_cooldown_hours",
    "lint_evolution.level2_cooldown_hours",
    "lint_evolution.level3_cooldown_hours",
    "lint_evolution.lock_ttl_minutes",
    "lint_evolution.error_retry_hours",
    "lint_evolution.fp_growth_threshold_pct",
    "lint_evolution.canary_rolling_days",
    "lint_evolution.canary_baseline_days",
    "lint_evolution.canary_max_schema_fields_per_180d",
})


DESKTOP_UNSUPPORTED_CONFIG_FIELDS = frozenset({
    # Rate-limit sub-fields are edited as JSON objects via security.rate_limits.default
    # and security.rate_limits.policies — no separate scalar widget needed.
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


class _JsonEditDialog(QDialog):
    """Generic dialog to create/edit a JSON object."""

    def __init__(self, title: str, initial: Any = None, lang: str = "ru", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(500, 400)

        layout = QVBoxLayout(self)
        self._editor = QPlainTextEdit()
        self._editor.setFont(self._mono_font())
        try:
            text = json.dumps(initial or {}, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = "{}"
        self._editor.setPlainText(text)
        layout.addWidget(self._editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._lang = lang
        self._result: Any = None

    def _mono_font(self):
        from PySide6.QtGui import QFont
        font = QFont("Courier New", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        return font

    def _on_accept(self) -> None:
        text = self._editor.toPlainText().strip()
        try:
            self._result = loads_safe(text)
        except json.JSONDecodeError as exc:
            QMessageBox.warning(
                self,
                t("desktop.cfgedit.validation_error", self._lang),
                t("desktop.cfgedit.err_json_invalid", self._lang, error=str(exc)),
            )
            return
        self.accept()

    def get_result(self) -> Any:
        return self._result


class PresetsDialog(QDialog):
    """CRUD dialog for config.presets (array of {name, prompt})."""

    def __init__(self, presets: List[Any], lang: str = "ru", parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("desktop.cfgedit.presets_title", lang))
        self.setMinimumSize(700, 500)
        self._lang = lang
        self._presets: List[Dict[str, str]] = [
            {"name": p.get("name", "") if isinstance(p, dict) else getattr(p, "name", ""),
             "prompt": p.get("prompt", "") if isinstance(p, dict) else getattr(p, "prompt", "")}
            for p in (presets or [])
        ]

        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self._list = QListWidget()
        self._refresh_list()
        self._list.currentRowChanged.connect(self._on_row_changed)
        left_layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        add_btn = QPushButton(t("desktop.cfgedit.btn_add", lang))
        add_btn.clicked.connect(self._on_add)
        self._del_btn = QPushButton(t("desktop.cfgedit.btn_delete", lang))
        self._del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(self._del_btn)
        left_layout.addLayout(btn_row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QFormLayout(right)
        self._name_edit = QLineEdit()
        self._name_edit.textChanged.connect(self._on_field_changed)
        right_layout.addRow(t("desktop.cfgedit.preset_name", lang), self._name_edit)
        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.textChanged.connect(self._on_field_changed)
        right_layout.addRow(t("desktop.cfgedit.preset_prompt", lang), self._prompt_edit)
        splitter.addWidget(right)
        splitter.setSizes([200, 500])

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._current_row: int = -1
        self._updating = False
        if self._presets:
            self._list.setCurrentRow(0)

    def _refresh_list(self) -> None:
        self._list.clear()
        for p in self._presets:
            self._list.addItem(p.get("name") or "(unnamed)")

    def _on_row_changed(self, row: int) -> None:
        self._save_current()
        self._current_row = row
        self._updating = True
        if 0 <= row < len(self._presets):
            p = self._presets[row]
            self._name_edit.setText(p.get("name", ""))
            self._prompt_edit.setPlainText(p.get("prompt", ""))
            self._del_btn.setEnabled(True)
        else:
            self._name_edit.clear()
            self._prompt_edit.clear()
            self._del_btn.setEnabled(False)
        self._updating = False

    def _save_current(self) -> None:
        row = self._current_row
        if 0 <= row < len(self._presets) and not self._updating:
            self._presets[row]["name"] = self._name_edit.text()
            self._presets[row]["prompt"] = self._prompt_edit.toPlainText()

    def _on_field_changed(self) -> None:
        if self._updating:
            return
        self._save_current()
        if 0 <= self._current_row < self._list.count():
            name = self._name_edit.text() or "(unnamed)"
            self._list.item(self._current_row).setText(name)

    def _on_add(self) -> None:
        self._save_current()
        self._presets.append({"name": "", "prompt": ""})
        self._refresh_list()
        self._list.setCurrentRow(len(self._presets) - 1)

    def _on_delete(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._presets):
            self._current_row = -1
            del self._presets[row]
            self._refresh_list()
            new_row = min(row, len(self._presets) - 1)
            if new_row >= 0:
                self._list.setCurrentRow(new_row)
            else:
                self._name_edit.clear()
                self._prompt_edit.clear()

    def get_presets(self) -> List[Dict[str, str]]:
        self._save_current()
        return self._presets


class MCPClientsDialog(QDialog):
    """Dialog for viewing/editing MCP clients list as JSON."""

    def __init__(self, clients: List[Any], lang: str = "ru", parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("desktop.cfgedit.mcp_clients_dialog_title", lang))
        self.setMinimumSize(700, 500)
        self._lang = lang

        def _to_dict(c: Any) -> Dict[str, Any]:
            if isinstance(c, dict):
                return c
            result: Dict[str, Any] = {}
            for field in ("name", "enabled", "transport", "cmd", "url", "cwd",
                          "env", "headers", "timeout_ms"):
                val = getattr(c, field, None)
                if val is not None:
                    result[field] = val
            return result

        self._clients = [_to_dict(c) for c in (clients or [])]

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(t("desktop.cfgedit.mcp_clients_json_hint", lang)))

        self._editor = QPlainTextEdit()
        self._editor.setFont(self._mono_font())
        self._editor.setPlainText(
            json.dumps(self._clients, ensure_ascii=False, indent=2)
        )
        layout.addWidget(self._editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._result: Optional[List[Dict[str, Any]]] = None

    def _mono_font(self):
        from PySide6.QtGui import QFont
        font = QFont("Courier New", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        return font

    def _on_accept(self) -> None:
        text = self._editor.toPlainText().strip()
        try:
            parsed = loads_safe(text)
        except json.JSONDecodeError as exc:
            QMessageBox.warning(
                self,
                t("desktop.cfgedit.validation_error", self._lang),
                t("desktop.cfgedit.err_json_invalid", self._lang, error=str(exc)),
            )
            return
        if not isinstance(parsed, list):
            QMessageBox.warning(
                self,
                t("desktop.cfgedit.validation_error", self._lang),
                t("desktop.cfgedit.err_mcp_clients_must_be_list", self._lang),
            )
            return
        self._result = parsed
        self.accept()

    def get_clients(self) -> Optional[List[Dict[str, Any]]]:
        return self._result


class DiffDialog(QDialog):
    """Dialog to display unified diff before saving."""

    def __init__(self, diff_text: str, lang: str = "ru", parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("desktop.cfgedit.review_changes", lang))
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
        facade: Optional["ApplicationFacade"] = None,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.config_service = config_service
        self._facade = facade
        self.logger = logging.getLogger(__name__)

        self._current_config: Optional[AppConfig] = None
        self._current_revision: Optional[str] = None
        self._widgets: Dict[str, Any] = {}
        self._lang: str = "ru"

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton(t("desktop.cfgedit.save_config", self._lang))
        self.save_btn.setObjectName("save_btn")
        self.save_btn.clicked.connect(self._on_save_clicked)

        self.reload_btn = QPushButton(t("desktop.btn.reload", self._lang))
        self.reload_btn.setObjectName("reload_btn")
        self.reload_btn.clicked.connect(self.load_config)

        self.reload_runtime_btn = QPushButton(t("desktop.cfgedit.reload_runtime_btn", self._lang))
        self.reload_runtime_btn.setObjectName("reload_runtime_btn")
        self.reload_runtime_btn.setToolTip(t("desktop.cfgedit.reload_runtime_btn", self._lang))
        self.reload_runtime_btn.clicked.connect(self._on_reload_runtime_clicked)

        btn_layout.addStretch()
        btn_layout.addWidget(self.reload_btn)
        btn_layout.addWidget(self.reload_runtime_btn)
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
                QMessageBox.critical(
                    self, t("common.error", self._lang),
                    t("desktop.cfgedit.load_error", self._lang, error=str(e)))
                self.loadFinished.emit()

        ensure_async(_load(), parent=self)

    def _update_ui(self):
        """Rebuild UI tabs based on current config."""
        if not self._current_config:
            return

        self.tabs.clear()
        self._widgets = {}

        lang = self._lang
        self.tabs.addTab(self._create_telegram_tab(lang), "Telegram")
        self.tabs.addTab(self._create_defaults_tab(lang), t("desktop.cfgedit.tab.defaults", lang))
        self.tabs.addTab(self._create_tools_tab(lang), t("desktop.cfgedit.tab.tools", lang))
        self.tabs.addTab(self._create_presets_tab(lang), t("desktop.cfgedit.tab.presets", lang))
        self.tabs.addTab(self._create_mcp_tab(lang), "MCP")
        self.tabs.addTab(self._create_miniapp_tab(lang), "MiniApp")
        self.tabs.addTab(self._create_thread_mode_tab(lang), t("desktop.cfgedit.tab.thread_mode", lang))
        self.tabs.addTab(self._create_webhooks_tab(lang), t("desktop.cfgedit.tab.webhooks", lang))
        self.tabs.addTab(self._create_scheduler_tab(lang), t("desktop.cfgedit.tab.scheduler", lang))
        self.tabs.addTab(self._create_security_tab(lang), t("desktop.cfgedit.tab.security", lang))
        self.tabs.addTab(self._create_lint_evolution_tab(lang), t("desktop.cfgedit.tab.lint_evolution", lang))

    def _create_scroll_area(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    def _create_telegram_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)

        cfg = self._current_config.telegram

        self._widgets["telegram.token"] = self._add_line_edit(
            layout, t("desktop.cfgedit.bot_token", lang), cfg.token, is_secret=True)
        self._widgets["telegram.whitelist_chat_ids"] = self._add_list_edit(
            layout, t("desktop.cfgedit.whitelist_ids", lang), cfg.whitelist_chat_ids)
        self._widgets["telegram.admlist_chat_ids"] = self._add_list_edit(
            layout, t("desktop.cfgedit.admin_ids", lang), cfg.admlist_chat_ids)

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.network_settings', lang)}</b>"), QLabel(""))
        self._widgets["telegram.connection_pool_size"] = self._add_spin(
            layout, t("desktop.cfgedit.connection_pool_size", lang),
            getattr(cfg, "connection_pool_size", 8), 1, 256)
        self._widgets["telegram.connect_timeout_sec"] = self._add_double_spin(
            layout, t("desktop.cfgedit.connect_timeout", lang), cfg.connect_timeout_sec)
        self._widgets["telegram.read_timeout_sec"] = self._add_double_spin(
            layout, t("desktop.cfgedit.read_timeout", lang), cfg.read_timeout_sec)
        self._widgets["telegram.write_timeout_sec"] = self._add_double_spin(
            layout, t("desktop.cfgedit.write_timeout", lang),
            getattr(cfg, "write_timeout_sec", 20.0))
        self._widgets["telegram.pool_timeout_sec"] = self._add_double_spin(
            layout, t("desktop.cfgedit.pool_timeout", lang),
            getattr(cfg, "pool_timeout_sec", 10.0))
        self._widgets["telegram.polling_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.polling_timeout", lang),
            getattr(cfg, "polling_timeout_sec", 5), 0, 120)
        self._widgets["telegram.poll_interval_sec"] = self._add_double_spin(
            layout, t("desktop.cfgedit.poll_interval", lang),
            getattr(cfg, "poll_interval_sec", 0.0))

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.user_access', lang)}</b>"), QLabel(""))
        self._widgets["telegram.user_workdirs"] = self._add_json_edit(
            layout, t("desktop.cfgedit.user_workdirs", lang),
            getattr(cfg, "user_workdirs", {}))
        self._widgets["telegram.user_modes"] = self._add_json_edit(
            layout, t("desktop.cfgedit.user_modes", lang),
            getattr(cfg, "user_modes", {}))

        return self._create_scroll_area(container)

    def _create_defaults_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)

        cfg = self._current_config.defaults

        self._widgets["defaults.workdir"] = self._add_line_edit(
            layout, t("desktop.cfgedit.default_workdir", lang), cfg.workdir)
        self._widgets["defaults.idle_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.idle_timeout", lang), cfg.idle_timeout_sec, 0, 3600)
        self._widgets["defaults.codex_jsonl_fallback_sec"] = self._add_spin(
            layout,
            t("desktop.cfgedit.codex_jsonl_fallback", lang),
            getattr(cfg, "codex_jsonl_fallback_sec", 180),
            0,
            3600,
        )
        self._widgets["defaults.pending_input_confirmation_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.pending_input_confirmation", lang),
            getattr(cfg, "pending_input_confirmation_enabled", True),
        )
        self._widgets["defaults.cli_json_stream_archive_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.archive_cli_json", lang),
            getattr(cfg, "cli_json_stream_archive_enabled", False),
        )
        self._widgets["defaults.assistant_preview_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.assistant_preview", lang),
            getattr(cfg, "assistant_preview_enabled", False),
        )
        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.memory_learning', lang)}</b>"), QLabel(""))
        self._widgets["defaults.memory_max_kb"] = self._add_spin(
            layout,
            t("desktop.cfgedit.memory_max_kb", lang),
            getattr(cfg, "memory_max_kb", 32),
            1,
            1048576,
        )
        self._widgets["defaults.memory_compact_target_kb"] = self._add_spin(
            layout,
            t("desktop.cfgedit.memory_compact_target_kb", lang),
            getattr(cfg, "memory_compact_target_kb", 24),
            1,
            1048576,
        )
        self._widgets["defaults.memory_events_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.memory_events", lang),
            getattr(cfg, "memory_events_enabled", False),
        )
        self._widgets["defaults.memory_native_cli_hooks_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.memory_native_hooks", lang),
            getattr(cfg, "memory_native_cli_hooks_enabled", False),
        )
        self._widgets["defaults.memory_outcomes_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.memory_outcomes", lang),
            getattr(cfg, "memory_outcomes_enabled", False),
        )
        self._widgets["defaults.memory_dreaming_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.memory_dreaming", lang),
            getattr(cfg, "memory_dreaming_enabled", False),
        )
        self._widgets["defaults.memory_events_retention_days"] = self._add_spin(
            layout,
            t("desktop.cfgedit.memory_retention_days", lang),
            getattr(cfg, "memory_events_retention_days", 30),
            1,
            3650,
        )
        self._widgets["defaults.memory_events_max_payload_chars"] = self._add_spin(
            layout,
            t("desktop.cfgedit.memory_payload_limit", lang),
            getattr(cfg, "memory_events_max_payload_chars", 6000),
            1,
            100000,
        )
        self._widgets["defaults.memory_events_redaction_enabled"] = self._add_check(
            layout,
            t("desktop.cfgedit.memory_redaction", lang),
            getattr(cfg, "memory_events_redaction_enabled", True),
        )
        self._widgets["defaults.memory_dreaming_batch_size"] = self._add_spin(
            layout,
            t("desktop.cfgedit.memory_dreaming_batch", lang),
            getattr(cfg, "memory_dreaming_batch_size", 20),
            1,
            1000,
        )

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.api_keys', lang)}</b>"), QLabel(""))
        self._widgets["defaults.openai_api_key"] = self._add_line_edit(
            layout, t("desktop.cfgedit.openai_api_key", lang), cfg.openai_api_key, is_secret=True)
        self._widgets["defaults.openai_model"] = self._add_line_edit(
            layout, t("desktop.cfgedit.openai_model", lang), cfg.openai_model)
        self._widgets["defaults.zai_api_key"] = self._add_line_edit(
            layout, t("desktop.cfgedit.zai_api_key", lang), cfg.zai_api_key, is_secret=True)
        self._widgets["defaults.github_token"] = self._add_line_edit(
            layout, t("desktop.cfgedit.github_token", lang), cfg.github_token, is_secret=True)
        self._widgets["defaults.gemini_oauth_client_secret"] = self._add_line_edit(
            layout,
            t("desktop.cfgedit.gemini_secret", lang),
            getattr(cfg, "gemini_oauth_client_secret", None),
            is_secret=True,
        )

        layout.addRow(QLabel(f"<br/><b>{t('desktop.nav.logs', lang)}</b>"), QLabel(""))
        self._widgets["defaults.log_path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.log_path", lang), cfg.log_path)

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.paths_section', lang)}</b>"), QLabel(""))
        self._widgets["defaults.state_path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.state_path", lang), getattr(cfg, "state_path", "state.json"))
        self._widgets["defaults.desktop_state_path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.desktop_state_path", lang),
            getattr(cfg, "desktop_state_path", "desktop_state.json"))
        self._widgets["defaults.toolhelp_path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.toolhelp_path", lang), getattr(cfg, "toolhelp_path", "toolhelp.json"))
        self._widgets["defaults.image_temp_dir"] = self._add_line_edit(
            layout, t("desktop.cfgedit.image_temp_dir", lang),
            getattr(cfg, "image_temp_dir", ".cli-proxy/.attachments"))
        self._widgets["defaults.html_filename_prefix"] = self._add_line_edit(
            layout, t("desktop.cfgedit.html_filename_prefix", lang),
            getattr(cfg, "html_filename_prefix", "cli-output"))

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.images_section', lang)}</b>"), QLabel(""))
        self._widgets["defaults.image_max_mb"] = self._add_spin(
            layout, t("desktop.cfgedit.image_max_mb", lang), getattr(cfg, "image_max_mb", 10), 1, 1000)
        self._widgets["defaults.summary_max_chars"] = self._add_spin(
            layout, t("desktop.cfgedit.summary_max_chars", lang),
            getattr(cfg, "summary_max_chars", 4000), 100, 100000)

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.api_keys_extra', lang)}</b>"), QLabel(""))
        self._widgets["defaults.openai_big_model"] = self._add_line_edit(
            layout, t("desktop.cfgedit.openai_big_model", lang), getattr(cfg, "openai_big_model", None))
        self._widgets["defaults.openai_base_url"] = self._add_line_edit(
            layout, t("desktop.cfgedit.openai_base_url", lang), getattr(cfg, "openai_base_url", None))
        self._widgets["defaults.tavily_api_key"] = self._add_line_edit(
            layout, t("desktop.cfgedit.tavily_api_key", lang), getattr(cfg, "tavily_api_key", None),
            is_secret=True)
        self._widgets["defaults.jina_api_key"] = self._add_line_edit(
            layout, t("desktop.cfgedit.jina_api_key", lang), getattr(cfg, "jina_api_key", None),
            is_secret=True)

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.agent_behaviour', lang)}</b>"), QLabel(""))
        self._widgets["defaults.clarification_enabled"] = self._add_check(
            layout, t("desktop.cfgedit.clarification_enabled", lang),
            getattr(cfg, "clarification_enabled", True))
        self._widgets["defaults.clarification_keywords"] = self._add_list_edit(
            layout, t("desktop.cfgedit.clarification_keywords", lang),
            getattr(cfg, "clarification_keywords", []))
        self._widgets["defaults.default_cli"] = self._add_line_edit(
            layout, t("desktop.cfgedit.default_cli", lang), getattr(cfg, "default_cli", None))
        self._widgets["defaults.default_execution_backend"] = self._add_combo(
            layout, t("desktop.cfgedit.default_execution_backend", lang),
            ["headless", "tmux"],
            getattr(cfg, "default_execution_backend", "headless"))
        self._widgets["defaults.codebase_mapper_usage"] = self._add_combo(
            layout, t("desktop.cfgedit.codebase_mapper_usage", lang),
            ["auto", "enabled", "disabled"],
            getattr(cfg, "codebase_mapper_usage", "auto"))
        self._widgets["defaults.run_artifacts_enabled"] = self._add_check(
            layout, t("desktop.cfgedit.run_artifacts_enabled", lang),
            getattr(cfg, "run_artifacts_enabled", True))
        self._widgets["defaults.run_artifacts_retention_days"] = self._add_spin(
            layout, t("desktop.cfgedit.run_artifacts_retention_days", lang),
            getattr(cfg, "run_artifacts_retention_days", 30), 1, 3650)
        self._widgets["defaults.run_doctor_enabled"] = self._add_check(
            layout, t("desktop.cfgedit.run_doctor_enabled", lang),
            getattr(cfg, "run_doctor_enabled", True))
        self._widgets["defaults.run_boundary_validation_enabled"] = self._add_check(
            layout, t("desktop.cfgedit.run_boundary_validation_enabled", lang),
            getattr(cfg, "run_boundary_validation_enabled", True))
        self._widgets["defaults.run_metrics_enabled"] = self._add_check(
            layout, t("desktop.cfgedit.run_metrics_enabled", lang),
            getattr(cfg, "run_metrics_enabled", True))
        self._widgets["defaults.llm_trace_enabled"] = self._add_check(
            layout, t("desktop.cfgedit.llm_trace_enabled", lang),
            getattr(cfg, "llm_trace_enabled", False))

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.context_section', lang)}</b>"), QLabel(""))
        self._widgets["defaults.tool_disclosure"] = self._add_combo(
            layout, t("desktop.cfgedit.tool_disclosure", lang),
            ["full", "progressive"],
            getattr(cfg, "tool_disclosure", "full"))
        self._widgets["defaults.context_window_tokens"] = self._add_spin(
            layout, t("desktop.cfgedit.context_window_tokens", lang),
            getattr(cfg, "context_window_tokens", 128000), 1000, 1000000)
        self._widgets["defaults.context_reserve_tokens"] = self._add_spin(
            layout, t("desktop.cfgedit.context_reserve_tokens", lang),
            getattr(cfg, "context_reserve_tokens", 8000), 0, 100000)
        self._widgets["defaults.summarization_threshold"] = self._add_double_spin(
            layout, t("desktop.cfgedit.summarization_threshold", lang),
            getattr(cfg, "summarization_threshold", 0.75))

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.skills_section', lang)}</b>"), QLabel(""))
        self._widgets["defaults.skill_discovery_mode"] = self._add_combo(
            layout, t("desktop.cfgedit.skill_discovery_mode", lang),
            ["off", "suggest", "auto"],
            getattr(cfg, "skill_discovery_mode", "suggest"))
        self._widgets["defaults.skill_install_policy"] = self._add_combo(
            layout, t("desktop.cfgedit.skill_install_policy", lang),
            ["manual", "admin_approve", "allowlisted_auto"],
            getattr(cfg, "skill_install_policy", "manual"))
        self._widgets["defaults.skill_registry_paths"] = self._add_list_edit(
            layout, t("desktop.cfgedit.skill_registry_paths", lang),
            getattr(cfg, "skill_registry_paths", []))
        self._widgets["defaults.skill_allowlisted_sources"] = self._add_list_edit(
            layout, t("desktop.cfgedit.skill_allowlisted_sources", lang),
            getattr(cfg, "skill_allowlisted_sources", []))

        layout.addRow(QLabel(f"<br/><b>{t('desktop.cfgedit.manager_section', lang)}</b>"), QLabel(""))
        self._widgets["defaults.manager_max_tasks"] = self._add_spin(
            layout, t("desktop.cfgedit.manager_max_tasks", lang),
            getattr(cfg, "manager_max_tasks", 10), 1, 200)
        self._widgets["defaults.manager_max_attempts"] = self._add_spin(
            layout, t("desktop.cfgedit.manager_max_attempts", lang),
            getattr(cfg, "manager_max_attempts", 3), 1, 50)
        self._widgets["defaults.manager_decompose_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.manager_decompose_timeout", lang),
            getattr(cfg, "manager_decompose_timeout_sec", 1200), 0, 86400)
        self._widgets["defaults.manager_dev_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.manager_dev_timeout", lang),
            getattr(cfg, "manager_dev_timeout_sec", 3600), 0, 86400)
        self._widgets["defaults.manager_review_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.manager_review_timeout", lang),
            getattr(cfg, "manager_review_timeout_sec", 1200), 0, 86400)
        self._widgets["defaults.manager_dev_report_max_chars"] = self._add_spin(
            layout, t("desktop.cfgedit.manager_dev_report_max_chars", lang),
            getattr(cfg, "manager_dev_report_max_chars", 20000), 100, 500000)
        self._widgets["defaults.manager_auto_resume"] = self._add_check(
            layout, t("desktop.cfgedit.manager_auto_resume", lang),
            getattr(cfg, "manager_auto_resume", True))
        self._widgets["defaults.manager_auto_commit"] = self._add_check(
            layout, t("desktop.cfgedit.manager_auto_commit", lang),
            getattr(cfg, "manager_auto_commit", True))
        self._widgets["defaults.manager_response_archive"] = self._add_check(
            layout, t("desktop.cfgedit.manager_response_archive", lang),
            getattr(cfg, "manager_response_archive", True))
        self._widgets["defaults.analyst_use_cli_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.analyst_use_cli_timeout", lang),
            getattr(cfg, "analyst_use_cli_timeout_sec", 3600), 0, 86400)
        self._widgets["defaults.webmaster_use_cli_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.webmaster_use_cli_timeout", lang),
            getattr(cfg, "webmaster_use_cli_timeout_sec", 3600), 0, 86400)
        self._widgets["defaults.webmaster_validation_max_fix_iterations"] = self._add_spin(
            layout, t("desktop.cfgedit.webmaster_max_fix_iterations", lang),
            getattr(cfg, "webmaster_validation_max_fix_iterations", 2), 0, 50)
        self._widgets["defaults.cli_routing"] = self._add_json_edit(
            layout, t("desktop.cfgedit.cli_routing", lang),
            getattr(cfg, "cli_routing", None) or {})

        return self._create_scroll_area(container)

    def _create_tools_tab(self, lang: str) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)

        inner = QWidget()
        layout = QHBoxLayout(inner)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.tool_list = QListWidget()
        self.tool_stack = QStackedWidget()

        for name, tool_cfg in self._current_config.tools.items():
            item = QListWidgetItem(name)
            self.tool_list.addItem(item)
            self.tool_stack.addWidget(self._create_tool_form(name, tool_cfg, lang))

        self.tool_list.currentRowChanged.connect(self.tool_stack.setCurrentIndex)
        if self.tool_list.count() > 0:
            self.tool_list.setCurrentRow(0)

        left_layout.addWidget(self.tool_list)

        tool_btns = QHBoxLayout()
        add_tool_btn = QPushButton(t("desktop.cfgedit.btn_add", lang))
        add_tool_btn.clicked.connect(lambda: self._on_add_tool(lang))
        del_tool_btn = QPushButton(t("desktop.cfgedit.btn_delete", lang))
        del_tool_btn.clicked.connect(self._on_delete_tool)
        tool_btns.addWidget(add_tool_btn)
        tool_btns.addWidget(del_tool_btn)
        left_layout.addLayout(tool_btns)

        layout.addWidget(left, 1)
        layout.addWidget(self.tool_stack, 3)
        outer.addWidget(inner)

        return container

    def _on_add_tool(self, lang: str) -> None:
        from config import ToolConfig as _ToolConfig
        name, ok = self._prompt_tool_name(lang)
        if not ok or not name:
            return
        if name in self._current_config.tools:
            QMessageBox.warning(
                self,
                t("desktop.cfgedit.validation_error", lang),
                t("desktop.cfgedit.err_tool_name_exists", lang, name=name),
            )
            return
        new_tool = _ToolConfig(name=name, mode="headless", cmd=[])
        self._current_config.tools[name] = new_tool
        item = QListWidgetItem(name)
        self.tool_list.addItem(item)
        self.tool_stack.addWidget(self._create_tool_form(name, new_tool, lang))
        self.tool_list.setCurrentRow(self.tool_list.count() - 1)

    def _on_delete_tool(self) -> None:
        row = self.tool_list.currentRow()
        if row < 0:
            return
        name = self.tool_list.item(row).text()
        confirm = QMessageBox.question(
            self,
            t("desktop.cfgedit.btn_delete", self._lang),
            t("desktop.cfgedit.confirm_delete_tool", self._lang, name=name),
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._current_config.tools.pop(name, None)
        # Remove widget keys for this tool
        prefix = f"tools.{name}."
        for key in [k for k in self._widgets if k.startswith(prefix)]:
            del self._widgets[key]
        widget = self.tool_stack.widget(row)
        self.tool_stack.removeWidget(widget)
        widget.deleteLater()
        self.tool_list.takeItem(row)

    def _prompt_tool_name(self, lang: str):
        dialog = QDialog(self)
        dialog.setWindowTitle(t("desktop.cfgedit.tool_name_dialog_title", lang))
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(t("desktop.cfgedit.tool_name_label", lang)))
        edit = QLineEdit()
        layout.addWidget(edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        ok = dialog.exec() == QDialog.DialogCode.Accepted
        return edit.text().strip(), ok

    def _create_tool_form(self, name: str, cfg: "ToolConfig", lang: str = "ru") -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)

        prefix = f"tools.{name}."
        self._widgets[f"{prefix}enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang), cfg.enabled)
        self._widgets[f"{prefix}mode"] = self._add_line_edit(
            layout, t("desktop.cfgedit.tool_mode", lang), cfg.mode)
        self._widgets[f"{prefix}cmd"] = self._add_list_edit(
            layout, t("desktop.cfgedit.command", lang), cfg.cmd)
        self._widgets[f"{prefix}headless_cmd"] = self._add_list_edit(
            layout, t("desktop.cfgedit.headless_command", lang), cfg.headless_cmd or [])
        self._widgets[f"{prefix}execution_backends"] = self._add_list_edit(
            layout, t("desktop.cfgedit.execution_backends", lang),
            getattr(cfg, "execution_backends", None) or [])
        self._widgets[f"{prefix}default_execution_backend"] = self._add_combo(
            layout, t("desktop.cfgedit.default_execution_backend", lang),
            ["", "headless", "tmux"],
            getattr(cfg, "default_execution_backend", None) or "")
        self._widgets[f"{prefix}tmux_user"] = self._add_line_edit(
            layout, t("desktop.cfgedit.tmux_user", lang),
            getattr(cfg, "tmux_user", None) or "")
        self._widgets[f"{prefix}interactive_cmd"] = self._add_list_edit(
            layout, t("desktop.cfgedit.interactive_command", lang),
            getattr(cfg, "interactive_cmd", None) or [])
        self._widgets[f"{prefix}interactive_resume_cmd"] = self._add_list_edit(
            layout, t("desktop.cfgedit.interactive_resume_command", lang),
            getattr(cfg, "interactive_resume_cmd", None) or [])
        self._widgets[f"{prefix}resume_cmd"] = self._add_list_edit(
            layout, t("desktop.cfgedit.resume_command", lang),
            getattr(cfg, "resume_cmd", None) or [])
        self._widgets[f"{prefix}image_cmd"] = self._add_list_edit(
            layout, t("desktop.cfgedit.image_command", lang),
            getattr(cfg, "image_cmd", None) or [])
        self._widgets[f"{prefix}auto_commands"] = self._add_list_edit(
            layout, t("desktop.cfgedit.auto_commands", lang),
            getattr(cfg, "auto_commands", None) or [])
        self._widgets[f"{prefix}prompt_regex"] = self._add_line_edit(
            layout, t("desktop.cfgedit.prompt_regex", lang), cfg.prompt_regex or "")
        self._widgets[f"{prefix}resume_regex"] = self._add_line_edit(
            layout, t("desktop.cfgedit.resume_regex", lang),
            getattr(cfg, "resume_regex", None) or "")
        self._widgets[f"{prefix}help_cmd"] = self._add_line_edit(
            layout, t("desktop.cfgedit.help_cmd", lang),
            getattr(cfg, "help_cmd", None) or "")
        self._widgets[f"{prefix}env"] = self._add_json_edit(
            layout, t("desktop.cfgedit.tool_env", lang),
            getattr(cfg, "env", None) or {})
        self._widgets[f"{prefix}separate_stderr"] = self._add_check(
            layout, t("desktop.cfgedit.separate_stderr", lang),
            getattr(cfg, "separate_stderr", False))
        self._widgets[f"{prefix}no_session_persistence_on_fresh"] = self._add_check(
            layout, t("desktop.cfgedit.no_session_persistence", lang),
            getattr(cfg, "no_session_persistence_on_fresh", False))

        return self._create_scroll_area(container)

    def _create_mcp_tab(self, lang: str) -> QWidget:
        container = QWidget()
        v_layout = QVBoxLayout(container)

        mcp_group = QGroupBox(t("desktop.cfgedit.mcp_server_settings", lang))
        mcp_layout = QFormLayout(mcp_group)
        cfg = self._current_config.mcp
        self._widgets["mcp.enabled"] = self._add_check(
            mcp_layout, t("desktop.admin.label.monitor_enabled", lang), cfg.enabled)
        self._widgets["mcp.host"] = self._add_line_edit(
            mcp_layout, t("desktop.cfgedit.host", lang), cfg.host)
        self._widgets["mcp.port"] = self._add_spin(
            mcp_layout, t("desktop.cfgedit.port", lang), cfg.port, 1, 65535)
        self._widgets["mcp.token"] = self._add_line_edit(
            mcp_layout, t("desktop.cfgedit.mcp_token", lang),
            getattr(cfg, "token", None), is_secret=True)
        v_layout.addWidget(mcp_group)

        clients_group = QGroupBox(t("desktop.cfgedit.mcp_clients_active", lang))
        clients_layout = QVBoxLayout(clients_group)
        self.mcp_clients_list = QListWidget()
        self._refresh_mcp_clients_list(lang)
        clients_layout.addWidget(self.mcp_clients_list)

        edit_btn = QPushButton(t("desktop.cfgedit.btn_edit_json", lang))
        edit_btn.clicked.connect(lambda: self._on_edit_mcp_clients(lang))
        clients_layout.addWidget(edit_btn)

        v_layout.addWidget(clients_group)

        v_layout.addStretch()
        return self._create_scroll_area(container)

    def _refresh_mcp_clients_list(self, lang: str) -> None:
        self.mcp_clients_list.clear()
        enabled_str = t("desktop.admin.label.monitor_enabled", lang)
        disabled_str = t("desktop.cfgedit.client_disabled", lang)
        for client in self._current_config.mcp_clients:
            if isinstance(client, dict):
                name = client.get("name", "?")
                enabled = client.get("enabled", True)
                transport = client.get("transport", "stdio")
            else:
                name = getattr(client, "name", "?")
                enabled = getattr(client, "enabled", True)
                transport = getattr(client, "transport", "stdio")
            status = enabled_str if enabled else disabled_str
            self.mcp_clients_list.addItem(f"{name} [{status}] ({transport})")

    def _on_edit_mcp_clients(self, lang: str) -> None:
        dialog = MCPClientsDialog(self._current_config.mcp_clients, lang, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            raw_list = dialog.get_clients()
            if raw_list is not None:
                from config import MCPClientServerConfig as _MCPClientConfig
                rebuilt = []
                for item in raw_list:
                    if isinstance(item, dict):
                        try:
                            rebuilt.append(_MCPClientConfig(**{
                                k: v for k, v in item.items()
                                if k in _MCPClientConfig.__dataclass_fields__
                            }))
                        except Exception:
                            rebuilt.append(item)
                    else:
                        rebuilt.append(item)
                self._current_config.mcp_clients = rebuilt
                self._refresh_mcp_clients_list(lang)

    def _create_miniapp_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        cfg = self._current_config.miniapp

        self._widgets["miniapp.enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang), cfg.enabled)
        self._widgets["miniapp.bind_host"] = self._add_line_edit(
            layout, t("desktop.cfgedit.bind_host", lang), cfg.bind_host)
        self._widgets["miniapp.bind_port"] = self._add_spin(
            layout, t("desktop.cfgedit.bind_port", lang), cfg.bind_port, 1, 65535)
        self._widgets["miniapp.base_path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.base_path", lang), getattr(cfg, "base_path", "/cli-proxy"))
        self._widgets["miniapp.public_url"] = self._add_line_edit(
            layout, t("desktop.cfgedit.public_url", lang), cfg.public_url)
        self._widgets["miniapp.max_edit_file_size_kb"] = self._add_spin(
            layout, t("desktop.cfgedit.max_edit_file_size_kb", lang),
            getattr(cfg, "max_edit_file_size_kb", 5120), 1, 1048576)
        self._widgets["miniapp.enable_delete"] = self._add_check(
            layout, t("desktop.cfgedit.enable_delete", lang),
            getattr(cfg, "enable_delete", True))

        return self._create_scroll_area(container)

    def _create_presets_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)

        self._presets_list_widget = QListWidget()
        self._refresh_presets_list()
        layout.addWidget(self._presets_list_widget)

        edit_btn = QPushButton(t("desktop.cfgedit.btn_edit_presets", lang))
        edit_btn.clicked.connect(lambda: self._on_edit_presets(lang))
        layout.addWidget(edit_btn)

        return container

    def _refresh_presets_list(self) -> None:
        self._presets_list_widget.clear()
        for p in self._current_config.presets:
            name = p.get("name", "") if isinstance(p, dict) else getattr(p, "name", "")
            self._presets_list_widget.addItem(name or "(unnamed)")

    def _on_edit_presets(self, lang: str) -> None:
        dialog = PresetsDialog(self._current_config.presets, lang, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            from config import PresetConfig as _PresetConfig
            raw = dialog.get_presets()
            self._current_config.presets = [
                _PresetConfig(name=p["name"], prompt=p["prompt"])
                for p in raw
            ]
            self._refresh_presets_list()

    def _create_thread_mode_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        cfg = self._current_config.thread_mode

        self._widgets["thread_mode.enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang),
            getattr(cfg, "enabled", True))
        self._widgets["thread_mode.mode"] = self._add_combo(
            layout, t("desktop.cfgedit.thread_mode_mode", lang),
            ["private", "group"],
            getattr(cfg, "mode", "private"))
        self._widgets["thread_mode.topics_chat_id"] = self._add_spin(
            layout, t("desktop.cfgedit.topics_chat_id", lang),
            getattr(cfg, "topics_chat_id", 0) or 0, 0, 2147483647)
        self._widgets["thread_mode.topic_title_prefix"] = self._add_line_edit(
            layout, t("desktop.cfgedit.topic_title_prefix", lang),
            getattr(cfg, "topic_title_prefix", ""))
        self._widgets["thread_mode.inactivity_ttl_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.inactivity_ttl_sec", lang),
            getattr(cfg, "inactivity_ttl_sec", 86400), 0, 2592000)

        return self._create_scroll_area(container)

    def _create_webhooks_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        cfg = self._current_config.webhooks

        self._widgets["webhooks.enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang),
            getattr(cfg, "enabled", True))
        self._widgets["webhooks.path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.webhooks_path", lang),
            getattr(cfg, "path", "/webhooks/telegram"))
        self._widgets["webhooks.public_base_url"] = self._add_line_edit(
            layout, t("desktop.cfgedit.webhooks_public_url", lang),
            getattr(cfg, "public_base_url", None))
        self._widgets["webhooks.secret_token"] = self._add_line_edit(
            layout, t("desktop.cfgedit.webhooks_secret_token", lang),
            getattr(cfg, "secret_token", None), is_secret=True)
        self._widgets["webhooks.request_timeout_sec"] = self._add_double_spin(
            layout, t("desktop.cfgedit.webhooks_request_timeout", lang),
            getattr(cfg, "request_timeout_sec", 30.0))
        self._widgets["webhooks.max_payload_bytes"] = self._add_spin(
            layout, t("desktop.cfgedit.webhooks_max_payload_bytes", lang),
            getattr(cfg, "max_payload_bytes", 1048576), 1024, 104857600)

        return self._create_scroll_area(container)

    def _create_scheduler_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        cfg = self._current_config.scheduler

        self._widgets["scheduler.enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang),
            getattr(cfg, "enabled", False))
        self._widgets["scheduler.timezone"] = self._add_line_edit(
            layout, t("desktop.cfgedit.scheduler_timezone", lang),
            getattr(cfg, "timezone", "UTC"))
        self._widgets["scheduler.tick_interval_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.scheduler_tick_interval", lang),
            getattr(cfg, "tick_interval_sec", 60), 1, 3600)
        self._widgets["scheduler.max_concurrent_jobs"] = self._add_spin(
            layout, t("desktop.cfgedit.scheduler_max_jobs", lang),
            getattr(cfg, "max_concurrent_jobs", 1), 1, 100)
        self._widgets["scheduler.job_timeout_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.scheduler_job_timeout", lang),
            getattr(cfg, "job_timeout_sec", 3600), 1, 86400)
        self._widgets["scheduler.misfire_grace_sec"] = self._add_spin(
            layout, t("desktop.cfgedit.scheduler_misfire_grace", lang),
            getattr(cfg, "misfire_grace_sec", 30), 0, 3600)

        return self._create_scroll_area(container)

    def _create_security_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        rl = self._current_config.security.rate_limits

        self._widgets["security.rate_limits.enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang),
            getattr(rl, "enabled", False))
        self._widgets["security.rate_limits.backend"] = self._add_combo(
            layout, t("desktop.cfgedit.rl_backend", lang),
            ["sqlite"],
            getattr(rl, "backend", "sqlite"))
        self._widgets["security.rate_limits.sqlite_path"] = self._add_line_edit(
            layout, t("desktop.cfgedit.rl_sqlite_path", lang),
            getattr(rl, "sqlite_path", None))

        import dataclasses as _dc

        def _policy_to_dict(p: Any) -> Dict[str, Any]:
            if p is None:
                return {}
            if isinstance(p, dict):
                return p
            return _dc.asdict(p)

        self._widgets["security.rate_limits.default"] = self._add_json_edit(
            layout, t("desktop.cfgedit.rl_default_policy", lang),
            _policy_to_dict(getattr(rl, "default", None)))
        self._widgets["security.rate_limits.policies"] = self._add_json_edit(
            layout, t("desktop.cfgedit.rl_policies", lang),
            {k: _policy_to_dict(v) for k, v in (getattr(rl, "policies", None) or {}).items()})

        return self._create_scroll_area(container)

    def _create_lint_evolution_tab(self, lang: str) -> QWidget:
        container = QWidget()
        layout = QFormLayout(container)
        cfg = self._current_config.lint_evolution

        self._widgets["lint_evolution.enabled"] = self._add_check(
            layout, t("desktop.admin.label.monitor_enabled", lang),
            getattr(cfg, "enabled", False))
        self._widgets["lint_evolution.level1_cooldown_hours"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_level1_cooldown", lang),
            getattr(cfg, "level1_cooldown_hours", 24.0))
        self._widgets["lint_evolution.level2_cooldown_hours"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_level2_cooldown", lang),
            getattr(cfg, "level2_cooldown_hours", 720.0))
        self._widgets["lint_evolution.level3_cooldown_hours"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_level3_cooldown", lang),
            getattr(cfg, "level3_cooldown_hours", 720.0))
        self._widgets["lint_evolution.lock_ttl_minutes"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_lock_ttl", lang),
            getattr(cfg, "lock_ttl_minutes", 30.0))
        self._widgets["lint_evolution.error_retry_hours"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_error_retry", lang),
            getattr(cfg, "error_retry_hours", 1.0))
        self._widgets["lint_evolution.fp_growth_threshold_pct"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_fp_threshold", lang),
            getattr(cfg, "fp_growth_threshold_pct", 50.0))
        self._widgets["lint_evolution.canary_rolling_days"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_canary_rolling", lang),
            getattr(cfg, "canary_rolling_days", 7.0))
        self._widgets["lint_evolution.canary_baseline_days"] = self._add_double_spin(
            layout, t("desktop.cfgedit.lint_canary_baseline", lang),
            getattr(cfg, "canary_baseline_days", 30.0))
        self._widgets["lint_evolution.canary_max_schema_fields_per_180d"] = self._add_spin(
            layout, t("desktop.cfgedit.lint_canary_max_fields", lang),
            getattr(cfg, "canary_max_schema_fields_per_180d", 3), 0, 1000)

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

    def _add_json_edit(self, layout, label, value: Any) -> QPlainTextEdit:
        """Add a QPlainTextEdit pre-filled with JSON-serialised value."""
        widget = QPlainTextEdit()
        widget.setMaximumHeight(100)
        try:
            widget.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            widget.setPlainText("{}")
        layout.addRow(label, widget)
        return widget

    def _add_combo(self, layout, label, options: List[str], current: str) -> QComboBox:
        widget = QComboBox()
        for opt in options:
            widget.addItem(opt)
        idx = widget.findText(current)
        if idx >= 0:
            widget.setCurrentIndex(idx)
        layout.addRow(label, widget)
        return widget

    def _parse_json_widget(self, key: str) -> Any:
        """Parse a JSON text widget; raise ValueError with key info on failure."""
        widget = self._widgets[key]
        text = widget.toPlainText().strip()
        if not text:
            return None
        try:
            return loads_safe(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key}: {exc}") from exc

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
            new_cfg.telegram.whitelist_chat_ids = self._parse_int_list(
                self._widgets["telegram.whitelist_chat_ids"].toPlainText())
            new_cfg.telegram.admlist_chat_ids = self._parse_int_list(
                self._widgets["telegram.admlist_chat_ids"].toPlainText())
            new_cfg.telegram.connection_pool_size = self._widgets["telegram.connection_pool_size"].value()
            new_cfg.telegram.connect_timeout_sec = self._widgets["telegram.connect_timeout_sec"].value()
            new_cfg.telegram.read_timeout_sec = self._widgets["telegram.read_timeout_sec"].value()
            new_cfg.telegram.write_timeout_sec = self._widgets["telegram.write_timeout_sec"].value()
            new_cfg.telegram.pool_timeout_sec = self._widgets["telegram.pool_timeout_sec"].value()
            new_cfg.telegram.polling_timeout_sec = self._widgets["telegram.polling_timeout_sec"].value()
            new_cfg.telegram.poll_interval_sec = self._widgets["telegram.poll_interval_sec"].value()
            new_cfg.telegram.user_workdirs = self._parse_json_widget("telegram.user_workdirs") or {}
            new_cfg.telegram.user_modes = self._parse_json_widget("telegram.user_modes") or {}

            # Defaults – general
            new_cfg.defaults.workdir = self._widgets["defaults.workdir"].text()
            new_cfg.defaults.idle_timeout_sec = self._widgets["defaults.idle_timeout_sec"].value()
            new_cfg.defaults.codex_jsonl_fallback_sec = self._widgets[
                "defaults.codex_jsonl_fallback_sec"
            ].value()
            new_cfg.defaults.summary_max_chars = self._widgets["defaults.summary_max_chars"].value()
            new_cfg.defaults.html_filename_prefix = self._widgets["defaults.html_filename_prefix"].text()
            new_cfg.defaults.state_path = self._widgets["defaults.state_path"].text()
            new_cfg.defaults.desktop_state_path = self._widgets["defaults.desktop_state_path"].text()
            new_cfg.defaults.toolhelp_path = self._widgets["defaults.toolhelp_path"].text()
            new_cfg.defaults.log_path = self._widgets["defaults.log_path"].text()
            new_cfg.defaults.image_temp_dir = self._widgets["defaults.image_temp_dir"].text()
            new_cfg.defaults.image_max_mb = self._widgets["defaults.image_max_mb"].value()

            # Defaults – API keys
            new_cfg.defaults.openai_api_key = self._widgets["defaults.openai_api_key"].text() or None
            new_cfg.defaults.openai_model = self._widgets["defaults.openai_model"].text() or None
            new_cfg.defaults.openai_big_model = self._widgets["defaults.openai_big_model"].text() or None
            new_cfg.defaults.openai_base_url = self._widgets["defaults.openai_base_url"].text() or None
            new_cfg.defaults.zai_api_key = self._widgets["defaults.zai_api_key"].text() or None
            new_cfg.defaults.tavily_api_key = self._widgets["defaults.tavily_api_key"].text() or None
            new_cfg.defaults.jina_api_key = self._widgets["defaults.jina_api_key"].text() or None
            new_cfg.defaults.github_token = self._widgets["defaults.github_token"].text() or None
            new_cfg.defaults.gemini_oauth_client_secret = (
                self._widgets["defaults.gemini_oauth_client_secret"].text() or None
            )

            # Defaults – memory
            new_cfg.defaults.memory_max_kb = self._widgets["defaults.memory_max_kb"].value()
            new_cfg.defaults.memory_compact_target_kb = self._widgets["defaults.memory_compact_target_kb"].value()
            new_cfg.defaults.memory_events_enabled = self._widgets["defaults.memory_events_enabled"].isChecked()
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

            # Defaults – agent behaviour
            new_cfg.defaults.clarification_enabled = self._widgets[
                "defaults.clarification_enabled"
            ].isChecked()
            new_cfg.defaults.pending_input_confirmation_enabled = self._widgets[
                "defaults.pending_input_confirmation_enabled"
            ].isChecked()
            new_cfg.defaults.default_cli = self._widgets["defaults.default_cli"].text() or None
            new_cfg.defaults.default_execution_backend = self._widgets[
                "defaults.default_execution_backend"
            ].currentText() or "headless"
            new_cfg.defaults.clarification_keywords = self._parse_str_list(
                self._widgets["defaults.clarification_keywords"].toPlainText()
            )
            new_cfg.defaults.cli_json_stream_archive_enabled = self._widgets[
                "defaults.cli_json_stream_archive_enabled"
            ].isChecked()
            new_cfg.defaults.assistant_preview_enabled = self._widgets[
                "defaults.assistant_preview_enabled"
            ].isChecked()
            new_cfg.defaults.codebase_mapper_usage = self._widgets[
                "defaults.codebase_mapper_usage"
            ].currentText()
            new_cfg.defaults.run_artifacts_enabled = self._widgets[
                "defaults.run_artifacts_enabled"
            ].isChecked()
            new_cfg.defaults.run_artifacts_retention_days = self._widgets[
                "defaults.run_artifacts_retention_days"
            ].value()
            new_cfg.defaults.run_doctor_enabled = self._widgets["defaults.run_doctor_enabled"].isChecked()
            new_cfg.defaults.run_boundary_validation_enabled = self._widgets[
                "defaults.run_boundary_validation_enabled"
            ].isChecked()
            new_cfg.defaults.run_metrics_enabled = self._widgets["defaults.run_metrics_enabled"].isChecked()
            new_cfg.defaults.llm_trace_enabled = self._widgets["defaults.llm_trace_enabled"].isChecked()
            new_cfg.defaults.tool_disclosure = self._widgets["defaults.tool_disclosure"].currentText()
            new_cfg.defaults.context_window_tokens = self._widgets[
                "defaults.context_window_tokens"
            ].value()
            new_cfg.defaults.context_reserve_tokens = self._widgets[
                "defaults.context_reserve_tokens"
            ].value()
            new_cfg.defaults.summarization_threshold = self._widgets[
                "defaults.summarization_threshold"
            ].value()

            # Defaults – skills
            new_cfg.defaults.skill_discovery_mode = self._widgets[
                "defaults.skill_discovery_mode"
            ].currentText()
            new_cfg.defaults.skill_install_policy = self._widgets[
                "defaults.skill_install_policy"
            ].currentText()
            new_cfg.defaults.skill_registry_paths = self._parse_str_list(
                self._widgets["defaults.skill_registry_paths"].toPlainText()
            )
            new_cfg.defaults.skill_allowlisted_sources = self._parse_str_list(
                self._widgets["defaults.skill_allowlisted_sources"].toPlainText()
            )

            # Defaults – manager
            new_cfg.defaults.manager_max_tasks = self._widgets["defaults.manager_max_tasks"].value()
            new_cfg.defaults.manager_max_attempts = self._widgets["defaults.manager_max_attempts"].value()
            new_cfg.defaults.manager_decompose_timeout_sec = self._widgets[
                "defaults.manager_decompose_timeout_sec"
            ].value()
            new_cfg.defaults.manager_dev_timeout_sec = self._widgets[
                "defaults.manager_dev_timeout_sec"
            ].value()
            new_cfg.defaults.manager_review_timeout_sec = self._widgets[
                "defaults.manager_review_timeout_sec"
            ].value()
            new_cfg.defaults.manager_dev_report_max_chars = self._widgets[
                "defaults.manager_dev_report_max_chars"
            ].value()
            new_cfg.defaults.manager_auto_resume = self._widgets[
                "defaults.manager_auto_resume"
            ].isChecked()
            new_cfg.defaults.manager_auto_commit = self._widgets[
                "defaults.manager_auto_commit"
            ].isChecked()
            new_cfg.defaults.manager_response_archive = self._widgets[
                "defaults.manager_response_archive"
            ].isChecked()
            new_cfg.defaults.analyst_use_cli_timeout_sec = self._widgets[
                "defaults.analyst_use_cli_timeout_sec"
            ].value()
            new_cfg.defaults.webmaster_use_cli_timeout_sec = self._widgets[
                "defaults.webmaster_use_cli_timeout_sec"
            ].value()
            new_cfg.defaults.webmaster_validation_max_fix_iterations = self._widgets[
                "defaults.webmaster_validation_max_fix_iterations"
            ].value()
            new_cfg.defaults.cli_routing = self._parse_json_widget("defaults.cli_routing") or None

            # Tools
            for name in list(new_cfg.tools.keys()):
                prefix = f"tools.{name}."
                if f"{prefix}enabled" not in self._widgets:
                    continue
                new_cfg.tools[name].enabled = self._widgets[f"{prefix}enabled"].isChecked()
                new_cfg.tools[name].mode = self._widgets[f"{prefix}mode"].text()
                new_cfg.tools[name].cmd = self._parse_str_list(
                    self._widgets[f"{prefix}cmd"].toPlainText())
                new_cfg.tools[name].headless_cmd = self._parse_str_list(
                    self._widgets[f"{prefix}headless_cmd"].toPlainText()) or None
                new_cfg.tools[name].execution_backends = self._parse_str_list(
                    self._widgets[f"{prefix}execution_backends"].toPlainText()) or None
                new_cfg.tools[name].default_execution_backend = (
                    self._widgets[f"{prefix}default_execution_backend"].currentText() or None
                )
                new_cfg.tools[name].tmux_user = self._widgets[f"{prefix}tmux_user"].text().strip() or None
                new_cfg.tools[name].interactive_cmd = self._parse_str_list(
                    self._widgets[f"{prefix}interactive_cmd"].toPlainText()) or None
                new_cfg.tools[name].interactive_resume_cmd = self._parse_str_list(
                    self._widgets[f"{prefix}interactive_resume_cmd"].toPlainText()) or None
                new_cfg.tools[name].resume_cmd = self._parse_str_list(
                    self._widgets[f"{prefix}resume_cmd"].toPlainText()) or None
                new_cfg.tools[name].image_cmd = self._parse_str_list(
                    self._widgets[f"{prefix}image_cmd"].toPlainText()) or None
                new_cfg.tools[name].auto_commands = self._parse_str_list(
                    self._widgets[f"{prefix}auto_commands"].toPlainText()) or None
                new_cfg.tools[name].prompt_regex = (
                    self._widgets[f"{prefix}prompt_regex"].text() or None)
                new_cfg.tools[name].resume_regex = (
                    self._widgets[f"{prefix}resume_regex"].text() or None)
                new_cfg.tools[name].help_cmd = (
                    self._widgets[f"{prefix}help_cmd"].text() or None)
                new_cfg.tools[name].env = self._parse_json_widget(f"{prefix}env") or None
                new_cfg.tools[name].separate_stderr = self._widgets[
                    f"{prefix}separate_stderr"
                ].isChecked()
                new_cfg.tools[name].no_session_persistence_on_fresh = self._widgets[
                    f"{prefix}no_session_persistence_on_fresh"
                ].isChecked()

            # MCP
            new_cfg.mcp.enabled = self._widgets["mcp.enabled"].isChecked()
            new_cfg.mcp.host = self._widgets["mcp.host"].text()
            new_cfg.mcp.port = self._widgets["mcp.port"].value()
            new_cfg.mcp.token = self._widgets["mcp.token"].text() or None

            # MCP clients: already updated in-place by _on_edit_mcp_clients
            # new_cfg.mcp_clients already reflects the edits

            # Presets: already updated in-place by _on_edit_presets
            # new_cfg.presets already reflects the edits

            # MiniApp
            new_cfg.miniapp.enabled = self._widgets["miniapp.enabled"].isChecked()
            new_cfg.miniapp.bind_host = self._widgets["miniapp.bind_host"].text()
            new_cfg.miniapp.bind_port = self._widgets["miniapp.bind_port"].value()
            new_cfg.miniapp.base_path = self._widgets["miniapp.base_path"].text()
            new_cfg.miniapp.public_url = self._widgets["miniapp.public_url"].text()
            new_cfg.miniapp.max_edit_file_size_kb = self._widgets["miniapp.max_edit_file_size_kb"].value()
            new_cfg.miniapp.enable_delete = self._widgets["miniapp.enable_delete"].isChecked()

            # Thread mode
            new_cfg.thread_mode.enabled = self._widgets["thread_mode.enabled"].isChecked()
            new_cfg.thread_mode.mode = self._widgets["thread_mode.mode"].currentText()
            topics_id = self._widgets["thread_mode.topics_chat_id"].value()
            new_cfg.thread_mode.topics_chat_id = topics_id if topics_id else None
            new_cfg.thread_mode.topic_title_prefix = self._widgets["thread_mode.topic_title_prefix"].text()
            new_cfg.thread_mode.inactivity_ttl_sec = self._widgets["thread_mode.inactivity_ttl_sec"].value()

            # Webhooks
            new_cfg.webhooks.enabled = self._widgets["webhooks.enabled"].isChecked()
            new_cfg.webhooks.path = self._widgets["webhooks.path"].text()
            new_cfg.webhooks.public_base_url = self._widgets["webhooks.public_base_url"].text() or None
            new_cfg.webhooks.secret_token = self._widgets["webhooks.secret_token"].text() or None
            new_cfg.webhooks.request_timeout_sec = self._widgets["webhooks.request_timeout_sec"].value()
            new_cfg.webhooks.max_payload_bytes = self._widgets["webhooks.max_payload_bytes"].value()

            # Scheduler
            new_cfg.scheduler.enabled = self._widgets["scheduler.enabled"].isChecked()
            new_cfg.scheduler.timezone = self._widgets["scheduler.timezone"].text()
            new_cfg.scheduler.tick_interval_sec = self._widgets["scheduler.tick_interval_sec"].value()
            new_cfg.scheduler.max_concurrent_jobs = self._widgets["scheduler.max_concurrent_jobs"].value()
            new_cfg.scheduler.job_timeout_sec = self._widgets["scheduler.job_timeout_sec"].value()
            new_cfg.scheduler.misfire_grace_sec = self._widgets["scheduler.misfire_grace_sec"].value()

            # Security
            new_cfg.security.rate_limits.enabled = self._widgets[
                "security.rate_limits.enabled"
            ].isChecked()
            new_cfg.security.rate_limits.backend = self._widgets[
                "security.rate_limits.backend"
            ].currentText()
            new_cfg.security.rate_limits.sqlite_path = (
                self._widgets["security.rate_limits.sqlite_path"].text() or None
            )
            from config import SecurityRateLimitPolicyConfig as _RLPolicy
            rl_default_raw = self._parse_json_widget("security.rate_limits.default")
            if rl_default_raw and isinstance(rl_default_raw, dict):
                try:
                    new_cfg.security.rate_limits.default = _RLPolicy(**rl_default_raw)
                except Exception:
                    new_cfg.security.rate_limits.default = None
            else:
                new_cfg.security.rate_limits.default = None
            rl_policies_raw = self._parse_json_widget("security.rate_limits.policies")
            if rl_policies_raw and isinstance(rl_policies_raw, dict):
                rebuilt_policies: Dict[str, Any] = {}
                for k, v in rl_policies_raw.items():
                    if isinstance(v, dict):
                        try:
                            rebuilt_policies[k] = _RLPolicy(**v)
                        except Exception:
                            rebuilt_policies[k] = v
                    else:
                        rebuilt_policies[k] = v
                new_cfg.security.rate_limits.policies = rebuilt_policies
            else:
                new_cfg.security.rate_limits.policies = {}

            # Lint evolution
            new_cfg.lint_evolution.enabled = self._widgets["lint_evolution.enabled"].isChecked()
            new_cfg.lint_evolution.level1_cooldown_hours = self._widgets[
                "lint_evolution.level1_cooldown_hours"
            ].value()
            new_cfg.lint_evolution.level2_cooldown_hours = self._widgets[
                "lint_evolution.level2_cooldown_hours"
            ].value()
            new_cfg.lint_evolution.level3_cooldown_hours = self._widgets[
                "lint_evolution.level3_cooldown_hours"
            ].value()
            new_cfg.lint_evolution.lock_ttl_minutes = self._widgets[
                "lint_evolution.lock_ttl_minutes"
            ].value()
            new_cfg.lint_evolution.error_retry_hours = self._widgets[
                "lint_evolution.error_retry_hours"
            ].value()
            new_cfg.lint_evolution.fp_growth_threshold_pct = self._widgets[
                "lint_evolution.fp_growth_threshold_pct"
            ].value()
            new_cfg.lint_evolution.canary_rolling_days = self._widgets[
                "lint_evolution.canary_rolling_days"
            ].value()
            new_cfg.lint_evolution.canary_baseline_days = self._widgets[
                "lint_evolution.canary_baseline_days"
            ].value()
            new_cfg.lint_evolution.canary_max_schema_fields_per_180d = self._widgets[
                "lint_evolution.canary_max_schema_fields_per_180d"
            ].value()

            return new_cfg
        except Exception as e:
            self.logger.exception("Failed to collect config from UI")
            QMessageBox.critical(
                self, t("common.error", self._lang),
                t("desktop.cfgedit.collect_error", self._lang, error=str(e)))
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
        lang = self._lang
        errors = []

        token = self._widgets["telegram.token"].text().strip()
        if not token:
            errors.append(t("desktop.cfgedit.err_token_required", lang))

        workdir = self._widgets["defaults.workdir"].text().strip()
        if not workdir:
            errors.append(t("desktop.cfgedit.err_workdir_required", lang))

        if self._widgets["mcp.enabled"].isChecked():
            if not self._widgets["mcp.host"].text().strip():
                errors.append(t("desktop.cfgedit.err_mcp_host_required", lang))

        # Validate JSON fields
        json_keys = [
            "telegram.user_workdirs",
            "telegram.user_modes",
            "defaults.cli_routing",
            "security.rate_limits.default",
            "security.rate_limits.policies",
        ]
        for key in json_keys:
            if key not in self._widgets:
                continue
            text = self._widgets[key].toPlainText().strip()
            if text:
                try:
                    loads_safe(text)
                except json.JSONDecodeError as exc:
                    errors.append(t("desktop.cfgedit.err_json_field_invalid", lang, field=key, error=str(exc)))
        # Also validate per-tool env JSON fields
        for key, widget in self._widgets.items():
            if key.endswith(".env") and key.startswith("tools."):
                text = widget.toPlainText().strip() if isinstance(widget, QPlainTextEdit) else ""
                if text:
                    try:
                        loads_safe(text)
                    except json.JSONDecodeError as exc:
                        errors.append(t("desktop.cfgedit.err_json_field_invalid", lang, field=key, error=str(exc)))

        # Warning for env vars
        for key, widget in self._widgets.items():
            if isinstance(widget, QLineEdit):
                val = widget.text().strip()
                if val.startswith("$"):
                    # Users might intentionally use env vars like "$VAR".
                    # No special handling is required in Desktop config editor.
                    self.logger.debug("Config editor preserves env-placeholder text field=%s", key)

        if errors:
            QMessageBox.warning(self, t("desktop.cfgedit.validation_error", lang), "\n".join(errors))
            return False

        return True

    def _save_result_message(self, result: ConfigDraftSaveResult) -> tuple[str, str]:
        lang = self._lang
        if not result.ok:
            lines = [t("desktop.cfgedit.msg_not_saved", lang)]
            if result.errors:
                lines.append("")
                lines.append(t("desktop.cfgedit.msg_errors", lang) + ":")
                lines.extend(f"- {error}" for error in result.errors)
            return t("desktop.cfgedit.save_failed", lang), "\n".join(lines)

        yes_str = t("common.yes", lang)
        no_str = t("common.no", lang)
        lines = [t("desktop.cfgedit.msg_changed", lang, value=yes_str if result.changed else no_str)]
        if result.backup_path:
            lines.append(t("desktop.cfgedit.msg_backup", lang, path=result.backup_path))
        if result.restart_required:
            lines.append(t("desktop.cfgedit.msg_restart_required", lang) + ": " + ", ".join(result.restart_required))
        else:
            lines.append(t("desktop.cfgedit.msg_restart_none", lang))
        if result.reloadable:
            lines.append(t("desktop.cfgedit.msg_reloadable", lang) + ": " + ", ".join(result.reloadable))
        else:
            lines.append(t("desktop.cfgedit.msg_reloadable_none", lang))
        if not result.changed:
            lines.insert(0, t("desktop.cfgedit.msg_up_to_date", lang))
            return t("desktop.cfgedit.no_changes", lang), "\n".join(lines)
        lines.insert(0, t("desktop.cfgedit.msg_saved", lang))
        return t("desktop.cfgedit.success", lang), "\n".join(lines)

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
                    dialog = DiffDialog(diff, self._lang, self)
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
                QMessageBox.critical(
                    self, t("common.error", self._lang),
                    t("desktop.cfgedit.save_error", self._lang, error=str(e)))
            finally:
                self.saveFinished.emit()

        ensure_async(_save(), parent=self)

    @Slot()
    def _on_reload_runtime_clicked(self) -> None:
        if self._facade is None:
            QMessageBox.warning(
                self, t("common.error", self._lang),
                t("desktop.cfgedit.reload_runtime_err", self._lang))
            return

        async def _reload():
            try:
                result = await self._facade.reload_runtime_config()
                if result.get("status") == "error":
                    QMessageBox.warning(
                        self, t("common.error", self._lang),
                        t("desktop.cfgedit.reload_runtime_err", self._lang))
                else:
                    applied = result.get("applied") or []
                    applied_str = ", ".join(applied) if applied else "—"
                    QMessageBox.information(
                        self,
                        t("desktop.cfgedit.success", self._lang),
                        t("desktop.cfgedit.reload_runtime_ok", self._lang, applied=applied_str),
                    )
            except Exception as e:
                self.logger.exception("reload_runtime_config failed")
                QMessageBox.critical(
                    self, t("common.error", self._lang),
                    t("desktop.cfgedit.reload_runtime_err", self._lang) + f": {e}")

        ensure_async(_reload(), parent=self)

    def retranslate_ui(self, lang: str) -> None:
        self._lang = lang
        self.save_btn.setText(t("desktop.cfgedit.save_config", lang))
        self.reload_btn.setText(t("desktop.btn.reload", lang))
        self.reload_runtime_btn.setText(t("desktop.cfgedit.reload_runtime_btn", lang))
        if self._current_config:
            self._update_ui()
