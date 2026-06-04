from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Dict

from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
    QPushButton,
    QHBoxLayout,
    QSpinBox,
    QSizePolicy
)

from i18n import t

if TYPE_CHECKING:
    from desktop.services.application_facade import ApplicationFacade
    from desktop.services.application_facade import AppNotification as AppNotificationType


class TaskProgressWidget(QWidget):
    """Улучшенный виджет отображения прогресса активных задач."""

    def __init__(
        self,
        facade: ApplicationFacade,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.facade = facade
        self.logger = logging.getLogger(__name__)
        self._active_session_id: Optional[str] = None
        self._task_bars: Dict[str, QProgressBar] = {}
        self._task_labels: Dict[str, QLabel] = {}
        self._task_containers: Dict[str, QFrame] = {}
        self._task_buttons: Dict[str, QWidget] = {}  # Контейнер для кнопок управления задачей

        self._setup_ui()
        self.retranslate_ui(self.facade.ui_language)
        self._unsubscribe = self.facade.subscribe(self._on_facade_notification)

    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(4)

        self.title_label = QLabel()
        self.title_label.setObjectName("progress_title")
        self.main_layout.addWidget(self.title_label)
        self.title_label.hide()  # Hidden by default, shown when tasks exist

    def set_session_id(self, session_id: Optional[str]) -> None:
        """Обновление активной сессии и очистка старых индикаторов."""
        if self._active_session_id != session_id:
            self._active_session_id = session_id
            self._clear_all_tasks()

    def _clear_all_tasks(self) -> None:
        for task_id in list(self._task_containers.keys()):
            self._remove_task(task_id)

    def _on_facade_notification(self, note: AppNotificationType) -> None:
        event = note.event
        payload = note.payload

        # Фильтрация по сессии
        session_id = payload.get("session_id")
        if self._active_session_id and session_id and session_id != self._active_session_id:
            return

        task_id = payload.get("task_id")
        if not task_id:
            return

        if event == "task:started":
            name = payload.get("name", "Unknown Task")
            self._add_task(task_id, name)
        elif event == "task:updated":
            progress = payload.get("progress")
            stage = payload.get("stage")
            self._update_task(task_id, progress, stage)
        elif event in ("task:completed", "task:failed", "task:cancelled"):
            # В тестах и при закрытии окна delayed-removal приводит к обращению к уже удаленным Qt-объектам.
            # Поэтому сразу чистим внутренние структуры и удаляем виджет.
            self._remove_task_if_exists(task_id)

    def _add_task(self, task_id: str, name: str) -> None:
        if task_id in self._task_containers:
            return

        container = QFrame()
        container.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # Горизонтальный слой для информации о задаче и кнопок управления
        info_layout = QHBoxLayout()

        label = QLabel(f"{name}")
        label.setStyleSheet("font-size: 10px;")
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info_layout.addWidget(label)

        # Кнопки управления задачей
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(2)

        # Кнопка изменения приоритета
        priority_box = QSpinBox()
        priority_box.setRange(-10, 10)
        priority_box.setValue(0)
        priority_box.setSuffix(" Prio")
        priority_box.setMaximumWidth(80)
        priority_box.valueChanged.connect(lambda value: self._on_priority_changed(task_id, value))
        buttons_layout.addWidget(priority_box)

        # Кнопка отмены задачи
        cancel_btn = QPushButton("❌")
        cancel_btn.setFixedSize(20, 20)
        cancel_btn.setToolTip(t("desktop.taskprogress.cancel_tooltip", self.facade.ui_language))
        cancel_btn.clicked.connect(lambda: self._on_cancel_clicked(task_id))
        buttons_layout.addWidget(cancel_btn)

        info_layout.addWidget(buttons_widget)
        layout.addLayout(info_layout)

        bar = QProgressBar()
        bar.setFixedHeight(8)
        bar.setTextVisible(False)
        bar.setRange(0, 100)
        bar.setValue(0)
        layout.addWidget(bar)

        self._task_bars[task_id] = bar
        self._task_labels[task_id] = label
        self._task_containers[task_id] = container
        self._task_buttons[task_id] = buttons_widget
        self.main_layout.addWidget(container)

        self.title_label.show()

    def _update_task(self, task_id: str, progress: Optional[float], stage: Optional[str]) -> None:
        if task_id not in self._task_bars:
            return

        if progress is not None:
            val = int(progress * 100)
            self._task_bars[task_id].setValue(val)

        if stage:
            name = self._task_labels[task_id].text().split(" [")[0]
            self._task_labels[task_id].setText(f"{name} [{stage}]")

    def _on_priority_changed(self, task_id: str, value: int) -> None:
        """Обработка изменения приоритета задачи."""
        if not self.facade.set_task_priority(str(task_id), int(value)):
            self.logger.warning("failed to change priority task_id=%s value=%s", task_id, value)

    def _on_cancel_clicked(self, task_id: str) -> None:
        """Обработка нажатия кнопки отмены задачи."""
        import asyncio

        async def _cancel() -> None:
            await self.facade.cancel_task(str(task_id), reason="ui_cancel")

        asyncio.create_task(_cancel())

    def _remove_task_if_exists(self, task_id: str) -> None:
        """Удаляет задачу, если она существует."""
        if task_id in self._task_containers:
            self._remove_task(task_id)

    def _remove_task(self, task_id: str) -> None:
        container = self._task_containers.pop(task_id, None)
        if container is not None:
            # Обертка PySide может остаться, даже если C++-объект уже уничтожен (RuntimeError при доступе).
            try:
                if self.main_layout is not None:
                    self.main_layout.removeWidget(container)
            except RuntimeError:
                pass
            try:
                container.deleteLater()
            except RuntimeError:
                pass

        self._task_bars.pop(task_id, None)
        self._task_labels.pop(task_id, None)
        self._task_buttons.pop(task_id, None)

        if not self._task_containers:
            self.title_label.hide()

    def retranslate_ui(self, lang: str) -> None:
        self.title_label.setText(t("desktop.taskprogress.title", lang))

    def closeEvent(self, event):
        # Отменяем все таймеры перед закрытием
        # NOTE: В PySide6 нет способа отменить singleShot таймер,
        # но мы можем минимизировать вероятность ошибок проверкой существования объектов
        unsubscribe_func = getattr(self, '_unsubscribe', None)
        if unsubscribe_func:
            unsubscribe_func()
            self._unsubscribe = None
        super().closeEvent(event)
