from __future__ import annotations

import logging
import time
from functools import partial
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade, AppNotification


class TaskQueueWidget(QWidget):
    """UI очереди задач Desktop: активные задачи, приоритет и отмена."""

    def __init__(
        self,
        facade: ApplicationFacade,
        *,
        session_uid: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.facade = facade
        self._session_uid = str(session_uid) if session_uid else None
        self.logger = logger or logging.getLogger(__name__)
        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)

        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        title = QLabel("Task Queue")
        title.setObjectName("task_queue_title")
        layout.addWidget(title)

        self.summary_label = QLabel("Нет активных задач")
        self.summary_label.setObjectName("task_queue_summary")
        layout.addWidget(self.summary_label)

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
        self._session_uid = str(session_uid) if session_uid else None
        self.refresh()

    def refresh(self) -> None:
        while self.rows_layout.count() > 1:
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        tasks = self.facade.list_active_tasks(session_uid=self._session_uid)
        if not tasks:
            self.summary_label.setText("Нет активных задач")
            return

        self.summary_label.setText(f"Активных задач: {len(tasks)}")
        now = time.time()
        for rec in tasks:
            frame = QFrame()
            frame.setObjectName("task_item_frame")
            row = QHBoxLayout(frame)
            row.setContentsMargins(8, 6, 8, 6)
            row.setSpacing(8)

            title = f"{rec.name} [{rec.task_id[:8]}]"
            if rec.session_id:
                title += f" · {rec.session_id}"
            left = QLabel(title)
            left.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(left, 1)

            stage = str(getattr(rec, "stage", "") or "")
            progress = float(getattr(rec, "progress", 0.0) or 0.0)
            age_s = max(0, int(now - float(getattr(rec, "created_at", now) or now)))
            info_text = f"{stage or 'running'} · {int(progress * 100)}% · {age_s}s"
            info = QLabel(info_text)
            info.setObjectName("task_item_info")
            row.addWidget(info)

            priority_box = QSpinBox()
            priority_box.setRange(-10, 10)
            priority_box.setPrefix("P:")
            priority_box.setValue(int(getattr(rec, "priority", 0) or 0))
            priority_box.valueChanged.connect(partial(self._on_priority_changed, str(rec.task_id)))
            row.addWidget(priority_box)

            cancel_btn = QPushButton("Отмена")
            cancel_btn.clicked.connect(partial(self._on_cancel_clicked, str(rec.task_id)))
            row.addWidget(cancel_btn)

            self.rows_layout.insertWidget(self.rows_layout.count() - 1, frame)

    def _on_priority_changed(self, task_id: str, value: int) -> None:
        if not self.facade.set_task_priority(str(task_id), int(value)):
            self.logger.warning("failed to change priority task_id=%s value=%s", task_id, value)
        self.refresh()

    def _on_cancel_clicked(self, task_id: str) -> None:
        async def _cancel() -> None:
            await self.facade.cancel_task(str(task_id), reason="ui_cancel")
            self.refresh()

        ensure_async(_cancel(), parent=self)

    def _on_facade_notification(self, note: "AppNotification") -> None:
        if note.event in ("task:started", "task:completed", "task:failed", "task:cancelled", "task:updated"):
            if self._session_uid is not None:
                sid = note.payload.get("session_uid") or note.payload.get("session_id")
                if sid is not None and str(sid) != self._session_uid:
                    return
            self.refresh()

    def closeEvent(self, event):  # type: ignore[override]
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        super().closeEvent(event)
