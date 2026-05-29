from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import AppNotification, ApplicationFacade


def _clean_text(value: Any, *, max_len: int = 256) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _recommended_action_label(action: Any) -> str:
    token = _clean_text(action, max_len=64)
    if token == "rerun_same_operation":
        return "Rerun"
    if token == "run_validate":
        return "Validate"
    if token == "run_repair":
        return "Repair"
    return "Apply Recommendation"


def _policy_operation(action: str) -> str:
    token = str(action or "").strip()
    if token == "promote_run_skills":
        return "promote_skills"
    return token


def _action_policy(record: dict[str, Any], action: str) -> dict[str, Any]:
    policy = record.get("run_operations_policy")
    if not isinstance(policy, dict):
        return {"allowed": True, "visibility": "show", "reason": ""}
    raw = policy.get(_policy_operation(action))
    if not isinstance(raw, dict):
        return {"allowed": True, "visibility": "show", "reason": ""}
    visibility = str(raw.get("visibility") or "").strip().lower() or "hide"
    if visibility not in {"show", "disable", "hide"}:
        visibility = "hide"
    allowed = bool(raw.get("allowed")) if "allowed" in raw else visibility == "show"
    return {
        "allowed": allowed,
        "visibility": visibility,
        "reason": str(raw.get("reason") or ""),
    }


def _policy_visible(policy: dict[str, Any]) -> bool:
    return str(policy.get("visibility") or "").strip().lower() != "hide"


def _policy_enabled(policy: dict[str, Any], base_enabled: bool) -> bool:
    return bool(base_enabled and policy.get("allowed") and policy.get("visibility") == "show")


