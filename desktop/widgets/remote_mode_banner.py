"""Remote mode banner and conflict diff dialog for Desktop parity with MiniApp."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class RemoteModeBanner(QWidget):
    """Banner showing 'Remote FS · <host> · <root>' or 'Execution Target: Local'.

    Used in Desktop panels (git, files) to indicate remote mode,
    mirroring the MiniApp Files/Editor banner.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel("Execution Target: Local")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "font-weight: bold; padding: 6px; background-color: #444; border-radius: 4px;"
        )
        layout.addWidget(self._label)

    def update_state(
        self,
        execution_target: str,
        host_alias: Optional[str] = None,
        remote_project_root: Optional[str] = None,
    ) -> None:
        if execution_target == "remote" and host_alias:
            root = remote_project_root or "/"
            self._label.setText(f"Remote FS \u00b7 {host_alias} \u00b7 {root}")
            self._label.setStyleSheet(
                "font-weight: bold; padding: 6px;"
                " background-color: #005A9E; color: white; border-radius: 4px;"
            )
        else:
            self._label.setText("Execution Target: Local")
            self._label.setStyleSheet(
                "font-weight: bold; padding: 6px; background-color: #444; border-radius: 4px;"
            )

    @property
    def text(self) -> str:
        return self._label.text()


class ConflictDiffDialog(QDialog):
    """Dialog showing unified diff when a file write conflict occurs.

    Provides Force Save and Cancel actions, mirroring the MiniApp conflict
    resolution flow.
    """

    def __init__(
        self,
        path: str,
        expected_revision: str,
        current_revision: str,
        diff_unified: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"File Conflict: {path}")
        self.setMinimumSize(600, 400)

        self._force_accepted = False

        layout = QVBoxLayout(self)

        info = QLabel(
            f"<b>Conflict detected</b> for <code>{path}</code><br>"
            f"Expected revision: <code>{expected_revision[:12]}...</code><br>"
            f"Current revision: <code>{current_revision[:12]}...</code>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        diff_view = QPlainTextEdit()
        diff_view.setPlainText(diff_unified or "(no diff available)")
        diff_view.setReadOnly(True)
        layout.addWidget(diff_view)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        force_btn = QPushButton("Force Save")
        force_btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold;")
        force_btn.clicked.connect(self._on_force)

        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        btn_row.addWidget(force_btn)
        layout.addLayout(btn_row)

    def _on_force(self) -> None:
        reply = QMessageBox.warning(
            self,
            "Confirm Force Save",
            "This will overwrite the file on the server.\nAre you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._force_accepted = True
            self.accept()

    @property
    def force_accepted(self) -> bool:
        return self._force_accepted
