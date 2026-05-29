from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QFrame

from utils.ui import ensure_async


class ModeMenuWidget(QWidget):
    visibilityChanged = Signal(bool)

    """
    Desktop renderer for mode menus that are normally delivered via Telegram inline keyboards.

    Expects `ui:mode_menu` events from ApplicationFacade with payload:
      - session_id: str
      - text: str
      - rows: list[list[{"text": str, "data": str}]]
    """

    def __init__(self, facade: Any, actor_id: str = "desktop", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.facade = facade
        self.session_uid = str(actor_id)
        self.logger = logging.getLogger(__name__)
        self._active_session_id: Optional[str] = None

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(8)

        self.title = QLabel("Mode Menu")
        self.title.setObjectName("mode_menu_title")
        self._layout.addWidget(self.title)

        self.text = QLabel("")
        self.text.setObjectName("mode_menu_text")
        self.text.setWordWrap(True)
        self._layout.addWidget(self.text)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._layout.addWidget(sep)

        self._buttons_container = QWidget()
        self._buttons_layout = QVBoxLayout(self._buttons_container)
        self._buttons_layout.setContentsMargins(0, 0, 0, 0)
        self._buttons_layout.setSpacing(6)
        self._layout.addWidget(self._buttons_container)

        self.setVisible(False)
        self._unsubscribe = self.facade.subscribe(self._on_note)

    def set_session(self, session_id: Optional[str]) -> None:
        self._active_session_id = str(session_id) if session_id else None
        self.clear()

    def clear(self) -> None:
        self.text.setText("")
        while self._buttons_layout.count():
            item = self._buttons_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.setVisible(False)
        self.visibilityChanged.emit(False)

    def _on_note(self, note: Any) -> None:
        if note.event != "ui:mode_menu":
            return
        payload = note.payload or {}
        session_id = payload.get("session_id") or payload.get("session_uid")
        if not session_id or str(session_id) != str(self._active_session_id or ""):
            return
        text = str(payload.get("text") or "")
        rows = payload.get("rows") or []
        if not text and not rows:
            self.clear()
            return
        self._render(text, rows)

    def _render(self, text: str, rows: list) -> None:
        self.text.setText(text)
        while self._buttons_layout.count():
            item = self._buttons_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for row in rows:
            if not isinstance(row, list):
                continue
            row_widget = QWidget()
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)
            for btn in row:
                if not isinstance(btn, dict):
                    continue
                label = str(btn.get("text") or "").strip()
                data = str(btn.get("data") or "").strip()
                if not label or not data:
                    continue
                qbtn = QPushButton(label)
                qbtn.setObjectName("mode_menu_button")

                def _mk(cb_data: str):
                    def _h():
                        ensure_async(self.facade.handle_mode_callback(self.session_uid, data=cb_data), parent=self)

                    return _h

                qbtn.clicked.connect(_mk(data))
                row_layout.addWidget(qbtn)
            self._buttons_layout.addWidget(row_widget)

        self.setVisible(True)
        self.visibilityChanged.emit(True)

    def closeEvent(self, event):
        try:
            self._unsubscribe()
        except Exception:
            pass
        super().closeEvent(event)
