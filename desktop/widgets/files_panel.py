from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.services.session_files_service import FilesServiceError, RevisionConflictError
from session import session_runtime_uid
from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from session import Session


class FilesPanelWidget(QWidget):
    """Session file browser/editor for Desktop, backed by the shared files service."""

    def __init__(self, facade: ApplicationFacade, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.facade = facade
        self._session_uid = ""
        self._current_dir = "."
        self._selected_path = ""
        self._open_path = ""
        self._open_revision: Optional[str] = None
        self._build_ui()
        self._set_enabled_state(False)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.execution_banner = QLabel("Execution Target: Local")
        self.execution_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.execution_banner.setStyleSheet(
            "font-weight: bold; padding: 8px; background-color: #444; border-radius: 4px;"
        )
        layout.addWidget(self.execution_banner)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)

        self.up_button = QPushButton("Up")
        self.up_button.clicked.connect(self.go_up)
        toolbar.addWidget(self.up_button)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)

        self.create_file_button = QPushButton("New file")
        self.create_file_button.clicked.connect(lambda: self.create_path("file"))
        toolbar.addWidget(self.create_file_button)

        self.create_dir_button = QPushButton("New dir")
        self.create_dir_button.clicked.connect(lambda: self.create_path("dir"))
        toolbar.addWidget(self.create_dir_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_selected)
        toolbar.addWidget(self.delete_button)

        toolbar.addStretch(1)

        self.reload_file_button = QPushButton("Reload")
        self.reload_file_button.clicked.connect(self.reload_open_file)
        toolbar.addWidget(self.reload_file_button)

        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(lambda: self.save_open_file(force=False))
        toolbar.addWidget(self.save_button)

        self.force_save_button = QPushButton("Force save")
        self.force_save_button.clicked.connect(lambda: self.save_open_file(force=True))
        self.force_save_button.hide()
        toolbar.addWidget(self.force_save_button)

        layout.addLayout(toolbar)

        self.path_label = QLabel("Path: .")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.file_list = QListWidget()
        self.file_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.file_list.itemActivated.connect(self._on_item_activated)
        splitter.addWidget(self.file_list)

        editor_box = QWidget()
        editor_layout = QVBoxLayout(editor_box)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        self.editor_path_label = QLabel("No file selected")
        self.editor_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        editor_layout.addWidget(self.editor_path_label)
        self.editor_meta_label = QLabel("")
        self.editor_meta_label.setWordWrap(True)
        self.editor_meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        editor_layout.addWidget(self.editor_meta_label)
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor_layout.addWidget(self.editor, 1)
        splitter.addWidget(editor_box)
        splitter.setSizes([320, 680])
        layout.addWidget(splitter, 1)

    def set_session(self, session: Optional[Session]) -> None:
        session_uid = ""
        if session is not None:
            session_uid = str(session_runtime_uid(session) or "").strip()
        if session_uid == self._session_uid:
            return
        self._session_uid = session_uid
        self._current_dir = "."
        self._selected_path = ""
        self._clear_open_file()
        self._set_enabled_state(bool(session_uid))
        if session_uid:
            self.refresh()
        else:
            self.file_list.clear()
            self.path_label.setText("Path: .")
            self.status_label.setText("Select a session in Chat first.")

    def refresh(self, checked: bool = False) -> None:
        _ = checked
        self.load_tree(self._current_dir)

    def load_tree(self, path: str = ".") -> None:
        if not self._session_uid:
            self.status_label.setText("Select a session in Chat first.")
            return

        async def _load() -> None:
            self.status_label.setText("Loading...")
            try:
                ctx = await self.facade.files_execution_context(self._session_uid)
                result = await self.facade.files_tree(self._session_uid, path or ".")
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(f"Failed to load files: {exc}")
                return

            self._current_dir = str(result.get("path") or ".")
            target = str(ctx.get("execution_target") or "local")
            if target == "remote":
                host = str(ctx.get("host_alias") or "-")
                root = str(ctx.get("remote_project_root") or "-")
                self.execution_banner.setText(f"Execution Target: Remote ({host}: {root})")
            else:
                self.execution_banner.setText("Execution Target: Local")
            self._render_tree(result)
            self.status_label.setText("")

        ensure_async(_load(), parent=self)

    def go_up(self) -> None:
        if self._current_dir in ("", "."):
            return
        parent = os.path.dirname(self._current_dir.rstrip("/")) or "."
        self.load_tree(parent)

    def reload_open_file(self) -> None:
        if self._open_path:
            self.open_file(self._open_path)

    def open_file(self, path: str) -> None:
        if not self._session_uid:
            self.status_label.setText("Select a session in Chat first.")
            return

        async def _open() -> None:
            self.status_label.setText("Loading file...")
            try:
                result = await self.facade.files_read(self._session_uid, path)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(f"Failed to read file: {exc}")
                return

            self._open_path = str(path)
            self._open_revision = str(result.get("revision") or "")
            self.editor.setPlainText(str(result.get("content") or ""))
            self.editor.setEnabled(True)
            self.editor_path_label.setText(self._open_path)
            self.editor_meta_label.setText(self._format_meta(result.get("meta") or {}))
            self.force_save_button.hide()
            self.save_button.show()
            self.status_label.setText("")

        ensure_async(_open(), parent=self)

    def save_open_file(self, *, force: bool = False) -> None:
        if not self._open_path:
            return
        if not self._session_uid:
            self.status_label.setText("Select a session in Chat first.")
            return

        async def _save() -> None:
            self.status_label.setText("Saving...")
            try:
                result = await self.facade.files_write(
                    self._session_uid,
                    self._open_path,
                    self.editor.toPlainText(),
                    self._open_revision,
                    force=force,
                )
            except RevisionConflictError as exc:
                self.force_save_button.show()
                self.save_button.hide()
                self.status_label.setText(
                    f"Revision conflict. Current revision: {exc.current_revision or '-'}."
                )
                return
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(f"Failed to save file: {exc}")
                return

            self._open_revision = str(result.get("revision") or "")
            self.force_save_button.hide()
            self.save_button.show()
            self.status_label.setText("Saved.")
            self.refresh()

        ensure_async(_save(), parent=self)

    def create_path(self, kind: str) -> None:
        if not self._session_uid:
            self.status_label.setText("Select a session in Chat first.")
            return
        title = "New file" if kind == "file" else "New directory"
        name, ok = QInputDialog.getText(self, title, "Name:")
        if not ok or not str(name or "").strip():
            return
        rel_path = self._join_current(str(name).strip())

        async def _create() -> None:
            try:
                await self.facade.files_create(self._session_uid, rel_path, kind)
                self.load_tree(self._current_dir)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
            except Exception as exc:
                self.status_label.setText(f"Failed to create path: {exc}")

        ensure_async(_create(), parent=self)

    def delete_selected(self) -> None:
        if not self._selected_path:
            return
        reply = QMessageBox.question(
            self,
            "Delete",
            f"Delete '{self._selected_path}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        async def _delete() -> None:
            try:
                await self.facade.files_delete(self._session_uid, self._selected_path)
                if self._open_path == self._selected_path:
                    self._clear_open_file()
                self.load_tree(self._current_dir)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
            except Exception as exc:
                self.status_label.setText(f"Failed to delete path: {exc}")

        ensure_async(_delete(), parent=self)

    def _render_tree(self, result: dict[str, Any]) -> None:
        self.file_list.clear()
        self._selected_path = ""
        self.path_label.setText(f"Path: {self._current_dir}")
        for item in list(result.get("items") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            path = str(item.get("path") or name)
            is_dir = bool(item.get("is_dir"))
            prefix = "[D]" if is_dir else "[F]"
            row = QListWidgetItem(f"{prefix} {name}")
            row.setData(Qt.ItemDataRole.UserRole, {"path": path, "is_dir": is_dir})
            self.file_list.addItem(row)

    def _on_selection_changed(self) -> None:
        items = self.file_list.selectedItems()
        if not items:
            self._selected_path = ""
            return
        data = items[0].data(Qt.ItemDataRole.UserRole) or {}
        self._selected_path = str(data.get("path") or "")

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        path = str(data.get("path") or "")
        if not path:
            return
        if bool(data.get("is_dir")):
            self.load_tree(path)
        else:
            self.open_file(path)

    def _clear_open_file(self) -> None:
        self._open_path = ""
        self._open_revision = None
        self.editor.clear()
        self.editor.setEnabled(False)
        self.editor_path_label.setText("No file selected")
        self.editor_meta_label.clear()
        self.force_save_button.hide()
        self.save_button.show()

    def _set_enabled_state(self, enabled: bool) -> None:
        for widget in (
            self.up_button,
            self.refresh_button,
            self.create_file_button,
            self.create_dir_button,
            self.delete_button,
            self.reload_file_button,
            self.save_button,
            self.force_save_button,
            self.file_list,
            self.editor,
        ):
            widget.setEnabled(enabled)

    def _join_current(self, name: str) -> str:
        if self._current_dir in ("", "."):
            return name
        return f"{self._current_dir.rstrip('/')}/{name}"

    @staticmethod
    def _format_meta(meta: Any) -> str:
        if not isinstance(meta, dict):
            return ""
        parts = []
        for key in ("path", "size", "mtime"):
            value = meta.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        return " | ".join(parts)
