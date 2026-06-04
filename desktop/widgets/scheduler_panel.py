from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from i18n import t
from modes.sdk.runtime.json_normalizer import loads_safe
from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade


class SchedulerPanelWidget(QWidget):
    def __init__(
        self,
        facade: ApplicationFacade,
        *,
        actor_id: str = "desktop",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.facade = facade
        self.actor_id = str(actor_id or "desktop")
        self.logger = logging.getLogger(__name__)
        self._projects: list[Dict[str, Any]] = []
        self._jobs_by_id: Dict[str, Dict[str, Any]] = {}
        self._targets_by_uid: Dict[str, Dict[str, Any]] = {}
        self._selected_job_id: Optional[str] = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

        self.setObjectName("scheduler_panel")
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        lang = self.facade.ui_language
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.title_label = QLabel(t("desktop.scheduler.title", lang))
        self.title_label.setObjectName("scheduler_panel_title")
        layout.addWidget(self.title_label)

        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)

        self.project_label = QLabel(t("desktop.scheduler.project_label", lang))
        controls_layout.addWidget(self.project_label)
        self.project_selector = QComboBox()
        self.project_selector.setObjectName("scheduler_project_selector")
        self.project_selector.currentIndexChanged.connect(self._on_project_changed)
        controls_layout.addWidget(self.project_selector, 1)

        self.session_label = QLabel(t("desktop.scheduler.session_uid_label", lang))
        controls_layout.addWidget(self.session_label)
        self.session_selector = QComboBox()
        self.session_selector.setObjectName("scheduler_session_selector")
        controls_layout.addWidget(self.session_selector, 1)

        self.refresh_button = QPushButton(t("desktop.btn.refresh", lang))
        self.refresh_button.setObjectName("scheduler_refresh_button")
        self.refresh_button.clicked.connect(self.refresh)
        controls_layout.addWidget(self.refresh_button)
        layout.addLayout(controls_layout)

        self.settings_box = QGroupBox(t("desktop.scheduler.settings_group", lang))
        self.settings_box.setObjectName("scheduler_settings_box")
        settings_layout = QFormLayout(self.settings_box)
        settings_layout.setContentsMargins(8, 8, 8, 8)
        settings_layout.setSpacing(8)

        self.settings_timezone_label = QLabel(t("desktop.scheduler.settings_timezone_label", lang))
        self.settings_timezone_input = QLineEdit()
        self.settings_timezone_input.setObjectName("scheduler_settings_timezone")
        self.settings_timezone_input.setPlaceholderText("UTC")
        settings_layout.addRow(self.settings_timezone_label, self.settings_timezone_input)

        self.settings_tick_label = QLabel(t("desktop.scheduler.settings_tick_label", lang))
        self.settings_tick_input = QSpinBox()
        self.settings_tick_input.setObjectName("scheduler_settings_tick")
        self.settings_tick_input.setRange(1, 86400)
        self.settings_tick_input.setValue(60)
        settings_layout.addRow(self.settings_tick_label, self.settings_tick_input)

        self.settings_concurrency_label = QLabel(t("desktop.scheduler.settings_concurrency_label", lang))
        self.settings_concurrency_input = QSpinBox()
        self.settings_concurrency_input.setObjectName("scheduler_settings_concurrency")
        self.settings_concurrency_input.setRange(1, 100)
        self.settings_concurrency_input.setValue(1)
        settings_layout.addRow(self.settings_concurrency_label, self.settings_concurrency_input)

        self.settings_apply_button = QPushButton(t("desktop.scheduler.settings_apply", lang))
        self.settings_apply_button.setObjectName("scheduler_settings_apply_button")
        self.settings_apply_button.clicked.connect(self._apply_settings)
        settings_layout.addRow("", self.settings_apply_button)

        self.settings_status_label = QLabel("")
        self.settings_status_label.setObjectName("scheduler_settings_status")
        self.settings_status_label.setWordWrap(True)
        settings_layout.addRow("", self.settings_status_label)

        layout.addWidget(self.settings_box)
        self._load_settings()

        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)

        self.jobs_box = QGroupBox(t("desktop.scheduler.jobs_group", lang))
        self.jobs_box.setObjectName("scheduler_jobs_box")
        jobs_layout = QVBoxLayout(self.jobs_box)
        jobs_layout.setContentsMargins(8, 8, 8, 8)
        jobs_layout.setSpacing(8)

        self.jobs_list = QListWidget()
        self.jobs_list.setObjectName("scheduler_jobs_list")
        self.jobs_list.itemSelectionChanged.connect(self._on_job_selected)
        jobs_layout.addWidget(self.jobs_list, 1)
        body_layout.addWidget(self.jobs_box, 1)

        self.form_box = QGroupBox(t("desktop.scheduler.editor_group", lang))
        self.form_box.setObjectName("scheduler_editor_box")
        form_layout = QVBoxLayout(self.form_box)
        form_layout.setContentsMargins(8, 8, 8, 8)
        form_layout.setSpacing(8)

        fields = QFormLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(8)

        self.job_name_input = QLineEdit()
        self.job_name_input.setObjectName("scheduler_job_name_input")
        self.job_name_row_label = QLabel(t("desktop.scheduler.name_label", lang))
        fields.addRow(self.job_name_row_label, self.job_name_input)

        self.cron_input = QLineEdit()
        self.cron_input.setObjectName("scheduler_cron_input")
        self.cron_input.setPlaceholderText("*/5 * * * *")
        self.cron_row_label = QLabel(t("desktop.scheduler.cron_label", lang))
        fields.addRow(self.cron_row_label, self.cron_input)

        self.mode_selector = QComboBox()
        self.mode_selector.setObjectName("scheduler_mode_selector")
        self._reload_modes()
        self.mode_row_label = QLabel(t("desktop.scheduler.target_mode_label", lang))
        fields.addRow(self.mode_row_label, self.mode_selector)

        self.enabled_checkbox = QCheckBox(t("desktop.admin.label.monitor_enabled", lang))
        self.enabled_checkbox.setObjectName("scheduler_enabled_checkbox")
        self.enabled_checkbox.setChecked(True)
        fields.addRow("", self.enabled_checkbox)

        self.payload_label = QLabel(t("desktop.scheduler.payload_label", lang))
        self.payload_input = QTextEdit()
        self.payload_input.setObjectName("scheduler_payload_input")
        self.payload_input.setPlaceholderText('{"key": "value"}')
        self.payload_input.setMaximumHeight(120)
        fields.addRow(self.payload_label, self.payload_input)

        form_layout.addLayout(fields)

        self.job_meta_label = QLabel(t("desktop.scheduler.no_job_selected", lang))
        self.job_meta_label.setObjectName("scheduler_job_meta")
        self.job_meta_label.setWordWrap(True)
        form_layout.addWidget(self.job_meta_label)

        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)

        self.save_button = QPushButton(t("desktop.btn.save", lang))
        self.save_button.setObjectName("scheduler_save_button")
        self.save_button.clicked.connect(self._save_job)
        buttons_layout.addWidget(self.save_button)

        self.delete_button = QPushButton(t("desktop.btn.delete", lang))
        self.delete_button.setObjectName("scheduler_delete_button")
        self.delete_button.clicked.connect(self._delete_job)
        buttons_layout.addWidget(self.delete_button)

        self.run_now_button = QPushButton(t("desktop.scheduler.run_now", lang))
        self.run_now_button.setObjectName("scheduler_run_now_button")
        self.run_now_button.clicked.connect(self._run_job_now)
        buttons_layout.addWidget(self.run_now_button)

        self.pause_button = QPushButton(t("desktop.scheduler.pause", lang))
        self.pause_button.setObjectName("scheduler_pause_button")
        self.pause_button.clicked.connect(self._pause_job)
        buttons_layout.addWidget(self.pause_button)

        self.resume_button = QPushButton(t("desktop.scheduler.resume", lang))
        self.resume_button.setObjectName("scheduler_resume_button")
        self.resume_button.clicked.connect(self._resume_job)
        buttons_layout.addWidget(self.resume_button)

        self.reset_button = QPushButton(t("miniapp.btn.reset", lang))
        self.reset_button.setObjectName("scheduler_reset_button")
        self.reset_button.clicked.connect(self.reset_form)
        buttons_layout.addWidget(self.reset_button)

        form_layout.addLayout(buttons_layout)

        self.status_label = QLabel("")
        self.status_label.setObjectName("scheduler_status_label")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form_layout.addWidget(self.status_label)

        body_layout.addWidget(self.form_box, 1)
        layout.addLayout(body_layout)

    def set_context_session(self, session_uid: Optional[str]) -> None:
        slug = None
        if session_uid:
            try:
                slug = self.facade.resolve_scheduler_project_slug(str(session_uid))
            except Exception:
                self.logger.exception(
                    "failed to resolve scheduler project slug session_uid=%s",
                    session_uid,
                )
        self.refresh(project_slug=slug)

    def refresh(self, checked: bool = False, *, project_slug: Optional[str] = None) -> None:
        _ = checked
        try:
            projects = list(self.facade.list_scheduler_projects() or [])
        except Exception as exc:
            self._projects = []
            self._render_projects(selected_slug=None)
            self._render_scope(project_slug=None)
            self.set_status(t("desktop.scheduler.err_load_projects", self.facade.ui_language, error=exc))
            return

        selected_slug = str(project_slug or self.current_project_slug() or "").strip() or None
        if selected_slug and not any(str(item.get("slug", "")) == selected_slug for item in projects):
            selected_slug = None
        if selected_slug is None and projects:
            selected_slug = str(projects[0].get("slug", "") or "").strip() or None
        self._projects = projects
        self._render_projects(selected_slug=selected_slug)
        self._render_scope(project_slug=selected_slug)

    def current_project_slug(self) -> Optional[str]:
        token = self.project_selector.currentData()
        return str(token or "").strip() or None

    def reset_form(self) -> None:
        self._selected_job_id = None
        self.job_name_input.clear()
        self.cron_input.setText("*/5 * * * *")
        self.enabled_checkbox.setChecked(True)
        self.payload_input.clear()
        if self.mode_selector.count() > 0:
            self.mode_selector.setCurrentIndex(0)
        self.jobs_list.clearSelection()
        self._update_job_meta()

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text or ""))

    def _reload_modes(self) -> None:
        self.mode_selector.clear()
        seen: set[str] = set()
        for mode_id in list(self.facade.list_modes() or []):
            token = str(mode_id or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            self.mode_selector.addItem(token, token)

    def _render_projects(self, *, selected_slug: Optional[str]) -> None:
        self.project_selector.blockSignals(True)
        self.project_selector.clear()
        for project in self._projects:
            slug = str(project.get("slug", "") or "").strip()
            label = str(project.get("name", "") or slug).strip() or slug
            self.project_selector.addItem(label, slug)
        if selected_slug:
            index = self.project_selector.findData(selected_slug)
            if index >= 0:
                self.project_selector.setCurrentIndex(index)
        self.project_selector.blockSignals(False)

    def _render_scope(self, *, project_slug: Optional[str]) -> None:
        self._targets_by_uid = {}
        self._jobs_by_id = {}
        self.session_selector.clear()
        self.jobs_list.clear()
        if not project_slug:
            self.reset_form()
            self.set_status(t("desktop.scheduler.no_projects", self.facade.ui_language))
            return
        try:
            targets = list(
                self.facade.list_scheduler_notification_targets(project_slug=project_slug) or []
            )
            jobs = list(self.facade.list_scheduler_jobs(project_slug=project_slug) or [])
        except Exception as exc:
            self.reset_form()
            self.set_status(t("desktop.scheduler.err_load_scope", self.facade.ui_language, error=exc))
            return

        for item in targets:
            session_uid = str(item.get("session_uid", "") or "").strip()
            label = str(item.get("label", "") or session_uid).strip() or session_uid
            if not session_uid:
                continue
            self._targets_by_uid[session_uid] = dict(item)
            self.session_selector.addItem(label, session_uid)

        for job in jobs:
            job_id = str(job.get("job_id", "") or "").strip()
            if not job_id:
                continue
            self._jobs_by_id[job_id] = dict(job)
            item = QListWidgetItem(self._job_label(job))
            item.setData(Qt.ItemDataRole.UserRole, job_id)
            self.jobs_list.addItem(item)

        self.reset_form()
        self._set_button_state()
        if not targets:
            self.set_status(t("desktop.scheduler.no_sessions", self.facade.ui_language))
        else:
            self.set_status("")

    def _set_button_state(self) -> None:
        has_project = bool(self.current_project_slug())
        has_target = self.session_selector.count() > 0
        has_job = bool(self._selected_job_id)
        self.save_button.setEnabled(has_project and has_target)
        self.delete_button.setEnabled(has_project and has_job)
        self.run_now_button.setEnabled(has_project and has_job)
        self.pause_button.setEnabled(has_project and has_job)
        self.resume_button.setEnabled(has_project and has_job)

    def _update_job_meta(self) -> None:
        lang = self.facade.ui_language
        if not self._selected_job_id:
            self.job_meta_label.setText(t("desktop.scheduler.no_job_selected", lang))
            self._set_button_state()
            return
        job = self._jobs_by_id.get(str(self._selected_job_id))
        if not job:
            self.job_meta_label.setText(t("desktop.scheduler.no_job_selected", lang))
            self._set_button_state()
            return
        self.job_meta_label.setText(
            t(
                "desktop.scheduler.job_meta",
                lang,
                job_id=str(job.get("job_id", "") or ""),
                next_run=str(job.get("next_run_at", 0.0)),
                last_fired=str(job.get("last_fired_at", 0.0)),
                last_status=str(job.get("last_status", "") or "-"),
                run_count=str(job.get("run_count", 0) or 0),
                last_error=str(job.get("last_error", "") or "-"),
            )
        )
        self._set_button_state()

    def _on_project_changed(self, index: int) -> None:
        if index < 0:
            self._render_scope(project_slug=None)
            return
        project_slug = self.project_selector.itemData(index)
        self._render_scope(project_slug=str(project_slug or "").strip() or None)

    def _on_job_selected(self) -> None:
        items = self.jobs_list.selectedItems()
        if not items:
            self._selected_job_id = None
            self._update_job_meta()
            return
        job_id = str(items[0].data(Qt.ItemDataRole.UserRole) or "").strip()
        job = self._jobs_by_id.get(job_id)
        if not job:
            self._selected_job_id = None
            self._update_job_meta()
            return
        self._selected_job_id = job_id
        self.job_name_input.setText(str(job.get("job_name", "") or ""))
        self.cron_input.setText(str(job.get("cron", "") or ""))
        self.enabled_checkbox.setChecked(bool(job.get("enabled", False)))

        mode_id = str(job.get("target_mode", "") or "").strip()
        mode_index = self.mode_selector.findData(mode_id)
        if mode_index >= 0:
            self.mode_selector.setCurrentIndex(mode_index)

        notification_target = dict(job.get("notification_target", {}) or {})
        target_uid = str(notification_target.get("telegram_session_uid", "") or "").strip()
        target_index = self.session_selector.findData(target_uid)
        if target_index >= 0:
            self.session_selector.setCurrentIndex(target_index)

        # Restore payload into editor
        payload = dict(job.get("payload", {}) or {})
        if payload:
            self.payload_input.setPlainText(json.dumps(payload, indent=2))
        else:
            self.payload_input.clear()

        self._update_job_meta()

    def _job_label(self, job: Dict[str, Any]) -> str:
        lang = self.facade.ui_language
        name = str(job.get("job_name", "") or job.get("job_id", "") or "").strip()
        cron = str(job.get("cron", "") or "").strip()
        mode = str(job.get("target_mode", "") or "").strip()
        prefix = t("desktop.scheduler.job_on", lang) if bool(job.get("enabled", False)) else t("miniapp.status.off", lang)
        status = str(job.get("last_status", "") or "").strip()
        status_part = f" | {status}" if status else ""

        # Add payload summary
        payload = dict(job.get("payload", {}) or {})
        payload_keys = list(payload.keys())
        payload_summary = ""
        if payload_keys:
            # Exclude project_slug from summary since it's redundant
            display_keys = [k for k in payload_keys if k != "project_slug"]
            if display_keys:
                payload_summary = f" | payload: {', '.join(display_keys[:3])}"
                if len(display_keys) > 3:
                    payload_summary += "..."

        return f"[{prefix}] {name} | {cron} | {mode}{status_part}{payload_summary}"

    def _collect_form(self) -> Optional[Dict[str, Any]]:
        project_slug = self.current_project_slug()
        session_uid = self.session_selector.currentData()
        mode_id = self.mode_selector.currentData()
        lang = self.facade.ui_language
        if not project_slug:
            self.set_status(t("desktop.scheduler.err_project_required", lang))
            return None
        if not session_uid:
            self.set_status(t("desktop.scheduler.err_session_required", lang))
            return None
        if not mode_id:
            self.set_status(t("desktop.scheduler.err_mode_required", lang))
            return None

        # Parse and validate JSON payload
        payload_text = str(self.payload_input.toPlainText() or "").strip()
        payload = {}
        if payload_text:
            try:
                payload = loads_safe(payload_text, strict_first=True)
                if not isinstance(payload, dict):
                    self.set_status(t("desktop.scheduler.err_payload_not_object", lang))
                    return None
            except json.JSONDecodeError as exc:
                self.set_status(t("desktop.scheduler.err_payload_invalid_json", lang, error=exc))
                return None

        return {
            "project_slug": str(project_slug),
            "notification_target_session_uid": str(session_uid),
            "job_name": str(self.job_name_input.text() or "").strip(),
            "cron": str(self.cron_input.text() or "").strip(),
            "target_mode": str(mode_id),
            "enabled": bool(self.enabled_checkbox.isChecked()),
            "payload": payload,
        }

    def _save_job(self) -> None:
        payload = self._collect_form()
        if payload is None:
            return
        lang = self.facade.ui_language
        try:
            if self._selected_job_id:
                job = self.facade.update_scheduler_job(
                    project_slug=payload["project_slug"],
                    job_id=str(self._selected_job_id),
                    cron=payload["cron"],
                    target_mode=payload["target_mode"],
                    notification_target_session_uid=payload["notification_target_session_uid"],
                    enabled=payload["enabled"],
                    job_name=payload["job_name"],
                    payload=payload["payload"],
                )
                self.set_status(t("desktop.scheduler.job_updated", lang, job_id=job['job_id']))
            else:
                job = self.facade.create_scheduler_job(
                    project_slug=payload["project_slug"],
                    cron=payload["cron"],
                    target_mode=payload["target_mode"],
                    notification_target_session_uid=payload["notification_target_session_uid"],
                    enabled=payload["enabled"],
                    job_name=payload["job_name"],
                    payload=payload["payload"],
                )
                self.set_status(t("desktop.scheduler.job_created", lang, job_id=job['job_id']))
            self.refresh(project_slug=payload["project_slug"])
            self._select_job(job_id=str(job.get("job_id", "") or ""))
        except Exception as exc:
            self.logger.exception("desktop scheduler save failed")
            self.set_status(t("desktop.scheduler.err_save", lang, error=exc))

    def _delete_job(self) -> None:
        project_slug = self.current_project_slug()
        job_id = str(self._selected_job_id or "").strip()
        if not project_slug or not job_id:
            return
        lang = self.facade.ui_language
        try:
            deleted = self.facade.delete_scheduler_job(
                project_slug=str(project_slug),
                job_id=job_id,
            )
            self.refresh(project_slug=str(project_slug))
            self.set_status(
                t("desktop.scheduler.job_deleted", lang) if deleted
                else t("desktop.scheduler.job_already_removed", lang)
            )
        except Exception as exc:
            self.logger.exception("desktop scheduler delete failed")
            self.set_status(t("desktop.scheduler.err_delete", lang, error=exc))

    def _run_job_now(self) -> None:
        project_slug = self.current_project_slug()
        job_id = str(self._selected_job_id or "").strip()
        if not project_slug or not job_id:
            return

        async def _runner() -> None:
            lang = self.facade.ui_language
            try:
                event = await self.facade.run_scheduler_job_now(
                    project_slug=str(project_slug),
                    job_id=job_id,
                )
                self.refresh(project_slug=str(project_slug))
                self._select_job(job_id=job_id)
                self.set_status(t("desktop.scheduler.job_triggered", lang, job_id=event['job_id']))
            except Exception as exc:
                self.logger.exception("desktop scheduler run_now failed")
                self.set_status(t("desktop.scheduler.err_trigger", lang, error=exc))

        self._schedule_async(_runner)

    def _pause_job(self) -> None:
        project_slug = self.current_project_slug()
        job_id = str(self._selected_job_id or "").strip()
        if not project_slug or not job_id:
            return
        lang = self.facade.ui_language
        try:
            job = self.facade.pause_scheduler_job(project_slug=str(project_slug), job_id=job_id)
            self.refresh(project_slug=str(project_slug))
            self._select_job(job_id=str(job.get("job_id", "") or ""))
            self.set_status(t("desktop.scheduler.job_paused", lang, job_id=job['job_id']))
        except Exception as exc:
            self.logger.exception("desktop scheduler pause failed")
            self.set_status(t("desktop.scheduler.err_pause", lang, error=exc))

    def _resume_job(self) -> None:
        project_slug = self.current_project_slug()
        job_id = str(self._selected_job_id or "").strip()
        if not project_slug or not job_id:
            return
        lang = self.facade.ui_language
        try:
            job = self.facade.resume_scheduler_job(project_slug=str(project_slug), job_id=job_id)
            self.refresh(project_slug=str(project_slug))
            self._select_job(job_id=str(job.get("job_id", "") or ""))
            self.set_status(t("desktop.scheduler.job_resumed", lang, job_id=job['job_id']))
        except Exception as exc:
            self.logger.exception("desktop scheduler resume failed")
            self.set_status(t("desktop.scheduler.err_resume", lang, error=exc))

    def _select_job(self, *, job_id: str) -> None:
        target = str(job_id or "").strip()
        if not target:
            return
        for index in range(self.jobs_list.count()):
            item = self.jobs_list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole) or "").strip() == target:
                self.jobs_list.setCurrentItem(item)
                return

    def _schedule_async(self, coro_factory) -> Optional[asyncio.Task[Any]]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if not loop.is_running():
            return None
        task = ensure_async(coro_factory(), parent=self)
        if task is not None:
            self._background_tasks.add(task)
            task.add_done_callback(lambda done: self._background_tasks.discard(done))
        return task

    def _load_settings(self) -> None:
        try:
            settings = self.facade.get_scheduler_settings()
        except Exception:
            self.logger.exception("desktop scheduler _load_settings failed")
            return
        self.settings_timezone_input.setText(str(settings.get("timezone", "") or ""))
        tick = int(settings.get("tick_interval_sec", 60) or 60)
        self.settings_tick_input.setValue(max(1, tick))
        concurrency = int(settings.get("max_concurrent_jobs", 1) or 1)
        self.settings_concurrency_input.setValue(max(1, concurrency))

    def _apply_settings(self) -> None:
        lang = self.facade.ui_language
        timezone = str(self.settings_timezone_input.text() or "").strip()
        if not timezone:
            self.settings_status_label.setText(
                t("desktop.scheduler.settings_err_timezone", lang)
            )
            return

        async def _save() -> None:
            try:
                result = await self.facade.update_scheduler_settings(
                    timezone=timezone,
                    tick_interval_sec=int(self.settings_tick_input.value()),
                    max_concurrent_jobs=int(self.settings_concurrency_input.value()),
                )
                if result.get("ok"):
                    self.settings_status_label.setText(
                        t("desktop.scheduler.settings_saved", lang)
                    )
                else:
                    self.settings_status_label.setText(
                        t("desktop.scheduler.settings_err_save", lang, error=str(result.get("error", "")))
                    )
            except Exception as exc:
                self.logger.exception("desktop scheduler apply_settings failed")
                self.settings_status_label.setText(
                    t("desktop.scheduler.settings_err_save", lang, error=exc)
                )

        self._schedule_async(_save)

    def retranslate_ui(self, lang: str) -> None:
        self.title_label.setText(t("desktop.scheduler.title", lang))
        self.project_label.setText(t("desktop.scheduler.project_label", lang))
        self.session_label.setText(t("desktop.scheduler.session_uid_label", lang))
        self.refresh_button.setText(t("desktop.btn.refresh", lang))
        self.settings_box.setTitle(t("desktop.scheduler.settings_group", lang))
        self.settings_timezone_label.setText(t("desktop.scheduler.settings_timezone_label", lang))
        self.settings_tick_label.setText(t("desktop.scheduler.settings_tick_label", lang))
        self.settings_concurrency_label.setText(t("desktop.scheduler.settings_concurrency_label", lang))
        self.settings_apply_button.setText(t("desktop.scheduler.settings_apply", lang))
        self.jobs_box.setTitle(t("desktop.scheduler.jobs_group", lang))
        self.form_box.setTitle(t("desktop.scheduler.editor_group", lang))
        self.job_name_row_label.setText(t("desktop.scheduler.name_label", lang))
        self.cron_row_label.setText(t("desktop.scheduler.cron_label", lang))
        self.mode_row_label.setText(t("desktop.scheduler.target_mode_label", lang))
        self.enabled_checkbox.setText(t("desktop.admin.label.monitor_enabled", lang))
        self.payload_label.setText(t("desktop.scheduler.payload_label", lang))
        self.save_button.setText(t("desktop.btn.save", lang))
        self.delete_button.setText(t("desktop.btn.delete", lang))
        self.run_now_button.setText(t("desktop.scheduler.run_now", lang))
        self.pause_button.setText(t("desktop.scheduler.pause", lang))
        self.resume_button.setText(t("desktop.scheduler.resume", lang))
        self.reset_button.setText(t("miniapp.btn.reset", lang))
        self._update_job_meta()