class RunOperationsPanelWidget(QWidget):
    """Desktop context panel for recent run artifacts and recovery actions."""

    POLL_INTERVAL_MS = 2000

    def __init__(
        self,
        facade: "ApplicationFacade",
        *,
        session_uid: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.facade = facade
        self._session_uid = str(session_uid or "").strip() or None
        self.logger = logger or logging.getLogger(__name__)
        self._pending_run_ids: set[str] = set()
        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)

        self._setup_ui()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.refresh)
        self._poll_timer.start()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        title = QLabel("Runs")
        title.setObjectName("run_operations_title")
        layout.addWidget(title)

        self.summary_label = QLabel("Сессия не выбрана")
        self.summary_label.setObjectName("run_operations_summary")
        layout.addWidget(self.summary_label)

        self.last_action_label = QLabel("")
        self.last_action_label.setObjectName("run_operations_last_action")
        self.last_action_label.setWordWrap(True)
        self.last_action_label.hide()
        layout.addWidget(self.last_action_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.scroll, 1)

        self.container = QWidget()
        self.rows_layout = QVBoxLayout(self.container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch()
        self.scroll.setWidget(self.container)

    def set_session_id(self, session_uid: Optional[str]) -> None:
        self._session_uid = str(session_uid or "").strip() or None
        self._pending_run_ids.clear()
        self.last_action_label.hide()
        self.last_action_label.setText("")
        self.refresh()

    def refresh(self) -> None:
        while self.rows_layout.count() > 1:
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if not self._session_uid:
            self.summary_label.setText("Сессия не выбрана")
            return

        try:
            runs = list(self.facade.list_runs(self._session_uid))
        except Exception:
            self.logger.exception("desktop run panel failed to list runs session_uid=%s", self._session_uid)
            self.summary_label.setText("Не удалось загрузить список запусков")
            return

        active_count = sum(1 for item in runs if bool(item.get("active")))
        if not runs:
            self.summary_label.setText("Запусков нет")
            return

        self.summary_label.setText(f"Активных запусков: {active_count} · Всего: {len(runs)}")
        for record in runs:
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, self._build_run_row(record))

    def _build_run_row(self, record: dict[str, Any]) -> QWidget:
        frame = QFrame()
        frame.setObjectName("run_operations_item_frame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        mode_id = _clean_text(record.get("mode_id"), max_len=64) or "unknown"
        run_id = _clean_text(record.get("run_id"), max_len=128) or "run"
        phase = _clean_text(record.get("phase"), max_len=64) or "unknown"
        status = _clean_text(record.get("status"), max_len=32) or "unknown"

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        title = QLabel(f"{mode_id} · {run_id}")
        title.setObjectName("run_operations_item_title")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header_row.addWidget(title, 1)

        status_label = QLabel(f"{phase} · {status}")
        status_label.setObjectName("run_operations_item_status")
        header_row.addWidget(status_label)
        layout.addLayout(header_row)

        meta_chunks: list[str] = []
        recommended_action = _clean_text(record.get("recommended_action"), max_len=64)
        if recommended_action:
            meta_chunks.append(f"Doctor: {recommended_action}")
        issue_codes = [
            _clean_text(code, max_len=48)
            for code in list(record.get("issue_codes") or [])
            if _clean_text(code, max_len=48)
        ]
        if issue_codes:
            meta_chunks.append(f"Issues: {', '.join(issue_codes[:3])}")
        executor_profile = _clean_text(record.get("executor_profile"), max_len=64)
        if executor_profile:
            meta_chunks.append(f"Exec: {executor_profile}")
        cli_work_type = _clean_text(record.get("cli_work_type"), max_len=64)
        if cli_work_type:
            meta_chunks.append(f"CLI: {cli_work_type}")
        meta_label = QLabel(" · ".join(meta_chunks) if meta_chunks else "Нет recovery-метаданных")
        meta_label.setObjectName("run_operations_item_meta")
        meta_label.setWordWrap(True)
        layout.addWidget(meta_label)

        skill_log = [
            _clean_text(item, max_len=160)
            for item in list(record.get("skill_log") or [])
            if _clean_text(item, max_len=160)
        ]
        skills_text = "Skills: " + (" | ".join(skill_log) if skill_log else "нет инъекций")
        skills_label = QLabel(skills_text)
        skills_label.setObjectName("run_operations_item_skills")
        skills_label.setWordWrap(True)
        layout.addWidget(skills_label)

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(6)

        pending = run_id in self._pending_run_ids
        terminal_actions_blocked = bool(record.get("terminal_actions_blocked")) or status in {
            "completed",
            "superseded",
        }
        doctor_policy = _action_policy(record, "doctor")
        if _policy_visible(doctor_policy):
            buttons_row.addWidget(
                self._build_action_button(
                    "Doctor",
                    "doctor",
                    record,
                    enabled=_policy_enabled(doctor_policy, not pending),
                )
            )
        can_apply_recommendation = bool(record.get("can_apply_recommendation"))
        can_recover = bool(record.get("can_recover")) and not terminal_actions_blocked and not can_apply_recommendation
        recover_policy = _action_policy(record, "recover")
        if _policy_visible(recover_policy):
            buttons_row.addWidget(
                self._build_action_button(
                    "Recover",
                    "recover",
                    record,
                    enabled=_policy_enabled(recover_policy, not pending and can_recover),
                )
            )
        can_resume = bool(record.get("can_resume")) and not terminal_actions_blocked
        resume_policy = _action_policy(record, "resume")
        if _policy_visible(resume_policy):
            buttons_row.addWidget(
                self._build_action_button(
                    "Resume",
                    "resume",
                    record,
                    enabled=_policy_enabled(resume_policy, not pending and can_resume),
                )
            )
        apply_policy = _action_policy(record, "apply_recommendation")
        if can_apply_recommendation and _policy_visible(apply_policy):
            buttons_row.addWidget(
                self._build_action_button(
                    _recommended_action_label(record.get("recommended_action")),
                    "apply_recommendation",
                    record,
                    enabled=_policy_enabled(apply_policy, not pending),
                )
            )
        can_promote = bool(list(record.get("project_local_skill_ids") or []))
        promote_policy = _action_policy(record, "promote_run_skills")
        if _policy_visible(promote_policy):
            buttons_row.addWidget(
                self._build_action_button(
                    "Promote Skills",
                    "promote_run_skills",
                    record,
                    enabled=_policy_enabled(promote_policy, not pending and can_promote),
                )
            )
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        return frame

    def _build_action_button(
        self,
        label: str,
        action: str,
        record: dict[str, Any],
        *,
        enabled: bool,
    ) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName(f"run_operations_{action}_button")
        button.setEnabled(bool(enabled))
        button.clicked.connect(partial(self._trigger_action, action, dict(record)))
        return button

    def _trigger_action(self, action: str, record: dict[str, Any]) -> None:
        if not self._session_uid:
            return
        run_id = _clean_text(record.get("run_id"), max_len=128)
        if not run_id or run_id in self._pending_run_ids:
            return
        self._pending_run_ids.add(run_id)
        self.refresh()

        async def _run() -> None:
            try:
                action_name = str(action or "").strip()
                method_name = action_name if action_name.endswith("_run_skills") else f"{action_name}_run"
                method = getattr(self.facade, method_name, None)
                if not callable(method):
                    self.last_action_label.setText("Run-операция недоступна в facade.")
                    self.last_action_label.show()
                    return
                result = await method(
                    self._session_uid,
                    mode_id=record.get("mode_id"),
                    run_id=record.get("run_id"),
                )
                message = _clean_text((result or {}).get("message"), max_len=240)
                if message:
                    self.last_action_label.setText(message)
                    self.last_action_label.show()
            except Exception:
                self.logger.exception(
                    "desktop run panel action failed action=%s run_id=%s",
                    action,
                    run_id,
                )
                self.last_action_label.setText("Операция завершилась ошибкой.")
                self.last_action_label.show()
            finally:
                self._pending_run_ids.discard(run_id)
                self.refresh()

        ensure_async(_run(), parent=self)

    def _on_facade_notification(self, note: "AppNotification") -> None:
        if self._session_uid is None:
            return
        note_session = str(note.payload.get("session_uid") or note.payload.get("session_id") or "").strip()
        if note_session and note_session != self._session_uid:
            return
        if note.event in {
            "ui:runs_updated",
            "task:started",
            "task:completed",
            "task:failed",
            "task:cancelled",
        }:
            self.refresh()

    def closeEvent(self, event):  # type: ignore[override]
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        timer = getattr(self, "_poll_timer", None)
        if timer is not None:
            timer.stop()
        super().closeEvent(event)
