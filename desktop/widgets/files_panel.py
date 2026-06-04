from __future__ import annotations

import logging
import os
from typing import Any, TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.services.session_files_service import FilesServiceError, RevisionConflictError
from i18n import t
from session import session_runtime_uid
from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from session import Session

logger = logging.getLogger(__name__)


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

        lang = self.facade.ui_language
        self.execution_banner = QLabel(t("desktop.files.exec_target_local", lang))
        self.execution_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.execution_banner.setStyleSheet(
            "font-weight: bold; padding: 8px; background-color: #444; border-radius: 4px;"
        )
        layout.addWidget(self.execution_banner)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)

        self.up_button = QPushButton(t("desktop.btn.up", lang))
        self.up_button.clicked.connect(self.go_up)
        toolbar.addWidget(self.up_button)

        self.refresh_button = QPushButton(t("desktop.btn.refresh", lang))
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)

        self.create_file_button = QPushButton(t("desktop.btn.new_file", lang))
        self.create_file_button.clicked.connect(lambda: self.create_path("file"))
        toolbar.addWidget(self.create_file_button)

        self.create_dir_button = QPushButton(t("desktop.btn.new_dir", lang))
        self.create_dir_button.clicked.connect(lambda: self.create_path("dir"))
        toolbar.addWidget(self.create_dir_button)

        self.delete_button = QPushButton(t("desktop.btn.delete", lang))
        self.delete_button.clicked.connect(self.delete_selected)
        toolbar.addWidget(self.delete_button)

        self.rename_button = QPushButton(t("desktop.btn.rename", lang))
        self.rename_button.clicked.connect(self.rename_selected)
        toolbar.addWidget(self.rename_button)

        self.upload_button = QPushButton(t("desktop.btn.upload", lang))
        self.upload_button.clicked.connect(self.upload_file)
        toolbar.addWidget(self.upload_button)

        toolbar.addStretch(1)

        self.reload_file_button = QPushButton(t("desktop.btn.reload", lang))
        self.reload_file_button.clicked.connect(self.reload_open_file)
        toolbar.addWidget(self.reload_file_button)

        self.save_button = QPushButton(t("desktop.btn.save", lang))
        self.save_button.clicked.connect(lambda: self.save_open_file(force=False))
        toolbar.addWidget(self.save_button)

        self.force_save_button = QPushButton(t("desktop.btn.force_save", lang))
        self.force_save_button.clicked.connect(lambda: self.save_open_file(force=True))
        self.force_save_button.hide()
        toolbar.addWidget(self.force_save_button)

        layout.addLayout(toolbar)

        self.path_label = QLabel(t("desktop.files.path_prefix", lang, path="."))
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
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._on_context_menu)
        splitter.addWidget(self.file_list)

        editor_box = QWidget()
        editor_layout = QVBoxLayout(editor_box)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        self.editor_path_label = QLabel(t("desktop.files.no_file", lang))
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
            lang = self.facade.ui_language
            self.path_label.setText(t("desktop.files.path_prefix", lang, path="."))
            self.status_label.setText(t("desktop.files.select_session", lang))

    def refresh(self, checked: bool = False) -> None:
        _ = checked
        self.load_tree(self._current_dir)

    def load_tree(self, path: str = ".") -> None:
        if not self._session_uid:
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return

        async def _load() -> None:
            lang = self.facade.ui_language
            self.status_label.setText(t("desktop.files.loading", lang))
            try:
                ctx = await self.facade.files_execution_context(self._session_uid)
                result = await self.facade.files_tree(self._session_uid, path or ".")
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(t("desktop.files.load_error", lang, error=str(exc)))
                return

            self._current_dir = str(result.get("path") or ".")
            target = str(ctx.get("execution_target") or "local")
            if target == "remote":
                host = str(ctx.get("host_alias") or "-")
                root = str(ctx.get("remote_project_root") or "-")
                self.execution_banner.setText(t("desktop.files.exec_target_remote", lang, host=host, root=root))
            else:
                self.execution_banner.setText(t("desktop.files.exec_target_local", lang))
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
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return

        async def _open() -> None:
            lang = self.facade.ui_language
            self.status_label.setText(t("desktop.files.loading", lang))
            try:
                result = await self.facade.files_read(self._session_uid, path)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(t("desktop.files.read_error", lang, error=str(exc)))
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
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return

        async def _save() -> None:
            lang = self.facade.ui_language
            self.status_label.setText(t("desktop.files.saving", lang))
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
                    t("desktop.files.revision_conflict", lang, revision=str(exc.current_revision or "-"))
                )
                return
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(t("desktop.files.save_error", lang, error=str(exc)))
                return

            self._open_revision = str(result.get("revision") or "")
            self.force_save_button.hide()
            self.save_button.show()
            self.status_label.setText(t("desktop.files.saved", lang))
            self.refresh()

        ensure_async(_save(), parent=self)

    def create_path(self, kind: str) -> None:
        if not self._session_uid:
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return
        lang = self.facade.ui_language
        title = t("desktop.btn.new_file", lang) if kind == "file" else t("desktop.files.new_dir_title", lang)
        name, ok = QInputDialog.getText(self, title, t("desktop.files.name_label", lang))
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
                self.status_label.setText(t("desktop.files.create_error", self.facade.ui_language, error=str(exc)))

        ensure_async(_create(), parent=self)

    def delete_selected(self) -> None:
        if not self._selected_path:
            return
        lang = self.facade.ui_language

        # Determine if the selected item is a directory from list widget data
        selected_is_dir = False
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item is None:
                continue
            d = item.data(Qt.ItemDataRole.UserRole) or {}
            if str(d.get("path") or "") == self._selected_path:
                selected_is_dir = bool(d.get("is_dir"))
                break

        reply = QMessageBox.question(
            self,
            t("desktop.btn.delete", lang),
            t("desktop.files.delete_confirm", lang, path=self._selected_path),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        recursive = False
        if selected_is_dir:
            # Ask for recursive confirmation only for non-empty directories
            recursive_reply = QMessageBox.warning(
                self,
                t("desktop.files.delete_recursive_title", lang),
                t("desktop.files.delete_recursive_confirm", lang, path=self._selected_path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if recursive_reply != QMessageBox.StandardButton.Yes:
                return
            recursive = True

        async def _delete() -> None:
            try:
                await self.facade.files_delete(self._session_uid, self._selected_path, recursive=recursive)
                if self._open_path == self._selected_path:
                    self._clear_open_file()
                self.load_tree(self._current_dir)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
            except Exception as exc:
                self.status_label.setText(t("desktop.files.delete_error", self.facade.ui_language, error=str(exc)))

        ensure_async(_delete(), parent=self)

    def rename_selected(self) -> None:
        """Prompt for a new basename and rename the selected file or directory."""
        if not self._selected_path:
            return
        if not self._session_uid:
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return
        lang = self.facade.ui_language
        new_name, ok = QInputDialog.getText(
            self,
            t("desktop.files.rename_title", lang),
            t("desktop.files.rename_label", lang),
            text=os.path.basename(self._selected_path),
        )
        if not ok or not str(new_name or "").strip():
            return

        async def _rename() -> None:
            lang_ = self.facade.ui_language
            self.status_label.setText(t("desktop.files.renaming", lang_))
            try:
                result = await self.facade.files_rename(self._session_uid, self._selected_path, new_name.strip())
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(t("desktop.files.rename_error", lang_, error=str(exc)))
                return
            if not result.get("ok"):
                self.status_label.setText(t("desktop.files.rename_error", lang_, error=str(result.get("error") or "")))
                return
            logger.info("files_panel: renamed %s -> %s", self._selected_path, new_name)
            if self._open_path == self._selected_path:
                # Update open-path tracking after rename
                new_path = os.path.join(os.path.dirname(self._selected_path), result["new_name"])
                self._open_path = new_path
                self.editor_path_label.setText(self._open_path)
            self.status_label.setText(t("desktop.files.renamed", lang_, name=result["new_name"]))
            self.load_tree(self._current_dir)

        ensure_async(_rename(), parent=self)

    def upload_file(self) -> None:
        """Open a file picker and copy the chosen file into the current session directory."""
        if not self._session_uid:
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return
        lang = self.facade.ui_language
        src_path, _ = QFileDialog.getOpenFileName(
            self,
            t("desktop.files.upload_title", lang),
            "",
            t("desktop.files.all_files_filter", lang),
        )
        if not src_path:
            return

        async def _upload() -> None:
            lang_ = self.facade.ui_language
            self.status_label.setText(t("desktop.files.uploading", lang_))
            try:
                result = await self.facade.files_upload(self._session_uid, self._current_dir, src_path)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(t("desktop.files.upload_error", lang_, error=str(exc)))
                return
            if not result.get("ok"):
                self.status_label.setText(t("desktop.files.upload_error", lang_, error=str(result.get("error") or "")))
                return
            logger.info("files_panel: uploaded %s -> %s", src_path, result.get("rel_path"))
            self.status_label.setText(t("desktop.files.uploaded", lang_, filename=result.get("filename", "")))
            self.load_tree(self._current_dir)

        ensure_async(_upload(), parent=self)

    def download_selected(self) -> None:
        """Save the selected file to a local path chosen via QFileDialog."""
        if not self._selected_path:
            return
        if not self._session_uid:
            self.status_label.setText(t("desktop.files.select_session", self.facade.ui_language))
            return
        lang = self.facade.ui_language
        suggested = os.path.basename(self._selected_path)
        dst_path, _ = QFileDialog.getSaveFileName(
            self,
            t("desktop.files.download_title", lang),
            suggested,
            t("desktop.files.all_files_filter", lang),
        )
        if not dst_path:
            return

        async def _download() -> None:
            lang_ = self.facade.ui_language
            self.status_label.setText(t("desktop.files.downloading", lang_))
            try:
                result = await self.facade.files_download_bytes(self._session_uid, self._selected_path)
            except FilesServiceError as exc:
                self.status_label.setText(str(exc))
                return
            except Exception as exc:
                self.status_label.setText(t("desktop.files.download_error", lang_, error=str(exc)))
                return
            content = result.get("content") or b""
            if isinstance(content, str):
                content = content.encode("utf-8")
            try:
                with open(dst_path, "wb") as fh:
                    fh.write(content)
            except OSError as exc:
                logger.exception("files_panel: download write failed dst=%s", dst_path)
                self.status_label.setText(t("desktop.files.download_error", lang_, error=str(exc)))
                return
            logger.info("files_panel: downloaded %s -> %s", self._selected_path, dst_path)
            self.status_label.setText(t("desktop.files.downloaded", lang_, path=dst_path))

        ensure_async(_download(), parent=self)

    def _on_context_menu(self, pos: Any) -> None:
        """Show context menu with Download / Rename / Delete for the clicked item."""
        item = self.file_list.itemAt(pos)
        if item is None:
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        path = str(data.get("path") or "")
        is_dir = bool(data.get("is_dir"))
        if not path:
            return
        # Sync selected_path with the right-clicked item
        self._selected_path = path
        lang = self.facade.ui_language
        menu = QMenu(self)
        if not is_dir:
            dl_action = menu.addAction(t("desktop.btn.download", lang))
            dl_action.triggered.connect(self.download_selected)
        rename_action = menu.addAction(t("desktop.btn.rename", lang))
        rename_action.triggered.connect(self.rename_selected)
        menu.addSeparator()
        del_action = menu.addAction(t("desktop.btn.delete", lang))
        del_action.triggered.connect(self.delete_selected)
        menu.exec(self.file_list.viewport().mapToGlobal(pos))

    def _render_tree(self, result: dict[str, Any]) -> None:
        self.file_list.clear()
        self._selected_path = ""
        self.path_label.setText(t("desktop.files.path_prefix", self.facade.ui_language, path=self._current_dir))
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
        self.editor_path_label.setText(t("desktop.files.no_file", self.facade.ui_language))
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
            self.rename_button,
            self.upload_button,
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

    def retranslate_ui(self, lang: str) -> None:
        self.up_button.setText(t("desktop.btn.up", lang))
        self.refresh_button.setText(t("desktop.btn.refresh", lang))
        self.create_file_button.setText(t("desktop.btn.new_file", lang))
        self.create_dir_button.setText(t("desktop.btn.new_dir", lang))
        self.delete_button.setText(t("desktop.btn.delete", lang))
        self.rename_button.setText(t("desktop.btn.rename", lang))
        self.upload_button.setText(t("desktop.btn.upload", lang))
        self.reload_file_button.setText(t("desktop.btn.reload", lang))
        self.save_button.setText(t("desktop.btn.save", lang))
        self.force_save_button.setText(t("desktop.btn.force_save", lang))
        # Перерисовываем префикс пути и для активной директории (не только пустой сессии)
        self.path_label.setText(t("desktop.files.path_prefix", lang, path=self._current_dir))
        if not self._session_uid:
            self.execution_banner.setText(t("desktop.files.exec_target_local", lang))
            self.status_label.setText(t("desktop.files.select_session", lang))
            self.editor_path_label.setText(t("desktop.files.no_file", lang))
        elif not self._open_path:
            self.editor_path_label.setText(t("desktop.files.no_file", lang))
