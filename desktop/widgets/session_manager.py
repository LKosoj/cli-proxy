from __future__ import annotations

import logging
import asyncio
import os
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QLineEdit,
    QPushButton,
    QLabel,
    QMessageBox,
    QInputDialog,
    QFileDialog,
    QMenu,
    QToolButton,
    QDialog,
    QDialogButtonBox,
)

from i18n import t
from session import session_runtime_uid
from utils.ui import ensure_async, format_session_title

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from session import Session


class ProjectPickerDialog(QDialog):
    """Диалог выбора зарегистрированного проекта или открытия файлового менеджера."""

    BROWSE_SENTINEL = "__browse__"

    def __init__(self, projects: list[str], lang: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("desktop.sessmgr.pick_title", lang))
        self._chosen: str | None = None

        layout = QVBoxLayout(self)

        label = QLabel(t("desktop.sessmgr.pick_label", lang))
        layout.addWidget(label)

        self._list = QListWidget()
        for path in projects:
            item = QListWidgetItem(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self._accept_selection)
        layout.addWidget(self._list)

        btn_box = QDialogButtonBox()
        ok_btn = btn_box.addButton(QDialogButtonBox.StandardButton.Ok)
        browse_btn = btn_box.addButton(
            t("desktop.sessmgr.pick_browse", lang),
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        cancel_btn = btn_box.addButton(QDialogButtonBox.StandardButton.Cancel)

        ok_btn.clicked.connect(self._accept_selection)
        browse_btn.clicked.connect(self._accept_browse)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(btn_box)

    def _accept_selection(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self._chosen = str(item.data(Qt.ItemDataRole.UserRole))
        self.accept()

    def _accept_browse(self) -> None:
        self._chosen = self.BROWSE_SENTINEL
        self.accept()

    def chosen_path(self) -> str | None:
        """Выбранный путь или BROWSE_SENTINEL; None если отменено."""
        return self._chosen


# Module-level sentinel — used in _pick_workdir to avoid referencing the class
# through a potentially-mocked name.
_BROWSE_SENTINEL: str = ProjectPickerDialog.BROWSE_SENTINEL


class SessionItemWidget(QWidget):
    """Виджет для отображения элемента списка сессий."""

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self._session_uid = session_runtime_uid(session)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)

        name_label = QLabel(format_session_title(session))
        name_label.setObjectName("session_item_name")

        active_cli = getattr(getattr(session, "cli", None), "active_cli", getattr(session, "active_cli", None))
        info_label = QLabel(f"{active_cli or 'no-cli'} | {session.workdir}")
        info_label.setObjectName("session_item_info")

        layout.addWidget(name_label)
        layout.addWidget(info_label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Клик по элементу — выбрать его в родительском QListWidget."""
        super().mousePressEvent(event)
        w = self.parent()
        while w:
            if isinstance(w, QListWidget):
                for i in range(w.count()):
                    if w.itemWidget(w.item(i)) is self:
                        w.setCurrentRow(i)
                        break
                break
            w = w.parent()


class SessionManagerWidget(QWidget):
    """Виджет управления сессиями."""

    sessionSelected = Signal(str)  # Передает session_uid

    def __init__(
        self,
        facade: ApplicationFacade,
        actor_id: str = "desktop",
        logger: Optional[logging.Logger] = None
    ):
        super().__init__()
        self.facade = facade
        self.session_service = facade.session_service
        self.actor_id = actor_id
        self.logger = logger or logging.getLogger(__name__)
        self._background_tasks = set()

        self._setup_ui()
        self.refresh_sessions()
        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)

    def _on_facade_notification(self, note):
        if note.event == "ui:session_updated":
            self.refresh_sessions()
        elif note.event == "ui:session_transfer_offer":
            self._handle_transfer_offer(note)

    def closeEvent(self, event):
        if hasattr(self, "_unsubscribe"):
            self._unsubscribe()
        super().closeEvent(event)

    def _schedule_async(self, coro_factory):
        """Планирует корутину только при наличии активного event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if not loop.is_running():
            return None
        return ensure_async(coro_factory(), parent=self)

    def _setup_ui(self):
        self.setObjectName("session_manager")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        lang = self.facade.ui_language

        # Поиск
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t("desktop.session.search_placeholder", lang))
        self.search_input.textChanged.connect(self._filter_sessions)
        layout.addWidget(self.search_input)

        # Список сессий
        self.session_list = QListWidget()
        self.session_list.setSpacing(2)
        self.session_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.session_list)

        # Основные действия
        btn_layout = QHBoxLayout()

        self.btn_new = QPushButton(t("desktop.btn.new", lang))
        self.btn_new.clicked.connect(self._on_new_session)
        btn_layout.addWidget(self.btn_new)

        self.btn_actions = QToolButton()
        self.btn_actions.setText(t("desktop.btn.actions", lang))
        self.btn_actions.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_actions.setEnabled(False)
        btn_layout.addWidget(self.btn_actions)
        layout.addLayout(btn_layout)

        self._rebuild_actions_menu()

    def _rebuild_actions_menu(self) -> None:
        lang = self.facade.ui_language
        menu = QMenu(self)
        menu.addAction(t("desktop.btn.delete", lang), self._on_delete_session)
        menu.addAction(t("desktop.sessmgr.rename", lang), self._on_rename_session)
        menu.addAction(t("desktop.sessmgr.reset", lang), self._on_reset_session)
        menu.addSeparator()
        menu.addAction(t("desktop.session.switch_cli_title", lang), self._on_switch_cli)
        menu.addAction(t("desktop.sessmgr.resume_token", lang), self._on_resume_edit)
        self.btn_actions.setMenu(menu)

    def _show_context_menu(self, pos) -> None:
        item = self.session_list.itemAt(pos)
        if item is not None:
            self.session_list.setCurrentItem(item)
        lang = self.facade.ui_language
        menu = QMenu(self)
        menu.addAction(t("desktop.session.new_title", lang), self._on_new_session)
        if self.session_list.selectedItems():
            menu.addSeparator()
            menu.addAction(t("desktop.sessmgr.rename", lang), self._on_rename_session)
            menu.addAction(t("desktop.btn.delete", lang), self._on_delete_session)
            menu.addAction(t("desktop.sessmgr.reset", lang), self._on_reset_session)
            menu.addAction(t("desktop.session.switch_cli_title", lang), self._on_switch_cli)
            menu.addAction(t("desktop.sessmgr.resume_token", lang), self._on_resume_edit)
        menu.exec(self.session_list.mapToGlobal(pos))

    def refresh_sessions(self):
        """Обновление списка сессий из сервиса."""
        self.session_list.clear()
        sessions = self.session_service.list_desktop_sessions()
        active_id = None

        for sess in sessions:
            item = QListWidgetItem(self.session_list)
            item.setData(Qt.ItemDataRole.UserRole, session_runtime_uid(sess))

            widget = SessionItemWidget(sess)
            item.setSizeHint(widget.sizeHint())

            self.session_list.addItem(item)
            self.session_list.setItemWidget(item, widget)

            if session_runtime_uid(sess) == active_id:
                item.setSelected(True)

    def _filter_sessions(self, text: str):
        """Фильтрация списка по тексту."""
        text = text.lower()
        for i in range(self.session_list.count()):
            item = self.session_list.item(i)
            widget = self.session_list.itemWidget(item)
            if isinstance(widget, SessionItemWidget):
                # Ищем по имени, ID или рабочей директории
                sid = item.data(Qt.ItemDataRole.UserRole)
                session = self.session_service.get_session_by_uid(sid)
                if session:
                    session_uid = session_runtime_uid(session)
                    visible = (
                        text in session_uid.lower() or
                        text in session.id.lower() or
                        (session.name and text in session.name.lower()) or
                        text in session.workdir.lower()
                    )
                    item.setHidden(not visible)

    def restore_selection(self, session_uid: str) -> bool:
        """Восстанавливает выбор сессии при перезапуске. Возвращает True если сессия найдена."""
        for i in range(self.session_list.count()):
            item = self.session_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == session_uid:
                self.session_list.blockSignals(True)
                self.session_list.setCurrentItem(item)
                self.session_list.blockSignals(False)
                self.btn_actions.setEnabled(True)
                pass
                self.sessionSelected.emit(session_uid)
                if self.facade.ui_state_service:
                    self._schedule_async(
                        lambda: self.facade.ui_state_service.save(last_session_id=session_uid)
                    )
                return True
        return False

    def _on_selection_changed(self):
        items = self.session_list.selectedItems()
        if not items:
            self.btn_actions.setEnabled(False)
            return

        self.btn_actions.setEnabled(True)
        session_uid = items[0].data(Qt.ItemDataRole.UserRole)

        # Устанавливаем активную сессию в сервисе
        if True:
            self.sessionSelected.emit(session_uid)

            # Сохраняем в состояние UI
            if self.facade.ui_state_service:
                self._schedule_async(
                    lambda: self.facade.ui_state_service.save(last_session_id=session_uid)
                )

    def _registered_projects(self) -> list[str]:
        """Возвращает дедублицированный список существующих путей из config.telegram.user_workdirs."""
        try:
            cfg = getattr(self.facade.config_service, "config", None)
            tg = getattr(cfg, "telegram", None)
            raw: dict = getattr(tg, "user_workdirs", {}) or {}
        except Exception:
            return []
        seen: set[str] = set()
        result: list[str] = []
        for paths in raw.values():
            if isinstance(paths, str):
                paths = [paths]
            for p in (paths or []):
                sp = str(p or "").strip()
                if not sp:
                    continue
                rp = os.path.realpath(os.path.expanduser(sp))
                if rp in seen or not os.path.isdir(rp):
                    continue
                seen.add(rp)
                result.append(rp)
        return result

    def _pick_workdir(self, lang: str, root_dir: str) -> str | None:
        """Показывает диалог выбора рабочей директории.

        Если есть зарегистрированные проекты — сначала показывает список.
        Возвращает абсолютный путь или None если пользователь отменил.
        """
        projects = self._registered_projects()
        if projects:
            dlg = ProjectPickerDialog(projects, lang, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            chosen = dlg.chosen_path()
            if chosen is None:
                return None
            if chosen != _BROWSE_SENTINEL:
                return os.path.abspath(chosen)

        # Fallback: QFileDialog
        default_dir = os.path.abspath(os.path.expanduser(root_dir or os.getcwd()))
        workdir = QFileDialog.getExistingDirectory(
            self,
            t("desktop.sessmgr.select_workdir", lang),
            default_dir,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not workdir:
            return None
        workdir = os.path.abspath(os.path.expanduser(str(workdir)))

        if root_dir:
            root_abs = os.path.abspath(os.path.expanduser(root_dir))
            try:
                valid = os.path.commonpath([workdir, root_abs]) == root_abs
            except ValueError:
                valid = False
            if not valid:
                QMessageBox.warning(
                    self,
                    t("desktop.sessmgr.invalid_dir_title", lang),
                    t("desktop.sessmgr.invalid_dir_msg", lang, root=root_abs),
                )
                return None

        return workdir

    def _on_new_session(self):
        """Создание новой сессии."""
        # Выбор инструмента (CLI)
        tools = []
        if self.facade.config_service.config:
            tools = list(self.facade.config_service.config.tools.keys())

        lang = self.facade.ui_language
        if not tools:
            QMessageBox.warning(self, t("desktop.btn.actions", lang), t("desktop.msg.no_tools", lang))
            return

        tool, ok = QInputDialog.getItem(
            self, t("desktop.session.new_title", lang), t("desktop.session.select_tool", lang), tools, 0, False
        )
        if not ok or not tool:
            return

        root_dir = ""
        try:
            cfg = getattr(self.facade.config_service, "config", None)
            defaults = getattr(cfg, "defaults", None)
            root_dir = str(getattr(defaults, "workdir", "") or "").strip()
        except Exception:
            self.logger.exception("failed to read defaults.workdir for new session dialog")
            root_dir = ""

        workdir = self._pick_workdir(lang, root_dir)
        if workdir is None:
            return

        try:
            session = self.session_service.create_desktop_session(tool, workdir)
            self.refresh_sessions()
            # Select the new session
            for i in range(self.session_list.count()):
                item = self.session_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == session_runtime_uid(session):
                    item.setSelected(True)
                    break
        except Exception as e:
            lang = self.facade.ui_language
            QMessageBox.critical(
                self, t("desktop.btn.actions", lang),
                t("desktop.msg.session_create_failed", lang, error=str(e))
            )

    def start_new_session(self) -> None:
        """Публичная команда для command palette."""
        self._on_new_session()

    def _on_delete_session(self):
        items = self.session_list.selectedItems()
        if not items:
            return

        session_uid = items[0].data(Qt.ItemDataRole.UserRole)

        lang = self.facade.ui_language
        reply = QMessageBox.question(
            self, t("desktop.session.close_confirm_title", lang),
            t("desktop.session.close_confirm_msg", lang, uid=session_uid),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            async def _close():
                await self.session_service.close_session_by_uid(session_uid)
                self.refresh_sessions()

            self._schedule_async(_close)

    def _on_rename_session(self):
        items = self.session_list.selectedItems()
        if not items:
            return
        session_uid = items[0].data(Qt.ItemDataRole.UserRole)
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return

        lang = self.facade.ui_language
        new_name, ok = QInputDialog.getText(
            self, t("desktop.session.rename_title", lang), t("desktop.session.rename_prompt", lang),
            text=(session.name or session.id)
        )
        if ok:
            self.facade.rename_session(session_uid, new_name)
            self.refresh_sessions()

    def _on_reset_session(self):
        items = self.session_list.selectedItems()
        if not items:
            return
        session_uid = items[0].data(Qt.ItemDataRole.UserRole)

        lang = self.facade.ui_language
        reply = QMessageBox.question(
            self, t("desktop.session.close_confirm_title", lang),
            t("desktop.session.reset_confirm", lang, uid=session_uid),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.facade.reset_session(session_uid)
            self.refresh_sessions()

    def _handle_transfer_offer(self, note):
        """Show QMessageBox asking user to confirm session context transfer."""
        payload = dict(getattr(note, "payload", {}) or {})
        session_uid = str(payload.get("session_uid") or payload.get("session_id") or "")
        source_cli = str(payload.get("source_cli") or "")
        target_cli = str(payload.get("target_cli") or "")
        if not session_uid or not source_cli:
            return
        lang = self.facade.ui_language
        reply = QMessageBox.question(
            self,
            t("desktop.sessmgr.transfer_title", lang),
            t("desktop.sessmgr.transfer_msg", lang, source=source_cli, target=target_cli),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            result = self.facade.confirm_session_transfer(session_uid, source_cli)
            if not result:
                QMessageBox.warning(
                    self,
                    t("desktop.sessmgr.transfer_fail_title", lang),
                    t("desktop.sessmgr.transfer_fail_msg", lang),
                )

    def _on_switch_cli(self):
        items = self.session_list.selectedItems()
        if not items:
            return
        session_uid = items[0].data(Qt.ItemDataRole.UserRole)
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return

        tools = []
        if self.facade.config_service.config:
            tools = list(self.facade.config_service.config.tools.keys())

        if not tools:
            return

        current_tool = getattr(getattr(session, "cli", None), "active_cli", getattr(session, "active_cli", None))
        current_tool = current_tool or session.tool.name
        try:
            current_idx = tools.index(current_tool)
        except ValueError:
            current_idx = 0

        lang = self.facade.ui_language
        tool, ok = QInputDialog.getItem(
            self, t("desktop.session.switch_cli_title", lang),
            t("desktop.session.select_tool", lang), tools, current_idx, False
        )
        if ok and tool:
            self.facade.set_active_cli(session_uid, tool)
            self.refresh_sessions()

    def _on_resume_edit(self):
        items = self.session_list.selectedItems()
        if not items:
            return
        session_uid = items[0].data(Qt.ItemDataRole.UserRole)
        session = self.session_service.get_session_by_uid(session_uid)
        if not session:
            return

        current_resume = getattr(session, "resume_token", "") or ""
        lang = self.facade.ui_language
        new_resume, ok = QInputDialog.getText(
            self, t("desktop.session.resume_token_title", lang),
            t("desktop.session.rename_prompt", lang), text=str(current_resume)
        )
        if ok:
            session.resume_token = str(new_resume).strip() or None
            try:
                self.session_service._manager._persist_sessions()
            except Exception:
                pass
            self.refresh_sessions()

    def retranslate_ui(self, lang: str) -> None:
        self.search_input.setPlaceholderText(t("desktop.session.search_placeholder", lang))
        self.btn_new.setText(t("desktop.btn.new", lang))
        self.btn_actions.setText(t("desktop.btn.actions", lang))
        self._rebuild_actions_menu()
