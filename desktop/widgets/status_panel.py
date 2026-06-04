from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from desktop.widgets.mode_panel import ModePanelWidget
from desktop.widgets.run_operations_panel import RunOperationsPanelWidget
from desktop.widgets.task_progress import TaskProgressWidget
from desktop.widgets.task_queue import TaskQueueWidget
from i18n import t

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from session import Session


class StatusPanelWidget(QWidget):
    """Top-level Desktop status view matching the MiniApp status surface."""

    def __init__(self, facade: ApplicationFacade, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.facade = facade
        self._session_uid = ""
        self._build_ui()

    def _build_ui(self) -> None:
        lang = self.facade.ui_language
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.mode_label = QLabel(t("desktop.statuspanel.mode", lang))
        layout.addWidget(self.mode_label)
        self.mode_panel = ModePanelWidget(self.facade)
        layout.addWidget(self.mode_panel)

        self.progress_label = QLabel(t("desktop.statuspanel.progress", lang))
        layout.addWidget(self.progress_label)
        self.task_progress = TaskProgressWidget(self.facade)
        layout.addWidget(self.task_progress)

        self.queue_label = QLabel(t("desktop.statuspanel.queue", lang))
        layout.addWidget(self.queue_label)
        self.task_queue = TaskQueueWidget(self.facade)
        layout.addWidget(self.task_queue, 1)

        self.runs_label = QLabel(t("desktop.btn.runs", lang))
        layout.addWidget(self.runs_label)
        self.run_operations = RunOperationsPanelWidget(self.facade)
        layout.addWidget(self.run_operations, 1)

    def set_session(self, session: Optional[Session], session_uid: str = "") -> None:
        token = str(session_uid or "").strip()
        self._session_uid = token
        self.mode_panel.set_session(session)
        self.task_progress.set_session_id(token)
        self.task_queue.set_session_id(token)
        self.run_operations.set_session_id(token)

    def refresh_mode(self, session: Optional[Session]) -> None:
        self.mode_panel.set_session(session)

    def retranslate_ui(self, lang: str) -> None:
        self.mode_label.setText(t("desktop.statuspanel.mode", lang))
        self.progress_label.setText(t("desktop.statuspanel.progress", lang))
        self.queue_label.setText(t("desktop.statuspanel.queue", lang))
        self.runs_label.setText(t("desktop.btn.runs", lang))
        for child in (self.mode_panel, self.task_progress, self.task_queue, self.run_operations):
            if hasattr(child, "retranslate_ui"):
                child.retranslate_ui(lang)
