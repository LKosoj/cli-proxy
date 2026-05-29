from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional, List

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QComboBox, QLabel, QFrame, QPushButton,
    QVBoxLayout,
)
from PySide6.QtCore import Qt

from session import session_runtime_uid
from sessions.session_state_access import get_active_mode
from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade, AppNotification
    from session import Session


class ModePanelWidget(QWidget):
    """Улучшенная панель выбора режима и отображения статуса."""

    def __init__(
        self,
        facade: ApplicationFacade,
        actor_id: str = "desktop",
        chat_id: Optional[object] = None,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.facade = facade
        self.actor_id = actor_id
        self.logger = logging.getLogger(__name__)
        self._active_session: Optional[Session] = None
        self._status_history: List[str] = []
        self._menu_open: bool = False
        self.chat_id: Optional[object] = chat_id

        self._setup_ui()
        self.load_modes()

        # Подписка на уведомления фасада для обновления статуса
        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)

    def _schedule_async(self, coro_factory):
        """
        Планирует корутину только при наличии running loop.
        Важно: не создавать coroutine object, если loop отсутствует, чтобы избежать RuntimeWarning.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if not loop.is_running():
            return None
        return ensure_async(coro_factory(), parent=self)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        # Основная панель управления
        controls_layout = QHBoxLayout()

        # Режим
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(self.mode_combo, 1)
        controls_layout.addLayout(mode_layout)

        # CLI
        cli_layout = QHBoxLayout()
        # Текст ожидается в формате "CLI: <name>" (см. desktop tests).
        # Отдельную подпись не показываем, чтобы не дублировать "CLI:".
        cli_layout.addWidget(QLabel(""))
        self.cli_label = QLabel("CLI: None")
        self.cli_label.setObjectName("mode_panel_cli")
        cli_layout.addWidget(self.cli_label, 1)
        controls_layout.addLayout(cli_layout)

        # Кнопки управления
        btn_layout = QHBoxLayout()
        self.menu_btn = QPushButton("Menu")
        self.menu_btn.setEnabled(False)
        self.menu_btn.clicked.connect(self._request_mode_menu)
        btn_layout.addWidget(self.menu_btn)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_modes)
        btn_layout.addWidget(self.refresh_btn)

        controls_layout.addLayout(btn_layout)
        layout.addLayout(controls_layout)

        # Разделитель
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        # Статус
        status_layout = QHBoxLayout()
        self.status_icon = QLabel("●")
        self.status_icon.setObjectName("mode_panel_status_icon")
        status_layout.addWidget(self.status_icon)

        self.status_text = QLabel("Idle")
        self.status_text.setObjectName("mode_panel_status_text")
        self.status_text.setCursor(Qt.CursorShape.WhatsThisCursor)
        status_layout.addWidget(self.status_text, 1)

        self.history_btn = QPushButton("🕒")
        self.history_btn.setFixedSize(24, 24)
        self.history_btn.setToolTip("История статусов")
        self.history_btn.setStyleSheet("QPushButton { border: none; background: transparent; font-size: 14px; }")
        self.history_btn.clicked.connect(self._show_history)
        status_layout.addWidget(self.history_btn)

        layout.addLayout(status_layout)

        self.setEnabled(False)

    def _refresh_modes(self):
        """Обновляет список доступных режимов."""
        self.load_modes()

    def load_modes(self):
        """Загрузка списка доступных режимов."""
        modes = self.facade.list_modes()
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        self.mode_combo.addItem("None")
        if modes:
            self.mode_combo.addItems(modes)
        self.mode_combo.blockSignals(False)

    def set_session(self, session: Optional[Session]):
        """Установка активной сессии."""
        self._active_session = session
        self._menu_open = False
        if session:
            self.setEnabled(True)
            if self.chat_id is None:
                self.chat_id = getattr(session, "chat_id", None) or session_runtime_uid(session) or None
            self.mode_combo.blockSignals(True)
            active_mode = get_active_mode(session, None) or "None"
            index = self.mode_combo.findText(active_mode)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
            else:
                self.mode_combo.setCurrentText(active_mode)
            self.mode_combo.blockSignals(False)
            cli = (
                getattr(getattr(session, "cli", None), "active_cli", getattr(session, "active_cli", None))
                or "None"
            )
            self.cli_label.setText(f"CLI: {cli}")
            self._update_status("Working" if session.busy else "Idle", busy=session.busy)
            mode_id = str(get_active_mode(session, "") or "").strip()
            self.menu_btn.setEnabled(bool(mode_id))
        else:
            self.setEnabled(False)
            self.chat_id = None
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(0)
            self.mode_combo.blockSignals(False)
            self.cli_label.setText("CLI: None")
            self._update_status("Idle", busy=False)
            self.menu_btn.setEnabled(False)

    def _update_status(self, status: str, busy: bool = False):
        """Обновление индикатора статуса с цветовой кодировкой."""
        timestamp = time.strftime("%H:%M:%S")
        history_entry = f"[{timestamp}] {status}"
        self._status_history.append(history_entry)
        if len(self._status_history) > 20:
            self._status_history.pop(0)

        self.status_text.setText(status)
        self.status_text.setToolTip("\n".join(reversed(self._status_history)))

        colors = self.facade.theme_service.get_theme_colors()

        if busy:
            # Working / Started
            color = colors.get("warning", "#e67e22")
        elif status == "Idle":
            color = colors.get("success", "#2ecc71")
        elif status == "Failed":
            color = colors.get("danger", "#e74c3c")
        elif status == "Cancelled":
            color = colors.get("text_secondary", "#95a5a6")
        elif status == "Completed":
            color = colors.get("success", "#2ecc71")
        else:
            color = colors.get("text_secondary", "#bdc3c7")

        self.status_icon.setStyleSheet(f"color: {color};")
        self.status_text.setStyleSheet(f"font-weight: bold; color: {color};")

    def _show_history(self):
        """Показывает историю статусов в виде меню."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction

        menu = QMenu(self)
        if not self._status_history:
            menu.addAction("Нет истории")
        else:
            for entry in reversed(self._status_history):
                action = QAction(entry, menu)
                menu.addAction(action)
        menu.exec(self.history_btn.mapToGlobal(self.history_btn.rect().bottomLeft()))

    def _on_mode_changed(self, mode_id: str):
        """Обработка смены режима пользователем."""
        if not self._active_session:
            return

        target_mode = None if mode_id == "None" else mode_id
        session_uid = session_runtime_uid(self._active_session)

        async def _switch_mode() -> None:
            result = self.facade.set_session_mode_via_callback(session_uid, target_mode)
            if asyncio.iscoroutine(result):
                success = bool(await result)
            else:
                success = bool(result)
            if success:
                self.logger.info("Mode changed to %s for session %s", mode_id, session_uid)
                self._menu_open = False
                # При смене/выключении режима закрываем текущее mode-меню.
                self.facade.notify("ui:mode_menu", session_uid=session_uid, text="", rows=[])
            self.menu_btn.setEnabled(bool(target_mode))

        scheduled = self._schedule_async(_switch_mode)
        if scheduled is None:
            # Fallback for contexts without running event loop (e.g. certain unit tests).
            success = self.facade.set_session_mode(session_uid, target_mode)
            if success:
                self.logger.info("Mode changed to %s for session %s", mode_id, session_uid)
                self._menu_open = False
                self.facade.notify("ui:mode_menu", session_uid=session_uid, text="", rows=[])
            self.menu_btn.setEnabled(bool(target_mode))

    def _request_mode_menu(self):
        if not self._active_session:
            return
        session_uid = session_runtime_uid(self._active_session)
        if self._menu_open:
            self._menu_open = False
            self.facade.notify("ui:mode_menu", session_uid=session_uid, text="", rows=[])
            return
        self._schedule_async(lambda: self.facade.show_mode_menu(session_uid))

    def _on_facade_notification(self, note: AppNotification):
        """Обработка уведомлений от фасада."""
        if not self._active_session:
            return

        event = note.event
        payload = note.payload
        session_id = payload.get("session_id") or payload.get("session_uid")

        if session_id != session_runtime_uid(self._active_session):
            return

        if event == "ui:mode_menu":
            text = str(payload.get("text") or "")
            rows = payload.get("rows") or []
            self._menu_open = bool(text or rows)
            return

        if event == "task:started":
            self._update_status("Working", busy=True)
        elif event == "task:completed":
            active_tasks = self.facade.task_service.list_active(session_id=session_id)
            if not active_tasks:
                self._update_status("Completed")
                # Через 3 секунды возвращаем Idle, если ничего не изменилось

                async def _reset():
                    await asyncio.sleep(3)
                    if self._active_session and session_runtime_uid(self._active_session) == session_id:
                        if not self.facade.task_service.list_active(session_id=session_id):
                            self._update_status("Idle")
                self._schedule_async(_reset)
        elif event == "task:cancelled":
            self._update_status("Cancelled")
        elif event == "task:failed":
            self._update_status("Failed")

    def closeEvent(self, event):
        self._unsubscribe()
        super().closeEvent(event)
