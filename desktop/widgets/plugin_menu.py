from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, List, Dict, Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QFrame,
    QLabel, QScrollArea, QGroupBox
)

from utils.ui import ensure_async

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade


class PluginMenuWidget(QWidget):
    """Виджет для отображения и управления плагинами агента."""

    def __init__(self, facade: ApplicationFacade, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.facade = facade
        self.logger = logging.getLogger(__name__)
        self._active_session_id: Optional[str] = None

        self._setup_ui()
        self.refresh_plugins()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Заголовок
        title = QLabel("Plugins")
        title.setObjectName("plugin_menu_title")
        layout.addWidget(title)

        # Кнопка обновления
        self.refresh_btn = QPushButton("Refresh Plugins")
        self.refresh_btn.clicked.connect(self.refresh_plugins)
        layout.addWidget(self.refresh_btn)

        # Область прокрутки для списка плагинов
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.plugins_container = QWidget()
        self.plugins_layout = QVBoxLayout(self.plugins_container)
        self.plugins_layout.setContentsMargins(0, 0, 0, 0)
        self.plugins_layout.setSpacing(8)

        self.scroll_area.setWidget(self.plugins_container)
        layout.addWidget(self.scroll_area)

        # Добавляем растягивающий элемент в конец
        layout.addStretch()

    def set_session(self, session_id: Optional[str]):
        """Устанавливает активную сессию."""
        self._active_session_id = session_id
        self.refresh_plugins()

    def refresh_plugins(self):
        """Обновляет список плагинов."""
        # Очищаем текущий список
        while self.plugins_layout.count():
            item = self.plugins_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._active_session_id:
            return

        # Получаем информацию о плагинах через facade
        try:
            # Получаем профиль для текущей сессии
            session = self.facade.session_service.get_session_by_uid(self._active_session_id)
            if not session:
                return

            # Получаем информацию о плагинах
            plugin_info = self.facade.get_plugin_ui(["All"])

            # Отображаем меню плагинов
            self._display_plugin_menu(plugin_info.get("plugin_menu", []))

        except Exception as e:
            self.logger.exception("Failed to refresh plugins")
            error_label = QLabel(f"Error loading plugins: {str(e)}")
            error_label.setStyleSheet("color: red;")
            self.plugins_layout.addWidget(error_label)

    def _display_plugin_menu(self, plugin_menu: List[Dict[str, Any]]):
        """Отображает двухуровневое меню плагинов."""
        if not plugin_menu:
            no_plugins_label = QLabel("No plugins available")
            no_plugins_label.setStyleSheet("color: gray;")
            self.plugins_layout.addWidget(no_plugins_label)
            return

        for plugin_info in plugin_menu:
            plugin_id = plugin_info.get("plugin_id", "")
            label = plugin_info.get("label", "")
            actions = plugin_info.get("actions", [])

            # Группа для плагина
            group_box = QGroupBox(label)
            group_layout = QVBoxLayout(group_box)

            if actions:
                for action in actions:
                    action_label = action.get("label", "")
                    action_name = action.get("action", "")

                    action_btn = QPushButton(action_label)
                    action_btn.clicked.connect(
                        lambda _, pid=plugin_id, an=action_name: self._on_plugin_action_clicked(pid, an)
                    )
                    group_layout.addWidget(action_btn)
            else:
                no_actions_label = QLabel("No actions available")
                no_actions_label.setStyleSheet("color: gray; font-style: italic;")
                group_layout.addWidget(no_actions_label)

            self.plugins_layout.addWidget(group_box)

    def _on_plugin_action_clicked(self, plugin_id: str, action: str):
        """Обработка клика по действию плагина."""
        if not self._active_session_id:
            return

        # Отправляем команду плагину через facade
        async def _execute():
            try:
                queued = await self.facade.try_queue_busy_input(
                    1,  # chat_id
                    self._active_session_id,
                    f"/plugin_action {plugin_id} {action}",
                )
                if queued:
                    return
                # Здесь мы должны вызвать соответствующий обработчик действия плагина
                # Пока просто отправляем сообщение с командой действия
                await self.facade.run_session_input(
                    self._active_session_id,
                    f"/plugin_action {plugin_id} {action}",
                )
            except Exception:
                self.logger.exception("Failed to execute plugin action")

        ensure_async(_execute(), parent=self)
