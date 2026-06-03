from __future__ import annotations

import asyncio
import importlib.util
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.path_normalization import normalize_optional_state_path
from desktop.widgets.admin_chat_section import AdminChatSection
from desktop.widgets.scheduler_panel import SchedulerPanelWidget
from i18n import t
from session import session_runtime_uid
from utils.ui import ensure_async
from utils.ui import format_session_title

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade


@lru_cache(maxsize=1)
def _load_admin_state_store_class() -> Optional[type]:
    state_store_path = Path(__file__).resolve().parents[2] / "modes" / "admin" / "state_store.py"
    spec = importlib.util.spec_from_file_location("desktop_admin_state_store_runtime", state_store_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "AdminStateStore", None)


class AdminPanel(QWidget):
    """Desktop-панель Admin с локальным выбором сессии и разделами состояния."""

    enableRequested = Signal(str)
    disableRequested = Signal(str)
    rescanRequested = Signal(str)

    def __init__(
        self,
        facade: ApplicationFacade,
        *,
        actor_id: str = "desktop",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.facade = facade
        self.actor_id = str(actor_id)
        self.logger = logging.getLogger(__name__)
        self._active_session_uid: Optional[str] = None
        self._sessions_by_uid: dict[str, Any] = {}
        self._status_payload: dict[str, Any] = {}
        self._pending_skill_install_actions: set[str] = set()
        self.setObjectName("admin_panel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)

        self.session_selector_label = QLabel("Session:")
        controls_layout.addWidget(self.session_selector_label)
        self.session_selector = QComboBox()
        self.session_selector.setObjectName("admin_panel_session_selector")
        self.session_selector.currentIndexChanged.connect(self._on_session_selector_changed)
        controls_layout.addWidget(self.session_selector, 1)

        self.enable_button = QPushButton("Enable")
        self.enable_button.setObjectName("admin_panel_enable_button")
        self.enable_button.clicked.connect(self._emit_enable_requested)
        controls_layout.addWidget(self.enable_button)

        self.disable_button = QPushButton("Disable")
        self.disable_button.setObjectName("admin_panel_disable_button")
        self.disable_button.clicked.connect(self._emit_disable_requested)
        controls_layout.addWidget(self.disable_button)

        self.rescan_button = QPushButton("Rescan")
        self.rescan_button.setObjectName("admin_panel_rescan_button")
        self.rescan_button.clicked.connect(self._emit_rescan_requested)
        controls_layout.addWidget(self.rescan_button)
        layout.addLayout(controls_layout)

        self.state_stack = QStackedWidget()
        self.state_stack.setObjectName("admin_panel_state_stack")
        layout.addWidget(self.state_stack, 1)

        self.enabled_page = QWidget()
        enabled_layout = QVBoxLayout(self.enabled_page)
        enabled_layout.setContentsMargins(0, 0, 0, 0)
        enabled_layout.setSpacing(8)

        self.admin_tabs = QTabWidget()
        self.admin_tabs.setObjectName("admin_panel_tabs")
        enabled_layout.addWidget(self.admin_tabs, 1)

        overview_tab = QWidget()
        overview_layout = QVBoxLayout(overview_tab)
        overview_layout.setContentsMargins(6, 6, 6, 6)
        overview_layout.setSpacing(8)

        operations_tab = QWidget()
        operations_layout = QVBoxLayout(operations_tab)
        operations_layout.setContentsMargins(6, 6, 6, 6)
        operations_layout.setSpacing(8)

        monitor_tab = QWidget()
        monitor_tab_layout = QVBoxLayout(monitor_tab)
        monitor_tab_layout.setContentsMargins(6, 6, 6, 6)
        monitor_tab_layout.setSpacing(8)

        config_tab = QWidget()
        config_layout = QVBoxLayout(config_tab)
        config_layout.setContentsMargins(6, 6, 6, 6)
        config_layout.setSpacing(8)

        chat_tab = QWidget()
        chat_layout = QVBoxLayout(chat_tab)
        chat_layout.setContentsMargins(6, 6, 6, 6)
        chat_layout.setSpacing(8)

        self.session_state_label = QLabel("Session: None")
        self.session_state_label.setObjectName("admin_panel_session_state")
        self.session_state_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        overview_layout.addWidget(self.session_state_label)

        self.status_form = QFormLayout()
        self.status_form.setContentsMargins(0, 0, 0, 0)
        self.status_form.setSpacing(8)

        self.operations_form = QFormLayout()
        self.operations_form.setContentsMargins(0, 0, 0, 0)
        self.operations_form.setSpacing(8)

        self.pipeline_status_value_label = QLabel("-")
        self.pipeline_status_value_label.setObjectName("admin_panel_pipeline_status_value")
        self.pipeline_row_label = QLabel("Pipeline:")
        self.status_form.addRow(self.pipeline_row_label, self.pipeline_status_value_label)

        self.monitor_status_value_label = QLabel("-")
        self.monitor_status_value_label.setObjectName("admin_panel_monitor_status_value")
        self.monitor_row_label = QLabel("Monitor:")
        self.status_form.addRow(self.monitor_row_label, self.monitor_status_value_label)

        self.analyzer_status_value_label = QLabel("-")
        self.analyzer_status_value_label.setObjectName("admin_panel_analyzer_status_value")
        self.analyzer_row_label = QLabel("Analyzer:")
        self.status_form.addRow(self.analyzer_row_label, self.analyzer_status_value_label)

        self.analyzer_detail_value_label = QLabel("-")
        self.analyzer_detail_value_label.setObjectName("admin_panel_analyzer_detail_value")
        self.analyzer_detail_value_label.setWordWrap(True)
        self.analyzer_detail_row_label = QLabel("Analyzer details:")
        self.status_form.addRow(self.analyzer_detail_row_label, self.analyzer_detail_value_label)

        self.executor_status_value_label = QLabel("-")
        self.executor_status_value_label.setObjectName("admin_panel_executor_status_value")
        self.executor_row_label = QLabel("Executor:")
        self.status_form.addRow(self.executor_row_label, self.executor_status_value_label)

        self.executor_detail_value_label = QLabel("-")
        self.executor_detail_value_label.setObjectName("admin_panel_executor_detail_value")
        self.executor_detail_value_label.setWordWrap(True)
        self.executor_detail_row_label = QLabel("Executor details:")
        self.status_form.addRow(self.executor_detail_row_label, self.executor_detail_value_label)

        self.notifier_status_value_label = QLabel("-")
        self.notifier_status_value_label.setObjectName("admin_panel_notifier_status_value")
        self.notifier_row_label = QLabel("Notifier:")
        self.status_form.addRow(self.notifier_row_label, self.notifier_status_value_label)

        self.scan_status_value_label = QLabel("-")
        self.scan_status_value_label.setObjectName("admin_panel_scan_status_value")
        self.scan_row_label = QLabel("Scan:")
        self.status_form.addRow(self.scan_row_label, self.scan_status_value_label)

        self.pinned_cli_value_label = QLabel("-")
        self.pinned_cli_value_label.setObjectName("admin_panel_pinned_cli_value")
        self.pinned_cli_value_label.setWordWrap(True)
        self.pinned_cli_row_label = QLabel("Pinned CLI:")
        self.status_form.addRow(self.pinned_cli_row_label, self.pinned_cli_value_label)

        self.executor_profile_value_label = QLabel("-")
        self.executor_profile_value_label.setObjectName("admin_panel_executor_profile_value")
        self.executor_profile_row_label = QLabel("Executor profile:")
        self.status_form.addRow(self.executor_profile_row_label, self.executor_profile_value_label)

        self.readiness_value_label = QLabel("-")
        self.readiness_value_label.setObjectName("admin_panel_readiness_value")
        self.readiness_value_label.setWordWrap(True)
        self.readiness_row_label = QLabel("Readiness:")
        self.status_form.addRow(self.readiness_row_label, self.readiness_value_label)

        self.runtime_flags_value_label = QLabel("-")
        self.runtime_flags_value_label.setObjectName("admin_panel_runtime_flags_value")
        self.runtime_flags_value_label.setWordWrap(True)
        self.runtime_row_label = QLabel("Runtime:")
        self.status_form.addRow(self.runtime_row_label, self.runtime_flags_value_label)

        self.scan_detail_value_label = QLabel("-")
        self.scan_detail_value_label.setObjectName("admin_panel_scan_detail_value")
        self.scan_detail_value_label.setWordWrap(True)
        self.scan_detail_row_label = QLabel("Scan details:")
        self.status_form.addRow(self.scan_detail_row_label, self.scan_detail_value_label)

        self.snapshot_summary_value_label = QLabel("-")
        self.snapshot_summary_value_label.setObjectName("admin_panel_snapshot_summary_value")
        self.snapshot_summary_value_label.setWordWrap(True)
        self.snapshot_row_label = QLabel("Last snapshot:")
        self.status_form.addRow(self.snapshot_row_label, self.snapshot_summary_value_label)

        self.decision_summary_value_label = QLabel("-")
        self.decision_summary_value_label.setObjectName("admin_panel_decision_summary_value")
        self.decision_summary_value_label.setWordWrap(True)
        self.decision_row_label = QLabel("Last decision:")
        self.status_form.addRow(self.decision_row_label, self.decision_summary_value_label)

        self.action_summary_value_label = QLabel("-")
        self.action_summary_value_label.setObjectName("admin_panel_action_summary_value")
        self.action_summary_value_label.setWordWrap(True)
        self.action_row_label = QLabel("Last action:")
        self.status_form.addRow(self.action_row_label, self.action_summary_value_label)

        self.pending_value_label = QLabel("-")
        self.pending_value_label.setObjectName("admin_panel_pending_value")
        self.pending_value_label.setWordWrap(True)
        self.pending_row_label = QLabel("Waiting now:")
        self.operations_form.addRow(self.pending_row_label, self.pending_value_label)

        self.mute_state_value_label = QLabel("-")
        self.mute_state_value_label.setObjectName("admin_panel_mute_state_value")
        self.mute_state_value_label.setWordWrap(True)
        self.mute_row_label = QLabel("Mute:")
        self.operations_form.addRow(self.mute_row_label, self.mute_state_value_label)

        self.recent_incidents_value_label = QLabel("-")
        self.recent_incidents_value_label.setObjectName("admin_panel_recent_incidents_value")
        self.recent_incidents_value_label.setWordWrap(True)
        self.incidents_row_label = QLabel("Recent incidents:")
        self.operations_form.addRow(self.incidents_row_label, self.recent_incidents_value_label)

        self.recent_actions_value_label = QLabel("-")
        self.recent_actions_value_label.setObjectName("admin_panel_recent_actions_value")
        self.recent_actions_value_label.setWordWrap(True)
        self.recent_actions_row_label = QLabel("Recent admin actions:")
        self.operations_form.addRow(self.recent_actions_row_label, self.recent_actions_value_label)

        self.approved_overrides_value_label = QLabel("-")
        self.approved_overrides_value_label.setObjectName("admin_panel_approved_overrides_value")
        self.approved_overrides_value_label.setWordWrap(True)
        self.overrides_row_label = QLabel("Approved overrides:")
        self.operations_form.addRow(self.overrides_row_label, self.approved_overrides_value_label)

        self.skill_installs_value_label = QLabel("-")
        self.skill_installs_value_label.setObjectName("admin_panel_skill_installs_value")
        self.skill_installs_value_label.setWordWrap(True)
        self.skill_installs_row_label = QLabel("Pending skill installs:")
        self.operations_form.addRow(self.skill_installs_row_label, self.skill_installs_value_label)

        self.skill_approval_selector = QComboBox()
        self.skill_approval_selector.setObjectName("admin_panel_skill_approval_selector")
        self.skill_approval_selector.currentIndexChanged.connect(self._render_skill_install_action_state)

        self.skill_approve_button = QPushButton("Approve skill")
        self.skill_approve_button.setObjectName("admin_panel_skill_approve_button")
        self.skill_approve_button.clicked.connect(self._trigger_skill_install_approve)

        self.skill_reject_button = QPushButton("Reject skill")
        self.skill_reject_button.setObjectName("admin_panel_skill_reject_button")
        self.skill_reject_button.clicked.connect(self._trigger_skill_install_reject)

        skill_actions_layout = QHBoxLayout()
        skill_actions_layout.setContentsMargins(0, 0, 0, 0)
        skill_actions_layout.setSpacing(8)
        skill_actions_layout.addWidget(self.skill_approval_selector, 1)
        skill_actions_layout.addWidget(self.skill_approve_button)
        skill_actions_layout.addWidget(self.skill_reject_button)
        self.skill_action_row_label = QLabel("Skill action:")
        self.operations_form.addRow(self.skill_action_row_label, skill_actions_layout)

        self.skill_action_result_label = QLabel("")
        self.skill_action_result_label.setObjectName("admin_panel_skill_action_result")
        self.skill_action_result_label.setWordWrap(True)
        self.skill_action_result_label.hide()

        overview_layout.addLayout(self.status_form)
        overview_layout.addStretch(1)

        self.runs_title_label = QLabel("Pipeline runs")
        runs_title = self.runs_title_label
        runs_title.setObjectName("admin_panel_runs_title")
        operations_layout.addLayout(self.operations_form)
        operations_layout.addWidget(self.skill_action_result_label)
        operations_layout.addWidget(runs_title)

        runs_controls = QHBoxLayout()
        runs_controls.setContentsMargins(0, 0, 0, 0)
        runs_controls.setSpacing(8)
        self.runs_refresh_button = QPushButton("Refresh runs")
        self.runs_refresh_button.setObjectName("admin_panel_runs_refresh_button")
        self.runs_refresh_button.clicked.connect(self._refresh_admin_runs)
        runs_controls.addWidget(self.runs_refresh_button)
        self.runs_view_button = QPushButton("View run details")
        self.runs_view_button.setObjectName("admin_panel_runs_view_button")
        self.runs_view_button.clicked.connect(self._view_admin_run_detail)
        runs_controls.addWidget(self.runs_view_button)
        runs_controls.addStretch(1)
        operations_layout.addLayout(runs_controls)

        self.runs_table = QTableWidget(0, 4)
        self.runs_table.setObjectName("admin_panel_runs_table")
        self.runs_table.setHorizontalHeaderLabels(["run_id", "status", "phase", "started_at"])
        self.runs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.runs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.runs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.runs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.runs_table.horizontalHeader().setStretchLastSection(True)
        self.runs_table.setMinimumHeight(150)
        operations_layout.addWidget(self.runs_table)

        self.run_detail_label = QLabel("Выберите run и нажмите «View run details».")
        self.run_detail_label.setObjectName("admin_panel_run_detail_label")
        self.run_detail_label.setWordWrap(True)
        operations_layout.addWidget(self.run_detail_label)

        self.run_detail_view = QPlainTextEdit()
        self.run_detail_view.setObjectName("admin_panel_run_detail_view")
        self.run_detail_view.setReadOnly(True)
        self.run_detail_view.setMinimumHeight(150)
        operations_layout.addWidget(self.run_detail_view)

        self.ssh_actions_grp = QGroupBox("SSH actions (admin.actions.ssh)")
        ssh_actions_grp = self.ssh_actions_grp
        ssh_actions_layout = QVBoxLayout(ssh_actions_grp)
        ssh_actions_layout.setContentsMargins(8, 8, 8, 8)
        ssh_actions_layout.setSpacing(6)
        ssh_actions_controls = QHBoxLayout()
        ssh_actions_controls.setSpacing(8)
        self.ssh_actions_reload_button = QPushButton("Reload")
        self.ssh_actions_reload_button.clicked.connect(self._reload_admin_ssh_actions)
        ssh_actions_controls.addWidget(self.ssh_actions_reload_button)
        self.ssh_actions_add_button = QPushButton("Добавить")
        self.ssh_actions_add_button.clicked.connect(self._add_admin_ssh_action_row)
        ssh_actions_controls.addWidget(self.ssh_actions_add_button)
        self.ssh_actions_save_button = QPushButton("Сохранить")
        self.ssh_actions_save_button.clicked.connect(self._save_admin_ssh_actions)
        ssh_actions_controls.addWidget(self.ssh_actions_save_button)
        ssh_actions_controls.addStretch(1)
        ssh_actions_layout.addLayout(ssh_actions_controls)
        self.ssh_actions_table = QTableWidget(0, 6)
        self.ssh_actions_table.setHorizontalHeaderLabels([
            "action_id", "argv (по строке)", "timeout_sec", "risk_level", "description", "",
        ])
        self.ssh_actions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.ssh_actions_table.verticalHeader().setVisible(False)
        self.ssh_actions_table.setMinimumHeight(140)
        ssh_actions_layout.addWidget(self.ssh_actions_table)
        self.ssh_actions_status_label = QLabel("")
        self.ssh_actions_status_label.setWordWrap(True)
        ssh_actions_layout.addWidget(self.ssh_actions_status_label)
        monitor_tab_layout.addWidget(ssh_actions_grp)

        self.monitor_grp = QGroupBox("Monitor servers (admin.monitor.servers)")
        monitor_grp = self.monitor_grp
        monitor_layout = QVBoxLayout(monitor_grp)
        monitor_layout.setContentsMargins(8, 8, 8, 8)
        monitor_layout.setSpacing(6)
        monitor_controls = QHBoxLayout()
        monitor_controls.setSpacing(8)
        self.monitor_servers_reload_button = QPushButton("Reload")
        self.monitor_servers_reload_button.clicked.connect(self._reload_admin_monitor_servers)
        monitor_controls.addWidget(self.monitor_servers_reload_button)
        self.monitor_servers_add_button = QPushButton("Добавить")
        self.monitor_servers_add_button.clicked.connect(self._add_admin_monitor_server_row)
        monitor_controls.addWidget(self.monitor_servers_add_button)
        self.monitor_servers_save_button = QPushButton("Сохранить")
        self.monitor_servers_save_button.clicked.connect(self._save_admin_monitor_servers)
        monitor_controls.addWidget(self.monitor_servers_save_button)
        monitor_controls.addStretch(1)
        monitor_layout.addLayout(monitor_controls)
        monitor_meta = QHBoxLayout()
        monitor_meta.setSpacing(10)
        self.monitor_enabled_checkbox = QCheckBox("Включён")
        self.monitor_enabled_checkbox.setObjectName("admin_panel_monitor_enabled_checkbox")
        monitor_meta.addWidget(self.monitor_enabled_checkbox)
        monitor_meta.addWidget(QLabel("interval_sec:"))
        self.monitor_interval_spin = QDoubleSpinBox()
        self.monitor_interval_spin.setRange(1.0, 3600.0)
        self.monitor_interval_spin.setDecimals(0)
        self.monitor_interval_spin.setValue(30)
        monitor_meta.addWidget(self.monitor_interval_spin)
        monitor_meta.addStretch(1)
        monitor_layout.addLayout(monitor_meta)
        self.monitor_servers_table = QTableWidget(0, 4)
        self.monitor_servers_table.setHorizontalHeaderLabels([
            "server (alias)", "action_id", "timeout_sec", "",
        ])
        self.monitor_servers_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.monitor_servers_table.verticalHeader().setVisible(False)
        self.monitor_servers_table.setMinimumHeight(140)
        monitor_layout.addWidget(self.monitor_servers_table)
        self.monitor_hint_label = QLabel("SSH-хосты редактируются на вкладке «Настройки». Здесь выбирается только server → action.")
        monitor_hint = self.monitor_hint_label
        monitor_hint.setWordWrap(True)
        monitor_layout.addWidget(monitor_hint)
        self.monitor_servers_status_label = QLabel("")
        self.monitor_servers_status_label.setWordWrap(True)
        monitor_layout.addWidget(self.monitor_servers_status_label)
        monitor_tab_layout.addWidget(monitor_grp)

        self._admin_hosts_cache: list[dict] = []

        self.config_title_label = QLabel("Admin config (YAML, read-only)")
        config_title = self.config_title_label
        config_title.setObjectName("admin_panel_config_title")
        config_layout.addWidget(config_title)

        config_controls = QHBoxLayout()
        config_controls.setContentsMargins(0, 0, 0, 0)
        config_controls.setSpacing(8)
        self.config_reload_button = QPushButton("Reload config")
        self.config_reload_button.setObjectName("admin_panel_config_reload_button")
        self.config_reload_button.clicked.connect(self._reload_admin_config)
        config_controls.addWidget(self.config_reload_button)
        config_controls.addStretch(1)
        config_layout.addLayout(config_controls)

        self.config_editor = QPlainTextEdit()
        self.config_editor.setObjectName("admin_panel_config_editor")
        self.config_editor.setMinimumHeight(200)
        self.config_editor.setReadOnly(True)
        self.config_editor.setPlaceholderText("Нажмите «Reload config» для просмотра итогового YAML.")
        config_layout.addWidget(self.config_editor, 1)

        self.config_status_label = QLabel("")
        self.config_status_label.setObjectName("admin_panel_config_status")
        self.config_status_label.setWordWrap(True)
        config_layout.addWidget(self.config_status_label)

        self.chat_section = AdminChatSection(
            self.facade,
            get_session_uid=lambda: self._active_session_uid,
            get_status_payload=lambda: self._status_payload,
            parent=self,
        )
        chat_layout.addWidget(self.chat_section, 1)

        self.autonomy_panel = AdminAutonomyPanel(self.facade, parent=self)
        self.autonomy_panel.setObjectName("admin_panel_autonomy_panel")

        self.scheduler_panel = SchedulerPanelWidget(self.facade, actor_id=self.actor_id)
        self.scheduler_panel.setObjectName("admin_panel_scheduler_panel")

        self.admin_tabs.addTab(overview_tab, "Обзор")
        self.admin_tabs.addTab(operations_tab, "Операции")
        self.admin_tabs.addTab(monitor_tab, "Мониторинг")
        self.admin_tabs.addTab(config_tab, "Config")
        self.admin_tabs.addTab(chat_tab, "Chat")
        self.admin_tabs.addTab(self.autonomy_panel, "Autonomy")
        self.admin_tabs.addTab(self.scheduler_panel, "Scheduler")

        self.disabled_page = QWidget()
        disabled_layout = QVBoxLayout(self.disabled_page)
        disabled_layout.setContentsMargins(0, 0, 0, 0)
        disabled_layout.setSpacing(8)

        self.disabled_title_label = QLabel("Admin disabled")
        self.disabled_title_label.setObjectName("admin_panel_disabled_title")
        self.disabled_title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        disabled_layout.addWidget(self.disabled_title_label)

        self.disabled_hint_label = QLabel("Enable admin for this session or rescan its environment.")
        self.disabled_hint_label.setObjectName("admin_panel_disabled_hint")
        self.disabled_hint_label.setWordWrap(True)
        disabled_layout.addWidget(self.disabled_hint_label)
        disabled_layout.addStretch(1)

        self.state_stack.addWidget(self.enabled_page)
        self.state_stack.addWidget(self.disabled_page)

        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)
        self._status_refresh_timer = QTimer(self)
        self._status_refresh_timer.setInterval(1000)
        self._status_refresh_timer.timeout.connect(self.refresh_status_payload)
        self._status_refresh_timer.start()
        self.refresh_sessions()

    def retranslate_ui(self, lang: str) -> None:
        """Re-set all static UI strings using i18n.t(key, lang)."""
        self.session_selector_label.setText(t("desktop.admin.label.session", lang))
        self.enable_button.setText(t("desktop.admin.btn.enable", lang))
        self.disable_button.setText(t("desktop.admin.btn.disable", lang))
        self.rescan_button.setText(t("desktop.admin.btn.rescan", lang))
        self.admin_tabs.setTabText(0, t("desktop.admin.tab.overview", lang))
        self.admin_tabs.setTabText(1, t("desktop.admin.tab.operations", lang))
        self.admin_tabs.setTabText(2, t("desktop.admin.tab.monitor", lang))
        self.admin_tabs.setTabText(3, t("desktop.admin.tab.config", lang))
        self.admin_tabs.setTabText(4, t("desktop.admin.tab.chat", lang))
        self.admin_tabs.setTabText(5, t("desktop.admin.tab.autonomy", lang))
        self.admin_tabs.setTabText(6, t("desktop.admin.tab.scheduler", lang))
        self.pipeline_row_label.setText(t("desktop.admin.label.pipeline", lang))
        self.monitor_row_label.setText(t("desktop.admin.label.monitor", lang))
        self.analyzer_row_label.setText(t("desktop.admin.label.analyzer", lang))
        self.analyzer_detail_row_label.setText(t("desktop.admin.label.analyzer_detail", lang))
        self.executor_row_label.setText(t("desktop.admin.label.executor", lang))
        self.executor_detail_row_label.setText(t("desktop.admin.label.executor_detail", lang))
        self.notifier_row_label.setText(t("desktop.admin.label.notifier", lang))
        self.scan_row_label.setText(t("desktop.admin.label.scan", lang))
        self.pinned_cli_row_label.setText(t("desktop.admin.label.pinned_cli", lang))
        self.executor_profile_row_label.setText(t("desktop.admin.label.executor_profile", lang))
        self.readiness_row_label.setText(t("desktop.admin.label.readiness", lang))
        self.runtime_row_label.setText(t("desktop.admin.label.runtime", lang))
        self.scan_detail_row_label.setText(t("desktop.admin.label.scan_detail", lang))
        self.snapshot_row_label.setText(t("desktop.admin.label.last_snapshot", lang))
        self.decision_row_label.setText(t("desktop.admin.label.last_decision", lang))
        self.action_row_label.setText(t("desktop.admin.label.last_action", lang))
        self.pending_row_label.setText(t("desktop.admin.label.waiting_now", lang))
        self.mute_row_label.setText(t("desktop.admin.label.mute", lang))
        self.incidents_row_label.setText(t("desktop.admin.label.recent_incidents", lang))
        self.recent_actions_row_label.setText(t("desktop.admin.label.recent_actions", lang))
        self.overrides_row_label.setText(t("desktop.admin.label.approved_overrides", lang))
        self.skill_installs_row_label.setText(t("desktop.admin.label.pending_skill_installs", lang))
        self.skill_action_row_label.setText(t("desktop.admin.label.skill_action", lang))
        self.skill_approve_button.setText(t("desktop.admin.btn.approve_skill", lang))
        self.skill_reject_button.setText(t("desktop.admin.btn.reject_skill", lang))
        self.runs_title_label.setText(t("desktop.admin.label.pipeline_runs", lang))
        self.runs_refresh_button.setText(t("desktop.admin.btn.refresh_runs", lang))
        self.runs_view_button.setText(t("desktop.admin.btn.view_run_details", lang))
        self.ssh_actions_grp.setTitle(t("desktop.admin.label.ssh_actions_group", lang))
        self.ssh_actions_reload_button.setText(t("desktop.admin.btn.reload", lang))
        self.ssh_actions_add_button.setText(t("desktop.admin.btn.add_row", lang))
        self.ssh_actions_save_button.setText(t("desktop.admin.btn.save_rows", lang))
        self.monitor_grp.setTitle(t("desktop.admin.label.monitor_servers_group", lang))
        self.monitor_servers_reload_button.setText(t("desktop.admin.btn.reload", lang))
        self.monitor_servers_add_button.setText(t("desktop.admin.btn.add_row", lang))
        self.monitor_servers_save_button.setText(t("desktop.admin.btn.save_rows", lang))
        self.monitor_enabled_checkbox.setText(t("desktop.admin.label.monitor_enabled", lang))
        self.monitor_hint_label.setText(t("desktop.admin.label.monitor_hint", lang))
        self.config_title_label.setText(t("desktop.admin.label.config_title", lang))
        self.config_reload_button.setText(t("desktop.admin.btn.reload_config", lang))
        self.config_editor.setPlaceholderText(t("desktop.admin.label.config_placeholder", lang))
        self.disabled_title_label.setText(t("desktop.admin.label.disabled_title", lang))
        self.disabled_hint_label.setText(t("desktop.admin.label.disabled_hint", lang))

    @property
    def active_session_uid(self) -> Optional[str]:
        return self._active_session_uid

    def refresh_sessions(self) -> None:
        sessions = list(self.facade.session_service.list_desktop_sessions() or [])
        selected_uid = self._active_session_uid
        self._sessions_by_uid = {}

        self.session_selector.blockSignals(True)
        self.session_selector.clear()

        for session in sessions:
            session_uid = session_runtime_uid(session)
            if not session_uid:
                continue
            self._sessions_by_uid[session_uid] = session
            label = format_session_title(session)
            self.session_selector.addItem(label, session_uid)
        if selected_uid:
            index = self.session_selector.findData(str(selected_uid))
            if index >= 0:
                self.session_selector.setCurrentIndex(index)
                self._update_active_session(str(selected_uid))
            else:
                self._update_active_session(None)

        self.session_selector.blockSignals(False)

        if self.session_selector.count() == 0:
            self._update_active_session(None)
            self.session_selector.setEnabled(False)
            return

        self.session_selector.setEnabled(True)
        current_id = self.session_selector.currentData()
        self._update_active_session(str(current_id) if current_id else None)

    def set_session(self, session_uid: Optional[str]) -> None:
        self.refresh_sessions()
        target_session_uid = str(session_uid or "").strip() or None
        if target_session_uid is None or self.session_selector.count() == 0:
            self._update_active_session(target_session_uid)
            return

        index = self.session_selector.findData(target_session_uid)
        if index < 0:
            self._update_active_session(target_session_uid)
            return

        self.session_selector.blockSignals(True)
        self.session_selector.setCurrentIndex(index)
        self.session_selector.blockSignals(False)
        self._update_active_session(target_session_uid)

    def _on_session_selector_changed(self, index: int) -> None:
        if index < 0:
            self._update_active_session(None)
            return
        session_uid = self.session_selector.itemData(index)
        self._update_active_session(str(session_uid) if session_uid else None)

    def _update_active_session(self, session_uid: Optional[str]) -> None:
        self._active_session_uid = str(session_uid).strip() if session_uid else None
        self._pending_skill_install_actions.clear()
        self.skill_action_result_label.hide()
        self.skill_action_result_label.setText("")
        self.session_state_label.setText(f"Session: {self._active_session_uid or 'None'}")
        self.scheduler_panel.set_context_session(self._active_session_uid)
        autonomy_panel = getattr(self, "autonomy_panel", None)
        if autonomy_panel is not None:
            autonomy_panel.set_session(self._active_session_uid)
        self.refresh_status_payload()
        self._render_current_state()

    def _render_current_state(self) -> None:
        admin_enabled = self._is_admin_enabled_for_session(self._active_session_uid)
        has_session = bool(self._active_session_uid)
        self.enable_button.setEnabled(has_session and not admin_enabled)
        self.disable_button.setEnabled(has_session and admin_enabled)
        self.rescan_button.setEnabled(has_session)
        self.state_stack.setCurrentWidget(self.enabled_page if admin_enabled else self.disabled_page)
        self._render_skill_install_action_state()
        chat_section = getattr(self, "chat_section", None)
        if chat_section is not None:
            if admin_enabled:
                chat_section.start()
            else:
                chat_section.stop()

    def refresh_status_payload(self) -> None:
        payload: dict[str, Any] = {}
        session_uid = self._active_session_uid
        loader = getattr(self.facade, "get_admin_status_payload", None)
        if session_uid and callable(loader):
            try:
                raw_payload = loader(session_uid)
            except Exception:
                self.logger.exception("failed to load admin status payload session_uid=%s", session_uid)
                raw_payload = None
            if isinstance(raw_payload, Mapping):
                payload = dict(raw_payload)
        self._status_payload = payload
        self._render_status_payload()

    def _render_status_payload(self) -> None:
        payload = dict(self._status_payload or {})
        self.pipeline_status_value_label.setText(str(payload.get("pipeline_status") or "-"))
        self.monitor_status_value_label.setText(str(payload.get("monitor_status") or "-"))
        self.analyzer_status_value_label.setText(str(payload.get("analyzer_status") or "-"))
        self.analyzer_detail_value_label.setText(str(payload.get("analyzer_message") or "-"))
        self.executor_status_value_label.setText(str(payload.get("executor_status") or "-"))
        self.executor_detail_value_label.setText(str(payload.get("executor_message") or "-"))
        self.notifier_status_value_label.setText(str(payload.get("notifier_status") or "-"))
        self.scan_status_value_label.setText(str(payload.get("scan_status") or "-"))
        self.pinned_cli_value_label.setText(self._format_value(payload.get("pinned_cli")))
        self.executor_profile_value_label.setText(str(payload.get("pinned_executor_profile") or "-"))
        self.readiness_value_label.setText(self._format_readiness(payload.get("component_readiness")))
        self.scan_detail_value_label.setText(
            self._format_scan_details(
                scan_error=payload.get("scan_error"),
                initialized_at=payload.get("initialized_at"),
                last_scan_at=payload.get("last_scan_at"),
            )
        )
        self.snapshot_summary_value_label.setText(self._format_value(payload.get("last_monitor_snapshot")))
        self.decision_summary_value_label.setText(self._format_value(payload.get("last_analyzer_decision")))
        self.action_summary_value_label.setText(self._format_value(payload.get("last_action")))
        self.pending_value_label.setText(
            self._format_pending(
                ask_user=payload.get("pending_ask_user"),
                approvals=payload.get("pending_approvals"),
            )
        )
        self.mute_state_value_label.setText(self._format_mute_state(payload.get("mute_state")))
        self.recent_incidents_value_label.setText(self._format_list(payload.get("recent_incidents")))
        self.recent_actions_value_label.setText(self._format_list(payload.get("recent_admin_actions")))
        self.approved_overrides_value_label.setText(self._format_list(payload.get("approved_overrides")))
        self.skill_installs_value_label.setText(self._format_pending_skill_installs(payload.get("pending_skill_installs")))
        self._render_skill_install_selector(payload.get("pending_skill_installs"))

        busy_3sig = bool(
            payload.get("busy")
            or payload.get("run_lock_locked")
            or payload.get("tick_active")
        )
        mode_tasks_running = bool(payload.get("mode_tasks_running"))
        flags_text = (
            f"Busy 3sig: {'yes' if busy_3sig else 'no'} | "
            f"Mode tasks: {'running' if mode_tasks_running else 'idle'}"
        )
        self.runtime_flags_value_label.setText(flags_text)
        self._render_current_state()
        self._render_skill_install_action_state()

    def _on_facade_notification(self, note: Any) -> None:
        event = str(getattr(note, "event", "") or "")
        payload = getattr(note, "payload", {}) if note is not None else {}
        if not isinstance(payload, dict):
            return
        event_chat_id = payload.get("chat_id")
        if event_chat_id is not None:
            try:
                if str(event_chat_id) != self.actor_id:
                    return
            except Exception:
                return

        session_uid = str(payload.get("session_uid") or payload.get("session_id") or "").strip()
        refresh_events = {
            "task:started",
            "task:completed",
            "task:cancelled",
            "task:failed",
            "task:updated",
            "ui:mode_changed",
            "ui:session_updated",
        }
        if event not in refresh_events:
            return
        if session_uid and self._active_session_uid and session_uid != self._active_session_uid:
            return
        if event in {"ui:mode_changed", "ui:session_updated"}:
            self.refresh_sessions()
            return
        self.refresh_status_payload()

    def _is_admin_enabled_for_session(self, session_uid: Optional[str]) -> bool:
        if not session_uid:
            return False
        session = self._sessions_by_uid.get(str(session_uid))
        if session is not None and getattr(session, "admin_enabled", None) is not None:
            return bool(getattr(session, "admin_enabled"))

        state = self._read_admin_session_state(str(session_uid))
        if isinstance(state, dict):
            return bool(state.get("enabled", False))
        return False

    def _read_admin_session_state(self, session_uid: str) -> Optional[dict[str, Any]]:
        state_path = self._resolve_state_path()
        if not state_path:
            return None
        try:
            state_store_cls = _load_admin_state_store_class()
            if state_store_cls is None:
                return None
            store = state_store_cls(str(state_path))
            session_id = self._state_store_session_id(session_uid)
            chat_id = self._state_store_chat_id()
            state = store.get_session_state(str(session_id), chat_id=chat_id)
            if not state and str(session_id) != str(session_uid):
                state = store.get_session_state(str(session_uid), chat_id=chat_id)
            return dict(state or {}) if isinstance(state, dict) else None
        except Exception:
            self.logger.exception("failed to load admin session state session_uid=%s", session_uid)
            return None

    def _state_store_session_id(self, session_uid: str) -> str:
        session = self._sessions_by_uid.get(str(session_uid))
        session_id = str(getattr(session, "id", "") or "").strip() if session is not None else ""
        return session_id or str(session_uid or "").strip()

    def _state_store_chat_id(self) -> Optional[int]:
        try:
            return int(self.actor_id)
        except Exception:
            return None

    def _resolve_state_path(self) -> Optional[str]:
        config = getattr(self.facade, "config", None)
        defaults = getattr(config, "defaults", None)
        try:
            return normalize_optional_state_path(getattr(defaults, "state_path", None))
        except TypeError:
            return None

    def _emit_enable_requested(self) -> None:
        if self._active_session_uid:
            self.enableRequested.emit(self._active_session_uid)

    def _emit_disable_requested(self) -> None:
        if self._active_session_uid:
            self.disableRequested.emit(self._active_session_uid)

    def _emit_rescan_requested(self) -> None:
        if self._active_session_uid:
            self.rescanRequested.emit(self._active_session_uid)

    @staticmethod
    def _format_value(value: Any) -> str:
        if value in (None, "", [], {}):
            return "-"
        if isinstance(value, Mapping):
            parts: list[str] = []
            for key, raw in value.items():
                token = str(raw or "").strip() if not isinstance(raw, (dict, list, tuple)) else ""
                if token:
                    parts.append(f"{key}={token}")
                else:
                    parts.append(f"{key}=…")
            return ", ".join(parts) if parts else "-"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value[:5]) or "-"
        return str(value)

    @staticmethod
    def _format_list(value: Any) -> str:
        if not isinstance(value, list) or not value:
            return "0"
        first = value[0]
        if not isinstance(first, Mapping):
            return str(len(value))
        ident = str(
            first.get("incident_id")
            or first.get("action_id")
            or first.get("override_id")
            or first.get("id")
            or ""
        ).strip()
        payload = first.get("payload") if isinstance(first.get("payload"), Mapping) else {}
        decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
        label = str(
            decision.get("incident_type")
            or decision.get("diagnosis")
            or decision.get("action")
            or first.get("title")
            or first.get("action")
            or payload.get("event")
            or ""
        ).strip()
        parts = [str(len(value))]
        if ident:
            parts.append(AdminPanel._compact_text(ident, max_len=42))
        if label:
            parts.append(AdminPanel._compact_text(label))
        return " | ".join(parts)

    @staticmethod
    def _count_value(value: Any) -> int:
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            return 0
        return max(0, num)

    @staticmethod
    def _compact_text(value: Any, *, max_len: int = 56) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    @staticmethod
    def _format_readiness(value: Any) -> str:
        if not isinstance(value, Mapping):
            return "-"
        parts = []
        for key in ("monitor", "analyzer", "executor", "notifier"):
            parts.append(f"{key}={'yes' if bool(value.get(key)) else 'no'}")
        return " | ".join(parts)

    @staticmethod
    def _format_scan_details(*, scan_error: Any, initialized_at: Any, last_scan_at: Any) -> str:
        parts: list[str] = []
        if initialized_at not in (None, ""):
            parts.append(f"initialized_at={initialized_at}")
        if last_scan_at not in (None, ""):
            parts.append(f"last_scan_at={last_scan_at}")
        if scan_error not in (None, ""):
            parts.append(f"scan_error={scan_error}")
        return " | ".join(parts) if parts else "-"

    @staticmethod
    def _format_pending(*, ask_user: Any, approvals: Any) -> str:
        ask_payload = ask_user if isinstance(ask_user, Mapping) else {}
        approvals_payload = approvals if isinstance(approvals, Mapping) else {}
        ask_count = AdminPanel._count_value(ask_payload.get("count"))
        approval_count = AdminPanel._count_value(approvals_payload.get("count"))
        active = bool(ask_payload.get("active") or approvals_payload.get("active"))
        if not active and ask_count == 0 and approval_count == 0:
            return "none"
        suffix = " | active" if active else ""
        return f"ask_user {ask_count} | approvals {approval_count}{suffix}"

    @staticmethod
    def _format_mute_state(value: Any) -> str:
        payload = value if isinstance(value, Mapping) else {}
        if not bool(payload.get("muted")):
            return "off"
        muted_until = payload.get("muted_until_ts")
        if muted_until in (None, ""):
            return "muted"
        return f"muted until {muted_until}"

    @staticmethod
    def _format_pending_skill_installs(value: Any) -> str:
        if not isinstance(value, Mapping):
            return "0 pending"
        count = AdminPanel._count_value(value.get("count"))
        items = value.get("items") if isinstance(value.get("items"), list) else []
        parts = [f"{count} pending"]
        if items:
            parts.append(f"{len(items)} listed")
        if bool(value.get("active")):
            parts.append("active")
        return " | ".join(parts)

    def _render_skill_install_selector(self, payload: Any) -> None:
        summary = dict(payload or {}) if isinstance(payload, Mapping) else {}
        items = summary.get("items") if isinstance(summary.get("items"), list) else []
        selected_before = str(self.skill_approval_selector.currentData() or "").strip()
        self.skill_approval_selector.blockSignals(True)
        self.skill_approval_selector.clear()
        self.skill_approval_selector.addItem("Выберите pending skill install", "")
        for item in items:
            if not isinstance(item, Mapping):
                continue
            approval_id = str(item.get("approval_id") or "").strip()
            if not approval_id:
                continue
            skill_id = str(item.get("skill_id") or "").strip() or "skill"
            mode_phase = "/".join(
                [
                    token
                    for token in (
                        str(item.get("mode_id") or "").strip(),
                        str(item.get("phase") or "").strip(),
                    )
                    if token
                ]
            )
            label_parts = [approval_id, skill_id]
            if mode_phase:
                label_parts.append(mode_phase)
            self.skill_approval_selector.addItem(" | ".join(label_parts), approval_id)
        if selected_before:
            index = self.skill_approval_selector.findData(selected_before)
            if index >= 0:
                self.skill_approval_selector.setCurrentIndex(index)
            elif self.skill_approval_selector.count() > 1:
                self.skill_approval_selector.setCurrentIndex(1)
        else:
            self.skill_approval_selector.setCurrentIndex(1 if self.skill_approval_selector.count() > 1 else 0)
        self.skill_approval_selector.blockSignals(False)

    def _selected_skill_approval_id(self) -> str:
        return str(self.skill_approval_selector.currentData() or "").strip()

    def _render_skill_install_action_state(self) -> None:
        approval_id = self._selected_skill_approval_id()
        blocked = bool(self._pending_skill_install_actions)
        enabled = bool(self._active_session_uid and approval_id and not blocked)
        self.skill_approval_selector.setEnabled(bool(self._active_session_uid and self.skill_approval_selector.count() > 1 and not blocked))
        self.skill_approve_button.setEnabled(enabled)
        self.skill_reject_button.setEnabled(enabled)

    def _schedule_async(self, coro_factory) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            ensure_async(coro_factory(), parent=self)
            return
        asyncio.run(coro_factory())

    def _trigger_skill_install_approve(self) -> None:
        approval_id = self._selected_skill_approval_id()
        session_uid = self._active_session_uid
        if not session_uid or not approval_id:
            return
        self._pending_skill_install_actions.add(approval_id)
        self._render_skill_install_action_state()

        async def _run() -> None:
            try:
                result = await self.facade.approve_pending_skill_install(session_uid, approval_id=approval_id)
                message = str(result.get("message") or "").strip()
                if message:
                    self.skill_action_result_label.setText(message)
                    self.skill_action_result_label.show()
            except Exception:
                self.logger.exception(
                    "desktop admin panel failed to approve pending skill install session_uid=%s approval_id=%s",
                    session_uid,
                    approval_id,
                )
            finally:
                self._pending_skill_install_actions.discard(approval_id)
                self.refresh_status_payload()

        self._schedule_async(_run)

    def _trigger_skill_install_reject(self) -> None:
        approval_id = self._selected_skill_approval_id()
        session_uid = self._active_session_uid
        if not session_uid or not approval_id:
            return
        self._pending_skill_install_actions.add(approval_id)
        self._render_skill_install_action_state()

        async def _run() -> None:
            try:
                result = await self.facade.reject_pending_skill_install(session_uid, approval_id=approval_id)
                message = str(result.get("message") or "").strip()
                if message:
                    self.skill_action_result_label.setText(message)
                    self.skill_action_result_label.show()
            except Exception:
                self.logger.exception(
                    "desktop admin panel failed to reject pending skill install session_uid=%s approval_id=%s",
                    session_uid,
                    approval_id,
                )
            finally:
                self._pending_skill_install_actions.discard(approval_id)
                self.refresh_status_payload()

        self._schedule_async(_run)

    def _refresh_admin_runs(self) -> None:
        session_uid = self._active_session_uid
        self.runs_table.setRowCount(0)
        if not session_uid:
            self.run_detail_label.setText("Session не выбрана.")
            return
        loader = getattr(self.facade, "list_admin_runs", None)
        if not callable(loader):
            self.run_detail_label.setText("Artifact store недоступен.")
            return
        try:
            rows = loader(session_uid, limit=20) or []
        except Exception:
            self.logger.exception(
                "desktop admin panel _refresh_admin_runs failed session_uid=%s",
                session_uid,
            )
            self.run_detail_label.setText("Ошибка загрузки runs.")
            return
        if not rows:
            self.run_detail_label.setText("Нет runs для этой сессии.")
            return
        self.runs_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            run_id = str(row.get("run_id") or "-")
            status = str(row.get("status") or "-")
            phase = str(row.get("phase") or "-")
            started = str(row.get("started_at") or "-")
            self.runs_table.setItem(row_index, 0, QTableWidgetItem(run_id))
            self.runs_table.setItem(row_index, 1, QTableWidgetItem(status))
            self.runs_table.setItem(row_index, 2, QTableWidgetItem(phase))
            self.runs_table.setItem(row_index, 3, QTableWidgetItem(started))
        self.run_detail_label.setText(f"Загружено runs: {len(rows)}")

    def _view_admin_run_detail(self) -> None:
        session_uid = self._active_session_uid
        if not session_uid:
            self.run_detail_label.setText("Session не выбрана.")
            return
        selected_row = self.runs_table.currentRow()
        if selected_row < 0:
            self.run_detail_label.setText("Выберите строку в таблице runs.")
            return
        item = self.runs_table.item(selected_row, 0)
        run_id = str(item.text() if item else "").strip()
        if not run_id:
            self.run_detail_label.setText("run_id не определён.")
            return
        loader = getattr(self.facade, "get_admin_run_detail", None)
        if not callable(loader):
            self.run_detail_label.setText("Artifact store недоступен.")
            return
        try:
            detail = loader(session_uid, run_id=run_id, events_limit=50)
        except Exception:
            self.logger.exception(
                "desktop admin panel _view_admin_run_detail failed session_uid=%s run_id=%s",
                session_uid,
                run_id,
            )
            self.run_detail_label.setText("Ошибка загрузки деталей run.")
            return
        if not detail:
            self.run_detail_label.setText(f"Run {run_id} не найден.")
            return
        lines: list[str] = []
        state = dict(detail.get("state") or {})
        lines.append(f"run_id: {detail.get('run_id', '-')}")
        lines.append(f"status: {state.get('status', '-')}")
        lines.append(f"phase: {state.get('phase', '-')}")
        lines.append(f"started_at: {state.get('started_at', '-')}")
        lines.append(f"finished_at: {state.get('finished_at', '-')}")
        lines.append("")
        lines.append("--- events (tail) ---")
        for event in list(detail.get("events") or [])[-20:]:
            if not isinstance(event, Mapping):
                continue
            event_type = str(event.get("event_type") or event.get("type") or "-")
            event_status = str(event.get("status") or "-")
            event_ts = str(event.get("ts") or event.get("timestamp") or "-")
            lines.append(f"[{event_ts}] {event_type} status={event_status}")
        self.run_detail_label.setText(f"Run {run_id}")
        self.run_detail_view.setPlainText("\n".join(lines))

    def _reload_admin_config(self) -> None:
        session_uid = self._active_session_uid
        if not session_uid:
            self.config_status_label.setText("Session не выбрана.")
            return
        loader = getattr(self.facade, "get_admin_config_yaml", None)
        if not callable(loader):
            self.config_status_label.setText("Config API недоступен.")
            return
        try:
            result = loader(session_uid)
        except Exception:
            self.logger.exception(
                "desktop admin panel _reload_admin_config failed session_uid=%s",
                session_uid,
            )
            self.config_status_label.setText("Ошибка загрузки config.")
            return
        if not isinstance(result, dict):
            self.config_status_label.setText("Config недоступен.")
            return
        self.config_editor.setPlainText(str(result.get("yaml") or ""))
        self.config_status_label.setText(f"Loaded: {result.get('config_path') or '-'}")

    def _refresh_admin_hosts_cache(self, session_uid: str) -> list[dict]:
        loader = getattr(self.facade, "get_admin_hosts", None)
        hosts: list[dict] = []
        if callable(loader):
            try:
                hosts = list(loader(session_uid) or [])
            except Exception:
                self.logger.exception(
                    "desktop admin panel _refresh_admin_hosts_cache failed session_uid=%s",
                    session_uid,
                )
                hosts = []
        if not hosts:
            hosts = [{"alias": "local", "target": "local"}]
        self._admin_hosts_cache = hosts
        return hosts

    def _render_monitor_server_row(self, row_idx: int, server: Mapping[str, Any]) -> None:
        table = self.monitor_servers_table
        combo = QComboBox()
        hosts = self._admin_hosts_cache or [{"alias": "local", "target": "local"}]
        selected_alias = str(server.get("id") or "")
        found = False
        for host in hosts:
            alias = str(host.get("alias") or "")
            target = str(host.get("target") or "local")
            combo.addItem(f"{alias} ({target})", alias)
            if alias == selected_alias:
                combo.setCurrentIndex(combo.count() - 1)
                found = True
        if selected_alias and not found:
            combo.addItem(f"{selected_alias} (missing)", selected_alias)
            combo.setCurrentIndex(combo.count() - 1)
        table.setCellWidget(row_idx, 0, combo)

        action_edit = QLineEdit()
        action_edit.setText(str(server.get("action_id") or ""))
        table.setCellWidget(row_idx, 1, action_edit)

        timeout_edit = QLineEdit()
        timeout_raw = server.get("timeout_sec")
        if timeout_raw not in (None, ""):
            timeout_edit.setText(str(timeout_raw))
        timeout_edit.setPlaceholderText("необязательно")
        table.setCellWidget(row_idx, 2, timeout_edit)

        del_button = QPushButton("Удалить")
        del_button.clicked.connect(lambda _checked=False, r=row_idx: self._remove_monitor_server_row(r))
        table.setCellWidget(row_idx, 3, del_button)

    def _render_monitor_servers_table(self, servers: list[dict]) -> None:
        table = self.monitor_servers_table
        table.setRowCount(0)
        for server in servers:
            row_idx = table.rowCount()
            table.insertRow(row_idx)
            self._render_monitor_server_row(row_idx, server)

    def _collect_monitor_servers_from_ui(self) -> list[dict]:
        table = self.monitor_servers_table
        rows: list[dict] = []
        for row_idx in range(table.rowCount()):
            combo = table.cellWidget(row_idx, 0)
            action_edit = table.cellWidget(row_idx, 1)
            timeout_edit = table.cellWidget(row_idx, 2)
            alias = str(combo.currentData() if isinstance(combo, QComboBox) else "")
            action_id = str(action_edit.text() if isinstance(action_edit, QLineEdit) else "").strip()
            timeout_raw = str(timeout_edit.text() if isinstance(timeout_edit, QLineEdit) else "").strip()
            if not alias or not action_id:
                continue
            target = "local"
            for host in self._admin_hosts_cache or []:
                if str(host.get("alias") or "") == alias:
                    target = str(host.get("target") or "local")
                    break
            row: dict = {"id": alias, "target": target, "action_id": action_id}
            if timeout_raw:
                try:
                    row["timeout_sec"] = float(timeout_raw)
                except ValueError:
                    continue
            rows.append(row)
        return rows

    def _remove_monitor_server_row(self, row_idx: int) -> None:
        rows = self._collect_monitor_servers_from_ui()
        if 0 <= row_idx < len(rows):
            rows.pop(row_idx)
        self._render_monitor_servers_table(rows)

    def _reload_admin_monitor_servers(self) -> None:
        session_uid = self._active_session_uid
        if not session_uid:
            self.monitor_servers_status_label.setText("Session не выбрана.")
            return
        loader = getattr(self.facade, "get_admin_monitor_servers", None)
        if not callable(loader):
            self.monitor_servers_status_label.setText("Monitor API недоступен.")
            return
        self._refresh_admin_hosts_cache(session_uid)
        try:
            result = loader(session_uid)
        except Exception:
            self.logger.exception(
                "desktop admin panel _reload_admin_monitor_servers failed session_uid=%s",
                session_uid,
            )
            self.monitor_servers_status_label.setText("Ошибка загрузки.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            error = str(result.get("error") or "unknown") if isinstance(result, dict) else "unknown"
            self.monitor_servers_status_label.setText(f"Ошибка: {error}")
            return
        servers = list(result.get("servers") or [])
        self._render_monitor_servers_table(servers)
        self.monitor_enabled_checkbox.setChecked(bool(result.get("enabled")))
        interval_raw = result.get("interval_sec")
        try:
            self.monitor_interval_spin.setValue(float(interval_raw) if interval_raw not in (None, "") else 30.0)
        except (TypeError, ValueError):
            self.monitor_interval_spin.setValue(30.0)
        self.monitor_servers_status_label.setText("")

    def _add_admin_monitor_server_row(self) -> None:
        current_rows = self._collect_monitor_servers_from_ui()
        default_alias = "local"
        if self._admin_hosts_cache:
            default_alias = str(self._admin_hosts_cache[0].get("alias") or "local")
        current_rows.append({
            "id": default_alias,
            "target": "local",
            "action_id": "",
        })
        self._render_monitor_servers_table(current_rows)

    def _save_admin_monitor_servers(self) -> None:
        session_uid = self._active_session_uid
        if not session_uid:
            self.monitor_servers_status_label.setText("Session не выбрана.")
            return
        saver = getattr(self.facade, "save_admin_monitor_servers", None)
        if not callable(saver):
            self.monitor_servers_status_label.setText("Monitor API недоступен.")
            return
        servers = self._collect_monitor_servers_from_ui()
        try:
            result = saver(
                session_uid,
                servers=servers,
                enabled=self.monitor_enabled_checkbox.isChecked(),
                interval_sec=float(self.monitor_interval_spin.value()),
            )
        except Exception:
            self.logger.exception(
                "desktop admin panel _save_admin_monitor_servers failed session_uid=%s",
                session_uid,
            )
            self.monitor_servers_status_label.setText("Ошибка сохранения.")
            return
        if isinstance(result, dict) and result.get("ok"):
            self.monitor_servers_status_label.setText("Сохранено.")
        else:
            error = "unknown"
            if isinstance(result, dict):
                error = str(result.get("error") or "unknown")
            self.monitor_servers_status_label.setText(f"Ошибка: {error}")

    def _render_ssh_action_row(self, row_idx: int, action: Mapping[str, Any]) -> None:
        table = self.ssh_actions_table
        id_edit = QLineEdit()
        id_edit.setText(str(action.get("action_id") or ""))
        table.setCellWidget(row_idx, 0, id_edit)

        argv_edit = QPlainTextEdit()
        argv_edit.setMaximumHeight(90)
        argv = action.get("argv") or []
        argv_text = "\n".join(str(x) for x in argv)
        argv_edit.setPlainText(argv_text)
        table.setCellWidget(row_idx, 1, argv_edit)

        timeout_edit = QLineEdit()
        timeout_raw = action.get("timeout_sec")
        timeout_edit.setText(str(timeout_raw) if timeout_raw not in (None, "") else "30")
        table.setCellWidget(row_idx, 2, timeout_edit)

        risk_combo = QComboBox()
        for level in ("low", "medium", "high"):
            risk_combo.addItem(level, level)
        risk_level = str(action.get("risk_level") or "low")
        idx = risk_combo.findData(risk_level)
        if idx >= 0:
            risk_combo.setCurrentIndex(idx)
        table.setCellWidget(row_idx, 3, risk_combo)

        desc_edit = QLineEdit()
        desc_edit.setText(str(action.get("description") or ""))
        table.setCellWidget(row_idx, 4, desc_edit)

        del_button = QPushButton("Удалить")
        del_button.clicked.connect(lambda _checked=False, r=row_idx: self._remove_ssh_action_row(r))
        table.setCellWidget(row_idx, 5, del_button)

    def _render_ssh_actions_table(self, actions: list[dict]) -> None:
        table = self.ssh_actions_table
        table.setRowCount(0)
        for action in actions:
            row_idx = table.rowCount()
            table.insertRow(row_idx)
            self._render_ssh_action_row(row_idx, action)

    def _collect_ssh_actions_from_ui(self) -> list[dict]:
        table = self.ssh_actions_table
        rows: list[dict] = []
        for row_idx in range(table.rowCount()):
            id_edit = table.cellWidget(row_idx, 0)
            argv_edit = table.cellWidget(row_idx, 1)
            timeout_edit = table.cellWidget(row_idx, 2)
            risk_combo = table.cellWidget(row_idx, 3)
            desc_edit = table.cellWidget(row_idx, 4)
            action_id = str(id_edit.text() if isinstance(id_edit, QLineEdit) else "").strip()
            argv_raw = str(argv_edit.toPlainText() if isinstance(argv_edit, QPlainTextEdit) else "").strip()
            if not action_id or not argv_raw:
                continue
            argv = [line.strip() for line in argv_raw.splitlines() if line.strip()]
            if not argv:
                continue
            timeout_raw = str(timeout_edit.text() if isinstance(timeout_edit, QLineEdit) else "").strip()
            try:
                timeout_sec = float(timeout_raw) if timeout_raw else 30.0
            except ValueError:
                continue
            risk = str(risk_combo.currentData() if isinstance(risk_combo, QComboBox) else "low")
            description = str(desc_edit.text() if isinstance(desc_edit, QLineEdit) else "").strip()
            row: dict = {
                "action_id": action_id,
                "argv": argv,
                "timeout_sec": timeout_sec,
                "risk_level": risk,
            }
            if description:
                row["description"] = description
            rows.append(row)
        return rows

    def _remove_ssh_action_row(self, row_idx: int) -> None:
        rows = self._collect_ssh_actions_from_ui()
        if 0 <= row_idx < len(rows):
            rows.pop(row_idx)
        self._render_ssh_actions_table(rows)

    def _reload_admin_ssh_actions(self) -> None:
        session_uid = self._active_session_uid
        if not session_uid:
            self.ssh_actions_status_label.setText("Session не выбрана.")
            return
        loader = getattr(self.facade, "get_admin_actions_ssh", None)
        if not callable(loader):
            self.ssh_actions_status_label.setText("SSH actions API недоступен.")
            return
        try:
            result = loader(session_uid)
        except Exception:
            self.logger.exception(
                "desktop admin panel _reload_admin_ssh_actions failed session_uid=%s",
                session_uid,
            )
            self.ssh_actions_status_label.setText("Ошибка загрузки.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            error = str(result.get("error") or "unknown") if isinstance(result, dict) else "unknown"
            self.ssh_actions_status_label.setText(f"Ошибка: {error}")
            return
        actions = list(result.get("actions") or [])
        self._render_ssh_actions_table(actions)
        self.ssh_actions_status_label.setText("")

    def _add_admin_ssh_action_row(self) -> None:
        current_rows = self._collect_ssh_actions_from_ui()
        current_rows.append({
            "action_id": "",
            "argv": [],
            "timeout_sec": 30.0,
            "risk_level": "low",
            "description": "",
        })
        self._render_ssh_actions_table(current_rows)

    def _save_admin_ssh_actions(self) -> None:
        session_uid = self._active_session_uid
        if not session_uid:
            self.ssh_actions_status_label.setText("Session не выбрана.")
            return
        saver = getattr(self.facade, "save_admin_actions_ssh", None)
        if not callable(saver):
            self.ssh_actions_status_label.setText("SSH actions API недоступен.")
            return
        actions = self._collect_ssh_actions_from_ui()
        try:
            result = saver(session_uid, actions=actions)
        except Exception:
            self.logger.exception(
                "desktop admin panel _save_admin_ssh_actions failed session_uid=%s",
                session_uid,
            )
            self.ssh_actions_status_label.setText("Ошибка сохранения.")
            return
        if isinstance(result, dict) and result.get("ok"):
            self.ssh_actions_status_label.setText("Сохранено.")
        else:
            error = "unknown"
            if isinstance(result, dict):
                error = str(result.get("error") or "unknown")
            self.ssh_actions_status_label.setText(f"Ошибка: {error}")

    def closeEvent(self, event: Any) -> None:
        unsubscribe = getattr(self, "_unsubscribe", None)
        if callable(unsubscribe):
            unsubscribe()
            self._unsubscribe = None
        timer = getattr(self, "_status_refresh_timer", None)
        if timer is not None:
            timer.stop()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Admin Autonomy section (inventory / baseline / drift / memory / runbooks)
# ---------------------------------------------------------------------------


_SEVERITY_EMOJI = {
    "alarm": "🔴",
    "warn": "🟡",
    "info": "🔵",
    "noise": "⚪",
}

_STATUS_COLORS = {
    "alarm": QColor(255, 200, 200),
    "warn": QColor(255, 230, 180),
    "proposed_baseline": QColor(210, 225, 255),
    "no_baseline": QColor(230, 230, 230),
    "ok": QColor(220, 245, 220),
}


def _dump_yaml_safe(value: Any) -> str:
    try:
        import yaml as _yaml
        return _yaml.safe_dump(
            value or {}, allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
    except Exception:  # pragma: no cover - defensive
        return str(value)


class AdminAutonomyPanel(QWidget):
    """Отдельная секция: inventory, baseline, drift, memory, runbooks."""

    def __init__(
        self,
        facade: "ApplicationFacade",
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.facade = facade
        self.logger = logging.getLogger(__name__ + ".AdminAutonomyPanel")
        self._session_uid: Optional[str] = None
        self._servers: list[dict[str, Any]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        group = QGroupBox("Автономия")
        group.setObjectName("admin_autonomy_group")
        root = QVBoxLayout(group)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.summary_label = QLabel("Сервера не загружены.")
        self.summary_label.setObjectName("admin_autonomy_summary_label")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        self.refresh_button = QPushButton("Обновить список")
        self.refresh_button.setObjectName("admin_autonomy_refresh_button")
        self.refresh_button.clicked.connect(self.refresh_servers)
        controls.addWidget(self.refresh_button)

        self.rescan_button = QPushButton("Rescan выбранный")
        self.rescan_button.setObjectName("admin_autonomy_rescan_button")
        self.rescan_button.clicked.connect(self._rescan_selected)
        controls.addWidget(self.rescan_button)

        self.rescan_all_button = QPushButton("Rescan all")
        self.rescan_all_button.setObjectName("admin_autonomy_rescan_all_button")
        self.rescan_all_button.clicked.connect(self._rescan_all)
        controls.addWidget(self.rescan_all_button)

        self.maintenance_button = QPushButton("Daily maintenance")
        self.maintenance_button.setObjectName("admin_autonomy_maintenance_button")
        self.maintenance_button.clicked.connect(self._run_daily_maintenance)
        controls.addWidget(self.maintenance_button)

        self.details_button = QPushButton("Открыть детали")
        self.details_button.setObjectName("admin_autonomy_details_button")
        self.details_button.clicked.connect(self._open_details)
        controls.addWidget(self.details_button)
        controls.addStretch(1)
        root.addLayout(controls)

        headers = [
            "Server ID", "Label", "Transport", "Host", "Status",
            "Baseline", "Alarm", "Warn", "Memory", "Last Scan",
        ]
        self.servers_table = QTableWidget(0, len(headers))
        self.servers_table.setObjectName("admin_autonomy_servers_table")
        self.servers_table.setHorizontalHeaderLabels(headers)
        self.servers_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.servers_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.servers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.servers_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.servers_table.horizontalHeader().setStretchLastSection(True)
        self.servers_table.setMinimumHeight(160)
        self.servers_table.doubleClicked.connect(lambda *_: self._open_details())
        root.addWidget(self.servers_table)

        outer.addWidget(group)

    # ------------------------------------------------------------------

    def set_session(self, session_uid: Optional[str]) -> None:
        self._session_uid = str(session_uid or "").strip() or None
        self.refresh_servers()

    def refresh_servers(self) -> None:
        self.servers_table.setRowCount(0)
        self._servers = []
        uid = self._session_uid
        if not uid:
            self.summary_label.setText("Session не выбрана.")
            return
        loader = getattr(self.facade, "admin_autonomy_list_servers", None)
        if not callable(loader):
            self.summary_label.setText("Autonomy API недоступен.")
            return
        try:
            servers = list(loader(uid) or [])
        except Exception:
            self.logger.exception("autonomy refresh_servers failed session_uid=%s", uid)
            self.summary_label.setText("Ошибка загрузки inventory.")
            return
        self._servers = servers

        summary_loader = getattr(self.facade, "admin_autonomy_global_summary", None)
        gs: dict[str, Any] = {}
        if callable(summary_loader):
            try:
                gs = dict(summary_loader(uid) or {})
            except Exception:
                self.logger.exception("autonomy global_summary failed session_uid=%s", uid)
        totals = dict(gs.get("totals") or {})
        statuses = dict(gs.get("statuses") or {})
        self.summary_label.setText(
            "Сервера: {count} | статусы: {statuses} | drifts: {totals}".format(
                count=gs.get("server_count", len(servers)),
                statuses=", ".join(f"{k}={v}" for k, v in statuses.items()) or "-",
                totals=", ".join(f"{k}={v}" for k, v in totals.items()) or "-",
            )
        )

        self.servers_table.setRowCount(len(servers))
        for row, data in enumerate(servers):
            drifts = dict(data.get("open_drifts") or {})
            status = str(data.get("status") or "-")
            cells = [
                str(data.get("server_id") or "-"),
                str(data.get("label") or "-"),
                str(data.get("transport") or "-"),
                str(data.get("host") or "-"),
                status,
                "yes" if data.get("baseline_present") else (
                    "proposed" if data.get("has_proposed_baseline") else "no"
                ),
                str(drifts.get("alarm", 0)),
                str(drifts.get("warn", 0)),
                str(data.get("memory_entries") or 0),
                str(data.get("last_scan_ts") or "-"),
            ]
            color = _STATUS_COLORS.get(status)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if color is not None and col == 4:
                    item.setBackground(color)
                self.servers_table.setItem(row, col, item)

    def _selected_server_id(self) -> Optional[str]:
        row = self.servers_table.currentRow()
        if row < 0 or row >= len(self._servers):
            return None
        sid = str(self._servers[row].get("server_id") or "").strip()
        return sid or None

    def _rescan_selected(self) -> None:
        uid = self._session_uid
        sid = self._selected_server_id()
        if not uid or not sid:
            QMessageBox.information(self, "Rescan", "Выберите сервер.")
            return
        fn = getattr(self.facade, "admin_autonomy_rescan_server", None)
        if not callable(fn):
            QMessageBox.warning(self, "Rescan", "API недоступен.")
            return
        self.rescan_button.setEnabled(False)
        try:
            result = fn(uid, sid)
        except Exception:
            self.logger.exception("autonomy rescan failed session_uid=%s server_id=%s", uid, sid)
            QMessageBox.critical(self, "Rescan", "Ошибка rescan.")
            self.rescan_button.setEnabled(True)
            return
        self.rescan_button.setEnabled(True)
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            QMessageBox.warning(self, "Rescan", f"Ошибка: {err}")
            self.refresh_servers()
            return
        QMessageBox.information(
            self,
            "Rescan",
            (
                f"Rescan {sid}: drifts_written={result.get('drifts_written', 0)}, "
                f"alarm={result.get('alarm_count', 0)}, warn={result.get('warn_count', 0)}"
            ),
        )
        self.refresh_servers()

    def _rescan_all(self) -> None:
        uid = self._session_uid
        if not uid:
            QMessageBox.information(self, "Rescan all", "Session не выбрана.")
            return
        fn = getattr(self.facade, "admin_autonomy_rescan_all", None)
        if not callable(fn):
            QMessageBox.warning(self, "Rescan all", "API недоступен.")
            return
        self.rescan_all_button.setEnabled(False)
        try:
            result = fn(uid)
        except Exception:
            self.logger.exception("autonomy rescan_all failed session_uid=%s", uid)
            QMessageBox.critical(self, "Rescan all", "Ошибка rescan_all.")
            self.rescan_all_button.setEnabled(True)
            return
        self.rescan_all_button.setEnabled(True)
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            QMessageBox.warning(self, "Rescan all", f"Ошибка: {err}")
            self.refresh_servers()
            return
        servers = list(result.get("servers") or [])
        total_drifts = sum(int(s.get("drifts_written", 0) or 0) for s in servers)
        total_alarm = sum(int(s.get("alarm_count", 0) or 0) for s in servers)
        QMessageBox.information(
            self,
            "Rescan all",
            f"Серверов: {len(servers)}, drifts: {total_drifts}, alarm: {total_alarm}",
        )
        self.refresh_servers()

    def _run_daily_maintenance(self) -> None:
        uid = self._session_uid
        if not uid:
            QMessageBox.information(self, "Daily maintenance", "Session не выбрана.")
            return
        fn = getattr(self.facade, "admin_autonomy_run_daily_maintenance", None)
        if not callable(fn):
            QMessageBox.warning(self, "Daily maintenance", "API недоступен.")
            return
        self.maintenance_button.setEnabled(False)
        try:
            result = fn(uid)
        except Exception:
            self.logger.exception("autonomy daily_maintenance failed session_uid=%s", uid)
            QMessageBox.critical(self, "Daily maintenance", "Ошибка maintenance.")
            self.maintenance_button.setEnabled(True)
            return
        self.maintenance_button.setEnabled(True)
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            QMessageBox.warning(self, "Daily maintenance", f"Ошибка: {err}")
            return
        report = dict(result.get("report") or {})
        servers = dict(report.get("servers") or {})
        QMessageBox.information(
            self,
            "Daily maintenance",
            f"Готово. Серверов обработано: {len(servers)}.",
        )
        self.refresh_servers()

    def _open_details(self) -> None:
        uid = self._session_uid
        sid = self._selected_server_id()
        if not uid or not sid:
            QMessageBox.information(self, "Детали", "Выберите сервер.")
            return
        dlg = AdminAutonomyDetailDialog(self.facade, uid, sid, parent=self)
        dlg.exec()
        # refresh inventory after dialog closed (statuses могут измениться)
        self.refresh_servers()


class AdminAutonomyDetailDialog(QDialog):
    """Detail-диалог для одного сервера: overview/baseline/drifts/memory/runbooks."""

    def __init__(
        self,
        facade: "ApplicationFacade",
        session_uid: str,
        server_id: str,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.facade = facade
        self.session_uid = str(session_uid)
        self.server_id = str(server_id)
        self.logger = logging.getLogger(__name__ + ".AdminAutonomyDetailDialog")
        self.setObjectName("admin_autonomy_detail_dialog")
        self.setWindowTitle(f"Автономия — {self.server_id}")
        self.resize(900, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("admin_autonomy_detail_tabs")
        layout.addWidget(self.tabs, 1)

        self.tabs.addTab(self._build_overview_tab(), "Обзор")
        self.tabs.addTab(self._build_baseline_tab(), "Baseline")
        self.tabs.addTab(self._build_drifts_tab(), "Drifts")
        self.tabs.addTab(self._build_memory_tab(), "Memory")
        self.tabs.addTab(self._build_runbooks_tab(), "Runbooks")
        self.tabs.addTab(self._build_builder_tab(), "Builder")
        self.tabs.addTab(self._build_snapshots_tab(), "Snapshots")
        self.tabs.addTab(self._build_prereqs_tab(), "Prereqs")

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._refresh_all()

    # ---------- overview ----------

    def _build_overview_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        self.overview_view = QPlainTextEdit()
        self.overview_view.setObjectName("admin_autonomy_overview_view")
        self.overview_view.setReadOnly(True)
        lay.addWidget(self.overview_view, 1)
        return w

    def _refresh_overview(self) -> None:
        summary_fn = getattr(self.facade, "admin_autonomy_server_summary", None)
        global_fn = getattr(self.facade, "admin_autonomy_global_summary", None)
        lines: list[str] = []
        if callable(summary_fn):
            try:
                summary = summary_fn(self.session_uid, self.server_id) or {}
            except Exception:
                self.logger.exception("overview: summary failed")
                summary = {}
            if summary:
                lines.append("# Server summary")
                lines.append(_dump_yaml_safe(summary))
        if callable(global_fn):
            try:
                gs = global_fn(self.session_uid) or {}
            except Exception:
                self.logger.exception("overview: global_summary failed")
                gs = {}
            if gs:
                lines.append("# Global summary")
                lines.append(_dump_yaml_safe(gs))
        self.overview_view.setPlainText("\n".join(lines).strip() or "Нет данных.")

    # ---------- baseline ----------

    def _build_baseline_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        btn_row = QHBoxLayout()
        self.baseline_accept_btn = QPushButton("Accept proposed")
        self.baseline_accept_btn.clicked.connect(self._accept_baseline)
        btn_row.addWidget(self.baseline_accept_btn)
        self.baseline_discard_btn = QPushButton("Discard proposed")
        self.baseline_discard_btn.clicked.connect(self._discard_baseline)
        btn_row.addWidget(self.baseline_discard_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        lay.addWidget(splitter, 1)

        cur_wrap = QWidget()
        cv = QVBoxLayout(cur_wrap)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(QLabel("Текущий baseline:"))
        self.baseline_current_view = QPlainTextEdit()
        self.baseline_current_view.setReadOnly(True)
        cv.addWidget(self.baseline_current_view, 1)
        splitter.addWidget(cur_wrap)

        prop_wrap = QWidget()
        pv = QVBoxLayout(prop_wrap)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(QLabel("Proposed baseline:"))
        self.baseline_proposed_view = QPlainTextEdit()
        self.baseline_proposed_view.setReadOnly(True)
        pv.addWidget(self.baseline_proposed_view, 1)
        splitter.addWidget(prop_wrap)
        return w

    def _refresh_baseline(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_get_baseline", None)
        data: dict[str, Any] = {}
        if callable(fn):
            try:
                data = fn(self.session_uid, self.server_id) or {}
            except Exception:
                self.logger.exception("baseline: load failed")
        self.baseline_current_view.setPlainText(_dump_yaml_safe(data.get("baseline") or {}))
        proposed = data.get("proposed")
        if proposed is None:
            self.baseline_proposed_view.setPlainText("(нет proposed baseline)")
        else:
            self.baseline_proposed_view.setPlainText(_dump_yaml_safe(proposed))
        has_proposed = bool(data.get("has_proposed"))
        self.baseline_accept_btn.setEnabled(has_proposed)
        self.baseline_discard_btn.setEnabled(has_proposed)

    def _accept_baseline(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_accept_baseline", None)
        if not callable(fn):
            return
        try:
            result = fn(self.session_uid, self.server_id)
        except Exception:
            self.logger.exception("baseline: accept failed")
            QMessageBox.critical(self, "Baseline", "Ошибка accept.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            QMessageBox.warning(self, "Baseline", f"Ошибка: {err}")
        self._refresh_baseline()

    def _discard_baseline(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_discard_baseline", None)
        if not callable(fn):
            return
        try:
            ok = bool(fn(self.session_uid, self.server_id))
        except Exception:
            self.logger.exception("baseline: discard failed")
            QMessageBox.critical(self, "Baseline", "Ошибка discard.")
            return
        if not ok:
            QMessageBox.information(self, "Baseline", "Нечего отменять.")
        self._refresh_baseline()

    # ---------- drifts ----------

    def _build_drifts_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        controls = QHBoxLayout()
        self.drifts_refresh_btn = QPushButton("Обновить")
        self.drifts_refresh_btn.clicked.connect(self._refresh_drifts)
        controls.addWidget(self.drifts_refresh_btn)
        self.drifts_open_only_btn = QPushButton("Показать все")
        self.drifts_open_only_btn.setCheckable(True)
        self.drifts_open_only_btn.toggled.connect(lambda _: self._refresh_drifts())
        controls.addWidget(self.drifts_open_only_btn)
        controls.addWidget(QLabel("Severity ≥"))
        self.drifts_severity_filter = QComboBox()
        self.drifts_severity_filter.addItem("all", "")
        self.drifts_severity_filter.addItem("noise", "noise")
        self.drifts_severity_filter.addItem("info", "info")
        self.drifts_severity_filter.addItem("warn", "warn")
        self.drifts_severity_filter.addItem("alarm", "alarm")
        self.drifts_severity_filter.currentIndexChanged.connect(lambda _: self._refresh_drifts())
        controls.addWidget(self.drifts_severity_filter)
        controls.addStretch(1)
        lay.addLayout(controls)

        headers = ["id", "severity", "check_id", "prev", "new", "details", ""]
        self.drifts_table = QTableWidget(0, len(headers))
        self.drifts_table.setObjectName("admin_autonomy_drifts_table")
        self.drifts_table.setHorizontalHeaderLabels(headers)
        self.drifts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.drifts_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.drifts_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.drifts_table, 1)
        return w

    def _refresh_drifts(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_list_drifts", None)
        self.drifts_table.setRowCount(0)
        if not callable(fn):
            return
        open_only = not self.drifts_open_only_btn.isChecked()
        try:
            rows = list(fn(self.session_uid, self.server_id, open_only=open_only, limit=200) or [])
        except Exception:
            self.logger.exception("drifts: load failed")
            return
        severity_min = str(self.drifts_severity_filter.currentData() or "").strip()
        if severity_min:
            order = {"noise": 0, "info": 1, "warn": 2, "alarm": 3}
            threshold = order.get(severity_min, 0)
            rows = [
                row for row in rows
                if order.get(str(row.get("severity") or "").lower(), 0) >= threshold
            ]
        self.drifts_table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            sev = str(row.get("severity") or "").lower()
            sev_cell = f"{_SEVERITY_EMOJI.get(sev, '')} {sev}".strip()
            drift_id = row.get("id") or row.get("drift_id") or 0
            cells = [
                str(drift_id),
                sev_cell,
                str(row.get("check_id") or "-"),
                str(row.get("prev_value") if row.get("prev_value") is not None else "-"),
                str(row.get("new_value") if row.get("new_value") is not None else "-"),
                str(row.get("details") or row.get("message") or "-"),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                self.drifts_table.setItem(row_idx, col, item)
            ack_btn = QPushButton("Ack")
            acknowledged = bool(row.get("acknowledged_at") or row.get("acknowledged"))
            ack_btn.setEnabled(not acknowledged)

            def _make_cb(did: int):
                def _cb() -> None:
                    self._ack_drift(int(did))
                return _cb

            ack_btn.clicked.connect(_make_cb(int(drift_id) if drift_id else 0))
            self.drifts_table.setCellWidget(row_idx, len(cells), ack_btn)

    def _ack_drift(self, drift_id: int) -> None:
        if drift_id <= 0:
            return
        fn = getattr(self.facade, "admin_autonomy_ack_drift", None)
        if not callable(fn):
            return
        try:
            ok = bool(fn(self.session_uid, self.server_id, drift_id))
        except Exception:
            self.logger.exception("drifts: ack failed")
            QMessageBox.critical(self, "Drifts", "Ошибка ack.")
            return
        if not ok:
            QMessageBox.information(self, "Drifts", "Drift не изменён.")
        self._refresh_drifts()

    # ---------- memory ----------

    def _build_memory_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        lay.addWidget(splitter, 1)

        # Facts column
        facts_wrap = QWidget()
        fv = QVBoxLayout(facts_wrap)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.addWidget(QLabel("Facts:"))
        self.facts_table = QTableWidget(0, 4)
        self.facts_table.setHorizontalHeaderLabels(["key", "value", "edit", "delete"])
        self.facts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.facts_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.facts_table.horizontalHeader().setStretchLastSection(True)
        fv.addWidget(self.facts_table, 1)
        fact_btn_row = QHBoxLayout()
        self.facts_add_btn = QPushButton("Добавить факт")
        self.facts_add_btn.clicked.connect(self._add_fact)
        fact_btn_row.addWidget(self.facts_add_btn)
        self.memory_compact_btn = QPushButton("Compact memory")
        self.memory_compact_btn.clicked.connect(self._compact_memory)
        fact_btn_row.addWidget(self.memory_compact_btn)
        fact_btn_row.addStretch(1)
        fv.addLayout(fact_btn_row)
        splitter.addWidget(facts_wrap)

        # Notes column
        notes_wrap = QWidget()
        nv = QVBoxLayout(notes_wrap)
        nv.setContentsMargins(0, 0, 0, 0)
        nv.addWidget(QLabel("Notes:"))
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        nv.addWidget(self.notes_view, 1)
        note_input_row = QHBoxLayout()
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("Добавить заметку...")
        note_input_row.addWidget(self.note_input, 1)
        self.note_append_btn = QPushButton("Append note")
        self.note_append_btn.clicked.connect(self._append_note)
        note_input_row.addWidget(self.note_append_btn)
        nv.addLayout(note_input_row)
        splitter.addWidget(notes_wrap)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        return w

    def _refresh_memory(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_get_memory", None)
        data: dict[str, Any] = {}
        if callable(fn):
            try:
                data = fn(self.session_uid, self.server_id) or {}
            except Exception:
                self.logger.exception("memory: load failed")
        facts = dict(data.get("facts") or {})
        self.facts_table.setRowCount(len(facts))
        for row_idx, (key, raw_entry) in enumerate(facts.items()):
            if isinstance(raw_entry, Mapping):
                display_value = raw_entry.get("value")
            else:
                display_value = raw_entry
            self.facts_table.setItem(row_idx, 0, QTableWidgetItem(str(key)))
            self.facts_table.setItem(row_idx, 1, QTableWidgetItem(str(display_value)))
            edit_btn = QPushButton("Edit")
            del_btn = QPushButton("Delete")

            def _make_edit(k: str):
                def _cb() -> None:
                    self._edit_fact(k)
                return _cb

            def _make_del(k: str):
                def _cb() -> None:
                    self._delete_fact(k)
                return _cb

            edit_btn.clicked.connect(_make_edit(str(key)))
            del_btn.clicked.connect(_make_del(str(key)))
            self.facts_table.setCellWidget(row_idx, 2, edit_btn)
            self.facts_table.setCellWidget(row_idx, 3, del_btn)
        self.notes_view.setPlainText(str(data.get("notes_text") or ""))

    def _add_fact(self) -> None:
        key, ok = QInputDialog.getText(self, "Новый факт", "Key:")
        if not ok or not str(key).strip():
            return
        value, ok = QInputDialog.getText(self, "Новый факт", f"Value для {key}:")
        if not ok:
            return
        self._update_fact(str(key).strip(), str(value))

    def _edit_fact(self, key: str) -> None:
        value, ok = QInputDialog.getText(self, "Редактировать факт", f"Value для {key}:")
        if not ok:
            return
        self._update_fact(key, str(value))

    def _update_fact(self, key: str, value: str) -> None:
        fn = getattr(self.facade, "admin_autonomy_update_fact", None)
        if not callable(fn):
            return
        try:
            result = fn(self.session_uid, self.server_id, key=key, value=value)
        except Exception:
            self.logger.exception("memory: update_fact failed")
            QMessageBox.critical(self, "Memory", "Ошибка обновления факта.")
            return
        if isinstance(result, dict) and not result.get("ok"):
            QMessageBox.warning(self, "Memory", str(result.get("error") or "Ошибка."))
        self._refresh_memory()

    def _delete_fact(self, key: str) -> None:
        if QMessageBox.question(self, "Удалить факт", f"Удалить {key}?") != QMessageBox.StandardButton.Yes:
            return
        fn = getattr(self.facade, "admin_autonomy_delete_fact", None)
        if not callable(fn):
            return
        try:
            fn(self.session_uid, self.server_id, key)
        except Exception:
            self.logger.exception("memory: delete_fact failed")
            QMessageBox.critical(self, "Memory", "Ошибка удаления факта.")
            return
        self._refresh_memory()

    def _append_note(self) -> None:
        text = self.note_input.text().strip()
        if not text:
            return
        fn = getattr(self.facade, "admin_autonomy_append_note", None)
        if not callable(fn):
            return
        try:
            fn(self.session_uid, self.server_id, text)
        except Exception:
            self.logger.exception("memory: append_note failed")
            QMessageBox.critical(self, "Memory", "Ошибка добавления заметки.")
            return
        self.note_input.clear()
        self._refresh_memory()

    def _compact_memory(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_compact_memory", None)
        if not callable(fn):
            return
        try:
            result = fn(self.session_uid, self.server_id, force=False)
        except Exception:
            self.logger.exception("memory: compact failed")
            QMessageBox.critical(self, "Memory", "Ошибка compact.")
            return
        if isinstance(result, dict) and not result.get("ok"):
            QMessageBox.warning(self, "Memory", str(result.get("error") or "Ошибка."))
        self._refresh_memory()

    # ---------- runbooks ----------

    def _build_runbooks_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        lay.addWidget(splitter, 1)

        self.runbooks_list = QListWidget()
        self.runbooks_list.setObjectName("admin_autonomy_runbooks_list")
        self.runbooks_list.currentItemChanged.connect(self._on_runbook_selected)
        splitter.addWidget(self.runbooks_list)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        rb_actions = QHBoxLayout()
        self.runbook_validate_btn = QPushButton("Validate")
        self.runbook_validate_btn.clicked.connect(self._validate_runbook)
        rb_actions.addWidget(self.runbook_validate_btn)
        self.runbook_promote_btn = QPushButton("Promote…")
        self.runbook_promote_btn.clicked.connect(self._promote_runbook)
        rb_actions.addWidget(self.runbook_promote_btn)
        self.runbook_run_step_btn = QPushButton("Run step…")
        self.runbook_run_step_btn.clicked.connect(self._run_runbook_step)
        rb_actions.addWidget(self.runbook_run_step_btn)
        rb_actions.addStretch(1)
        rv.addLayout(rb_actions)

        self.runbook_body_view = QPlainTextEdit()
        self.runbook_body_view.setReadOnly(True)
        rv.addWidget(self.runbook_body_view, 2)

        rv.addWidget(QLabel("Результат:"))
        self.runbook_result_view = QPlainTextEdit()
        self.runbook_result_view.setReadOnly(True)
        self.runbook_result_view.setMaximumHeight(200)
        rv.addWidget(self.runbook_result_view, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        self._current_runbook: Optional[dict[str, Any]] = None
        self._set_runbook_actions_enabled(False)
        return w

    def _set_runbook_actions_enabled(self, enabled: bool) -> None:
        for btn in (
            getattr(self, "runbook_validate_btn", None),
            getattr(self, "runbook_promote_btn", None),
            getattr(self, "runbook_run_step_btn", None),
        ):
            if btn is not None:
                btn.setEnabled(bool(enabled))

    def _refresh_runbooks(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_list_runbooks", None)
        self.runbooks_list.clear()
        self.runbook_body_view.setPlainText("")
        if not callable(fn):
            return
        try:
            rows = list(fn(self.session_uid, self.server_id) or [])
        except Exception:
            self.logger.exception("runbooks: list failed")
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            rb_id = str(row.get("id") or "").strip()
            if not rb_id:
                continue
            title = str(row.get("title") or rb_id)
            tags = ", ".join(str(t) for t in (row.get("tags") or []))
            label = f"{rb_id} — {title}"
            if tags:
                label += f"  [{tags}]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, rb_id)
            self.runbooks_list.addItem(item)

    def _on_runbook_selected(self, current: Optional[QListWidgetItem], _previous: Any) -> None:
        self._current_runbook = None
        self._set_runbook_actions_enabled(False)
        self.runbook_result_view.setPlainText("")
        if current is None:
            self.runbook_body_view.setPlainText("")
            return
        rb_id = str(current.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not rb_id:
            return
        fn = getattr(self.facade, "admin_autonomy_get_runbook", None)
        if not callable(fn):
            return
        try:
            rb = fn(self.session_uid, rb_id, server_id=self.server_id)
        except Exception:
            self.logger.exception("runbooks: get failed")
            return
        if not rb:
            self.runbook_body_view.setPlainText(f"Runbook {rb_id} не найден.")
            return
        self._current_runbook = dict(rb)
        header = (
            f"# {rb.get('title') or rb_id}\n"
            f"id: {rb.get('id')}\n"
            f"scope: {rb.get('scope')}\n"
            f"tags: {', '.join(rb.get('tags') or [])}\n"
            f"triggers: {', '.join(rb.get('triggers') or [])}\n"
            f"path: {rb.get('path')}\n\n"
        )
        self.runbook_body_view.setPlainText(header + str(rb.get("body") or ""))
        self._set_runbook_actions_enabled(True)

    def _selected_runbook_id(self) -> Optional[str]:
        rb = self._current_runbook
        if not rb:
            return None
        rb_id = str(rb.get("id") or "").strip()
        return rb_id or None

    def _validate_runbook(self) -> None:
        rb_id = self._selected_runbook_id()
        if not rb_id:
            return
        fn = getattr(self.facade, "admin_autonomy_validate_runbook", None)
        if not callable(fn):
            QMessageBox.warning(self, "Validate", "API недоступен.")
            return
        self.runbook_result_view.setPlainText("Validating…")
        try:
            result = fn(self.session_uid, rb_id)
        except Exception:
            self.logger.exception("runbook validate failed")
            self.runbook_result_view.setPlainText("Ошибка validate.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.runbook_result_view.setPlainText(f"Ошибка: {err}")
            return
        report = dict(result.get("report") or {})
        lines: list[str] = []
        lines.append(f"Validation: {'OK' if report.get('ok') else 'FAIL'}")
        for check in report.get("checks") or []:
            lines.append(
                f"  - {check.get('step')}: checksum={check.get('checksum')} "
                f"bash_n={check.get('bash_n')} shellcheck={check.get('shellcheck')}"
            )
        errors = report.get("errors") or []
        warnings = report.get("warnings") or []
        if errors:
            lines.append("Errors:")
            for e in errors:
                lines.append(f"  - {e}")
        if warnings:
            lines.append("Warnings:")
            for w in warnings:
                lines.append(f"  - {w}")
        self.runbook_result_view.setPlainText("\n".join(lines))

    def _promote_runbook(self) -> None:
        rb_id = self._selected_runbook_id()
        if not rb_id:
            return
        dlg = RunbookPromoteDialog(default_server_id=self.server_id, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.result_payload()
        add_servers = payload.get("add_servers") or []
        if not add_servers:
            QMessageBox.information(self, "Promote", "Укажите хотя бы один server_id.")
            return
        fn = getattr(self.facade, "admin_autonomy_promote_runbook", None)
        if not callable(fn):
            QMessageBox.warning(self, "Promote", "API недоступен.")
            return
        self.runbook_result_view.setPlainText("Promoting…")
        try:
            result = fn(
                self.session_uid,
                rb_id,
                add_servers=list(add_servers),
                confidence=payload.get("confidence"),
                run_validation=bool(payload.get("run_validation", True)),
            )
        except Exception:
            self.logger.exception("runbook promote failed")
            self.runbook_result_view.setPlainText("Ошибка promote.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.runbook_result_view.setPlainText(f"Ошибка: {err}")
            return
        r = dict(result.get("result") or {})
        lines = [
            f"Added: {', '.join(r.get('added_servers') or []) or '—'}",
            f"Already present: {', '.join(r.get('already_present') or []) or '—'}",
        ]
        if r.get("confidence_before") is not None or r.get("confidence_after") is not None:
            lines.append(
                f"Confidence: {r.get('confidence_before')} → {r.get('confidence_after')}"
            )
        self.runbook_result_view.setPlainText("\n".join(lines))

    def _run_runbook_step(self) -> None:
        rb_id = self._selected_runbook_id()
        rb = self._current_runbook or {}
        if not rb_id:
            return
        metadata = dict(rb.get("metadata") or {})
        steps = list(metadata.get("steps") or [])
        if not steps:
            QMessageBox.information(self, "Run step", "У runbook нет шагов.")
            return
        step_names = [str(s.get("name") or "") for s in steps if s.get("name")]
        if not step_names:
            QMessageBox.information(self, "Run step", "Шаги без имён.")
            return
        step_name, ok = QInputDialog.getItem(
            self, "Выбор шага", "Шаг для выполнения:", step_names, 0, False,
        )
        if not ok or not step_name:
            return
        dry_run = QMessageBox.question(
            self,
            "Run step",
            "Выполнить в режиме DRY-RUN?\n(Нет — выполнить реально на сервере)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if dry_run == QMessageBox.StandardButton.Cancel:
            return
        is_dry = dry_run == QMessageBox.StandardButton.Yes
        if not is_dry:
            confirm = QMessageBox.warning(
                self,
                "Run step",
                f"Запустить LIVE выполнение шага '{step_name}' на {self.server_id}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        fn = getattr(self.facade, "admin_autonomy_run_step", None)
        if not callable(fn):
            QMessageBox.warning(self, "Run step", "API недоступен.")
            return
        self.runbook_result_view.setPlainText(
            f"{'DRY' if is_dry else 'LIVE'}-run {step_name} на {self.server_id}…"
        )
        try:
            result = fn(
                self.session_uid,
                rb_id,
                step_name=step_name,
                server_id=self.server_id,
                dry_run=is_dry,
            )
        except Exception:
            self.logger.exception("runbook run_step failed")
            self.runbook_result_view.setPlainText("Ошибка выполнения.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.runbook_result_view.setPlainText(f"Ошибка: {err}")
            return
        r = dict(result.get("result") or {})
        dry_marker = "DRY-RUN" if r.get("dry_run") else f"rc={r.get('exit_code')}"
        header = (
            f"rb={r.get('rb_id')} step={r.get('step')} target={r.get('target')} "
            f"{dry_marker}"
        )
        stdout = r.get("stdout") or ""
        stderr = r.get("stderr") or ""
        error = r.get("error")
        parts = [header]
        if error:
            parts.append(f"[error] {error}")
        if stdout:
            parts.append(f"[stdout]\n{stdout}")
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        self.runbook_result_view.setPlainText("\n".join(parts))

    # ---------- builder ----------

    def _build_builder_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # Source scan
        src_grp = QGroupBox("1. Источник скриптов (admin.runbook_sources)")
        sv = QVBoxLayout(src_grp)
        sv.setContentsMargins(6, 6, 6, 6)
        dir_row = QHBoxLayout()
        self.builder_dir_edit = QLineEdit()
        self.builder_dir_edit.setPlaceholderText("/path/to/scripts/dir")
        dir_row.addWidget(self.builder_dir_edit, 1)
        self.builder_browse_btn = QPushButton("Обзор…")
        self.builder_browse_btn.clicked.connect(self._builder_browse_dir)
        dir_row.addWidget(self.builder_browse_btn)
        self.builder_scan_btn = QPushButton("Scan")
        self.builder_scan_btn.clicked.connect(self._builder_scan)
        dir_row.addWidget(self.builder_scan_btn)
        sv.addLayout(dir_row)
        self.builder_files_list = QListWidget()
        self.builder_files_list.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection,
        )
        self.builder_files_list.setMinimumHeight(120)
        sv.addWidget(self.builder_files_list)
        lay.addWidget(src_grp)

        # Metadata
        meta_grp = QGroupBox("2. Параметры runbook")
        mf = QFormLayout(meta_grp)
        mf.setContentsMargins(6, 6, 6, 6)
        self.builder_title_edit = QLineEdit()
        mf.addRow("Title:", self.builder_title_edit)
        self.builder_rb_id_edit = QLineEdit()
        self.builder_rb_id_edit.setPlaceholderText("опционально")
        mf.addRow("rb_id:", self.builder_rb_id_edit)
        self.builder_tags_edit = QLineEdit()
        self.builder_tags_edit.setPlaceholderText("через запятую")
        mf.addRow("Tags:", self.builder_tags_edit)
        self.builder_dev_edit = QLineEdit(self.server_id)
        mf.addRow("Dev server_id:", self.builder_dev_edit)
        self.builder_target_combo = QComboBox()
        self.builder_target_combo.addItem("local", "local")
        self.builder_target_combo.addItem("ssh", "ssh")
        mf.addRow("Target hint:", self.builder_target_combo)
        self.builder_desc_edit = QPlainTextEdit()
        self.builder_desc_edit.setMaximumHeight(70)
        mf.addRow("Description:", self.builder_desc_edit)
        lay.addWidget(meta_grp)

        # Build
        action_row = QHBoxLayout()
        self.builder_build_btn = QPushButton("Build runbook")
        self.builder_build_btn.clicked.connect(self._builder_build)
        action_row.addWidget(self.builder_build_btn)
        self.builder_validate_btn = QPushButton("Validate")
        self.builder_validate_btn.setEnabled(False)
        self.builder_validate_btn.clicked.connect(self._builder_validate)
        action_row.addWidget(self.builder_validate_btn)
        action_row.addStretch(1)
        lay.addLayout(action_row)

        self.builder_result_view = QPlainTextEdit()
        self.builder_result_view.setReadOnly(True)
        self.builder_result_view.setMaximumHeight(180)
        lay.addWidget(self.builder_result_view, 1)

        self._builder_scanned_files: list[dict[str, Any]] = []
        self._builder_last_rb_id: Optional[str] = None
        return w

    def _builder_browse_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Каталог скриптов", self.builder_dir_edit.text() or str(Path.home()),
        )
        if directory:
            self.builder_dir_edit.setText(directory)

    def _builder_scan(self) -> None:
        directory = self.builder_dir_edit.text().strip()
        if not directory:
            self.builder_result_view.setPlainText("Укажите каталог.")
            return
        fn = getattr(self.facade, "admin_autonomy_scan_scripts", None)
        if not callable(fn):
            self.builder_result_view.setPlainText("API недоступен.")
            return
        self.builder_result_view.setPlainText("Сканируем…")
        try:
            result = fn(self.session_uid, directory)
        except Exception:
            self.logger.exception("builder scan failed")
            self.builder_result_view.setPlainText("Ошибка сканирования.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.builder_result_view.setPlainText(f"Ошибка: {err}")
            return
        files = list(result.get("files") or [])
        self._builder_scanned_files = files
        self.builder_files_list.clear()
        for f in files:
            label = f"{f.get('name')}  ({f.get('size_bytes', 0)} байт · sha1:{str(f.get('sha1') or '')[:8]}…)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, f)
            item.setSelected(True)
            self.builder_files_list.addItem(item)
        self.builder_result_view.setPlainText(
            f"Найдено файлов: {len(files)}. Отметьте нужные в списке."
        )

    def _builder_build(self) -> None:
        title = self.builder_title_edit.text().strip()
        dev_sid = self.builder_dev_edit.text().strip()
        if not title:
            self.builder_result_view.setPlainText("Title обязателен.")
            return
        if not dev_sid:
            self.builder_result_view.setPlainText("Dev server_id обязателен.")
            return
        selected_files: list[dict[str, Any]] = []
        for item in self.builder_files_list.selectedItems():
            meta = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(meta, dict):
                selected_files.append(meta)
        if not selected_files:
            self.builder_result_view.setPlainText("Выберите хотя бы один скрипт.")
            return
        target = str(self.builder_target_combo.currentData() or "local")
        scripts_payload = [
            {
                "source_path": str(f.get("path") or ""),
                "name": str(f.get("name") or ""),
                "target_hint": target,
            }
            for f in selected_files
        ]
        tags = [t.strip() for t in self.builder_tags_edit.text().split(",") if t.strip()]
        fn = getattr(self.facade, "admin_autonomy_build_runbook", None)
        if not callable(fn):
            self.builder_result_view.setPlainText("API недоступен.")
            return
        self.builder_result_view.setPlainText("Сборка runbook…")
        try:
            result = fn(
                self.session_uid,
                title=title,
                dev_server_id=dev_sid,
                scripts=scripts_payload,
                rb_id=self.builder_rb_id_edit.text().strip() or None,
                tags=tags,
                description=self.builder_desc_edit.toPlainText(),
            )
        except Exception:
            self.logger.exception("builder build failed")
            self.builder_result_view.setPlainText("Ошибка сборки.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.builder_result_view.setPlainText(f"Ошибка: {err}")
            return
        rb = dict(result.get("runbook") or {})
        self._builder_last_rb_id = str(rb.get("id") or "") or None
        self.builder_validate_btn.setEnabled(bool(self._builder_last_rb_id))
        self.builder_result_view.setPlainText(
            f"Собран runbook {rb.get('id')}\n"
            f"path: {rb.get('path')}\n"
            f"servers: {', '.join(rb.get('servers') or [])}\n"
            "Нажмите Validate для проверки."
        )

    def _builder_validate(self) -> None:
        rb_id = self._builder_last_rb_id
        if not rb_id:
            return
        fn = getattr(self.facade, "admin_autonomy_validate_runbook", None)
        if not callable(fn):
            self.builder_result_view.setPlainText("API недоступен.")
            return
        self.builder_result_view.setPlainText("Validating…")
        try:
            result = fn(self.session_uid, rb_id)
        except Exception:
            self.logger.exception("builder validate failed")
            self.builder_result_view.setPlainText("Ошибка validate.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.builder_result_view.setPlainText(f"Ошибка: {err}")
            return
        report = dict(result.get("report") or {})
        lines = [f"Validation: {'OK' if report.get('ok') else 'FAIL'}"]
        for check in report.get("checks") or []:
            lines.append(
                f"  - {check.get('step')}: shellcheck={check.get('shellcheck')} "
                f"bash_n={check.get('bash_n')}"
            )
        for e in report.get("errors") or []:
            lines.append(f"ERR: {e}")
        for warn in report.get("warnings") or []:
            lines.append(f"WARN: {warn}")
        self.builder_result_view.setPlainText("\n".join(lines))

    # ---------- snapshots ----------

    def _build_snapshots_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(QLabel("check_id:"))
        self.snapshots_check_combo = QComboBox()
        self.snapshots_check_combo.setMinimumWidth(280)
        self.snapshots_check_combo.currentIndexChanged.connect(
            lambda _: self._load_snapshots(),
        )
        top.addWidget(self.snapshots_check_combo, 1)
        self.snapshots_reload_btn = QPushButton("Обновить список")
        self.snapshots_reload_btn.clicked.connect(self._refresh_snapshot_checks)
        top.addWidget(self.snapshots_reload_btn)
        top.addStretch(1)
        lay.addLayout(top)

        self.snapshots_table = QTableWidget(0, 3)
        self.snapshots_table.setHorizontalHeaderLabels(["ts", "value", "hash"])
        self.snapshots_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.snapshots_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.snapshots_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.snapshots_table, 1)
        return w

    def _refresh_snapshot_checks(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_list_snapshot_checks", None)
        self.snapshots_check_combo.blockSignals(True)
        self.snapshots_check_combo.clear()
        if callable(fn):
            try:
                checks = list(fn(self.session_uid, self.server_id) or [])
            except Exception:
                self.logger.exception("snapshots: list checks failed")
                checks = []
            for c in checks:
                self.snapshots_check_combo.addItem(str(c), str(c))
        self.snapshots_check_combo.blockSignals(False)
        self._load_snapshots()

    def _load_snapshots(self) -> None:
        self.snapshots_table.setRowCount(0)
        check_id = str(self.snapshots_check_combo.currentData() or "").strip()
        if not check_id:
            return
        fn = getattr(self.facade, "admin_autonomy_get_snapshots", None)
        if not callable(fn):
            return
        try:
            rows = list(fn(self.session_uid, self.server_id, check_id, limit=200) or [])
        except Exception:
            self.logger.exception("snapshots: load failed")
            return
        self.snapshots_table.setRowCount(len(rows))
        for row_idx, row in enumerate(rows):
            ts = row.get("ts") or row.get("timestamp") or ""
            value = row.get("value")
            value_text = _dump_yaml_safe(value) if isinstance(value, (dict, list)) else str(value)
            if len(value_text) > 240:
                value_text = value_text[:240] + "…"
            hash_text = str(row.get("hash") or row.get("value_hash") or "")[:16]
            self.snapshots_table.setItem(row_idx, 0, QTableWidgetItem(str(ts)))
            self.snapshots_table.setItem(row_idx, 1, QTableWidgetItem(value_text))
            self.snapshots_table.setItem(row_idx, 2, QTableWidgetItem(hash_text))

    # ---------- prereqs ----------

    def _build_prereqs_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        ctr = QHBoxLayout()
        self.prereqs_check_btn = QPushButton("Check prereqs")
        self.prereqs_check_btn.clicked.connect(self._refresh_prereqs)
        ctr.addWidget(self.prereqs_check_btn)
        self.prereqs_bootstrap_btn = QPushButton("Build bootstrap runbook")
        self.prereqs_bootstrap_btn.clicked.connect(self._build_prereqs_bootstrap)
        self.prereqs_bootstrap_btn.setEnabled(False)
        ctr.addWidget(self.prereqs_bootstrap_btn)
        ctr.addStretch(1)
        lay.addLayout(ctr)

        self.prereqs_view = QPlainTextEdit()
        self.prereqs_view.setReadOnly(True)
        lay.addWidget(self.prereqs_view, 1)
        return w

    def _refresh_prereqs(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_check_prereqs", None)
        if not callable(fn):
            self.prereqs_view.setPlainText("API недоступен.")
            return
        try:
            result = fn(self.session_uid, self.server_id)
        except Exception:
            self.logger.exception("prereqs: check failed")
            self.prereqs_view.setPlainText("Ошибка проверки.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            self.prereqs_view.setPlainText(f"Ошибка: {err}")
            self.prereqs_bootstrap_btn.setEnabled(False)
            return
        report = dict(result.get("report") or {})
        self.prereqs_view.setPlainText(_dump_yaml_safe(report))
        missing = list(report.get("required_missing") or []) + list(
            report.get("recommended_missing") or [],
        )
        self.prereqs_bootstrap_btn.setEnabled(bool(missing) and bool(report.get("installable")))

    def _build_prereqs_bootstrap(self) -> None:
        fn = getattr(self.facade, "admin_autonomy_build_prereqs_bootstrap", None)
        if not callable(fn):
            return
        try:
            result = fn(self.session_uid, self.server_id, force=False)
        except Exception:
            self.logger.exception("prereqs: bootstrap failed")
            QMessageBox.critical(self, "Bootstrap", "Ошибка.")
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            QMessageBox.warning(self, "Bootstrap", f"Ошибка: {err}")
            return
        reason = str(result.get("reason") or "")
        rb = result.get("runbook")
        if rb is None:
            QMessageBox.information(
                self, "Bootstrap", f"Runbook не создан. Причина: {reason or 'unknown'}",
            )
            return
        QMessageBox.information(
            self,
            "Bootstrap",
            f"Создан runbook {rb.get('id')}. Путь: {rb.get('path')}",
        )
        self._refresh_runbooks()

    # ---------- refresh all ----------

    def _refresh_all(self) -> None:
        self._refresh_overview()
        self._refresh_baseline()
        self._refresh_drifts()
        self._refresh_memory()
        self._refresh_runbooks()
        self._refresh_snapshot_checks()


class RunbookPromoteDialog(QDialog):
    """Диалог promote runbook: allowlist серверов, опциональный confidence, run_validation."""

    def __init__(
        self,
        *,
        default_server_id: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Promote runbook")
        self.resize(420, 220)
        layout = QFormLayout(self)

        self.servers_edit = QLineEdit(default_server_id or "")
        self.servers_edit.setPlaceholderText("prod-01, prod-02")
        layout.addRow("Add servers:", self.servers_edit)

        self.confidence_spin = QDoubleSpinBox()
        self.confidence_spin.setRange(-1.0, 1.0)
        self.confidence_spin.setDecimals(2)
        self.confidence_spin.setSingleStep(0.1)
        self.confidence_spin.setValue(-1.0)
        self.confidence_spin.setSpecialValueText("— (не менять)")
        layout.addRow("Confidence:", self.confidence_spin)

        self.validate_check = QCheckBox("Run validation first")
        self.validate_check.setChecked(True)
        layout.addRow("", self.validate_check)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = QPushButton("Promote")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addRow(btn_row)

    def result_payload(self) -> dict[str, Any]:
        servers = [s.strip() for s in self.servers_edit.text().split(",") if s.strip()]
        conf_value = self.confidence_spin.value()
        confidence: Optional[float] = None
        if conf_value >= 0.0:
            confidence = float(conf_value)
        return {
            "add_servers": servers,
            "confidence": confidence,
            "run_validation": bool(self.validate_check.isChecked()),
        }
