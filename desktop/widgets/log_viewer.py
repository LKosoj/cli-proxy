from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING, Optional, List

from i18n import t

from PySide6.QtCore import Signal, Slot, QObject, QTimer
from PySide6.QtGui import QTextCursor, QColor, QTextCharFormat, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QLineEdit,
    QLabel,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QTabWidget,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
    QFileDialog,
)

if TYPE_CHECKING:
    from app.services.task_service import TaskService

from app.services.logging_service import resolve_log_paths

logger = logging.getLogger(__name__)

VERBOSE_LEVEL = 10

# Ordered list of log type identifiers matching miniapp/services/logs_service.py
_LOG_TYPES = ("main", "error", "agent", "cli_dialog", "miniapp")
_LOG_TYPE_LABELS = {
    "main": "Основной",
    "error": "Ошибки",
    "agent": "Agent",
    "cli_dialog": "CLI диалог",
    "miniapp": "MiniApp",
}


class LogSignalEmitter(QObject):
    """Эмиттер сигналов для передачи логов в поток UI."""
    record_received = Signal(logging.LogRecord)


class LogViewerWidget(QWidget):
    """Улучшенный виджет для просмотра логов в реальном времени."""

    def __init__(
        self,
        task_service: TaskService,
        log_path: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.task_service = task_service
        self._log_path: str = log_path
        self.emitter = LogSignalEmitter()
        self.emitter.record_received.connect(self._on_record_emitted)

        self._theme_colors: dict = {}
        self._buffer: List[logging.LogRecord] = []
        self._setup_ui()

        # Таймер для пакетного обновления UI (каждые 100мс)
        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush_buffer)
        self._flush_timer.start(100)

        # Подписка на шину логов
        if hasattr(self.task_service, "log_bus") and self.task_service.log_bus:
            self._unsubscribe = self.task_service.log_bus.subscribe(self._on_bus_record)
        else:
            self._unsubscribe = lambda: None

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Создаем вкладки для разных видов логов
        self.tabs = QTabWidget()

        # Основная вкладка логов
        self.main_log_tab = QWidget()
        self._setup_main_log_tab()
        self.tabs.addTab(self.main_log_tab, "Main Log")

        # Вкладка фильтрации
        self.filter_tab = QWidget()
        self._setup_filter_tab()
        self.tabs.addTab(self.filter_tab, "Filters")

        # Вкладка мониторинга задач
        self.tasks_tab = QWidget()
        self._setup_tasks_tab()
        self.tabs.addTab(self.tasks_tab, "Tasks")

        layout.addWidget(self.tabs)

    def _setup_main_log_tab(self):
        """Настройка основной вкладки логов."""
        layout = QVBoxLayout(self.main_log_tab)
        layout.setContentsMargins(5, 5, 5, 5)

        # Панель инструментов
        toolbar = QHBoxLayout()

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter by keyword...")
        self.filter_input.textChanged.connect(self._apply_filter)
        self.filter_label = QLabel("Filter:")
        toolbar.addWidget(self.filter_label)
        toolbar.addWidget(self.filter_input)

        self.level_filter = QComboBox()
        self.level_filter.addItems(["All", "VERBOSE", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.level_filter.currentTextChanged.connect(self._apply_filter)
        self.level_label = QLabel("Level:")
        toolbar.addWidget(self.level_label)
        toolbar.addWidget(self.level_filter)

        self.type_label = QLabel("Type:")
        self.type_filter = QComboBox()
        for key in _LOG_TYPES:
            self.type_filter.addItem(_LOG_TYPE_LABELS[key], userData=key)
        self.type_filter.currentIndexChanged.connect(self._on_log_type_changed)
        toolbar.addWidget(self.type_label)
        toolbar.addWidget(self.type_filter)

        self.auto_scroll_cb = QCheckBox("Auto-scroll")
        self.auto_scroll_cb.setChecked(True)
        toolbar.addWidget(self.auto_scroll_cb)

        self.wrap_text_cb = QCheckBox("Wrap Text")
        self.wrap_text_cb.setChecked(False)
        self.wrap_text_cb.toggled.connect(self._toggle_wrap_text)
        toolbar.addWidget(self.wrap_text_cb)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self._on_copy_clicked)
        toolbar.addWidget(self.copy_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        toolbar.addWidget(self.clear_btn)

        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(self._on_download_clicked)
        toolbar.addWidget(self.download_btn)

        layout.addLayout(toolbar)

        # Поле вывода логов
        self.log_display = QPlainTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setUndoRedoEnabled(False)
        self.log_display.setMaximumBlockCount(10000)  # Увеличенный лимит

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.log_display.setFont(font)

        layout.addWidget(self.log_display)

    def _setup_filter_tab(self):
        """Настройка вкладки фильтрации."""
        layout = QVBoxLayout(self.filter_tab)
        layout.setContentsMargins(5, 5, 5, 5)

        # Группа фильтров по уровням
        self.level_group = QGroupBox(t("desktop.log.group.level_filters", "ru"))
        level_group = self.level_group
        level_layout = QVBoxLayout(level_group)

        self.verbose_filter_cb = QCheckBox("VERBOSE")
        self.verbose_filter_cb.setChecked(True)
        level_layout.addWidget(self.verbose_filter_cb)

        self.info_filter_cb = QCheckBox("INFO")
        self.info_filter_cb.setChecked(True)
        level_layout.addWidget(self.info_filter_cb)

        self.warning_filter_cb = QCheckBox("WARNING")
        self.warning_filter_cb.setChecked(True)
        level_layout.addWidget(self.warning_filter_cb)

        self.error_filter_cb = QCheckBox("ERROR")
        self.error_filter_cb.setChecked(True)
        level_layout.addWidget(self.error_filter_cb)

        self.critical_filter_cb = QCheckBox("CRITICAL")
        self.critical_filter_cb.setChecked(True)
        level_layout.addWidget(self.critical_filter_cb)

        layout.addWidget(level_group)

        # Группа фильтров по модулям
        self.module_group = QGroupBox(t("desktop.log.group.module_filters", "ru"))
        module_group = self.module_group
        module_layout = QVBoxLayout(module_group)

        self.module_filter_input = QLineEdit()
        self.module_filter_input.setPlaceholderText("Enter module name to filter...")
        module_layout.addWidget(self.module_filter_input)

        layout.addWidget(module_group)

        # Кнопка применения фильтров
        self.apply_filters_btn = QPushButton("Apply Filters")
        self.apply_filters_btn.clicked.connect(self._apply_advanced_filters)
        layout.addWidget(self.apply_filters_btn)

        layout.addStretch()

    def _setup_tasks_tab(self):
        """Настройка вкладки мониторинга задач."""
        layout = QVBoxLayout(self.tasks_tab)
        layout.setContentsMargins(5, 5, 5, 5)

        # Заголовок
        self.tasks_monitor_title = QLabel("Active Tasks Monitor")
        self.tasks_monitor_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.tasks_monitor_title)

        # Список активных задач
        self.tasks_list = QListWidget()
        self.tasks_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.tasks_list)

        # Кнопка обновления
        self.refresh_tasks_btn = QPushButton("Refresh Tasks")
        self.refresh_tasks_btn.clicked.connect(self._refresh_tasks)
        layout.addWidget(self.refresh_tasks_btn)

        layout.addStretch()

        # Обновляем список задач
        self._refresh_tasks()

    def _refresh_tasks(self):
        """Обновление списка активных задач."""
        self.tasks_list.clear()

        # Получаем активные задачи из сервиса
        active_tasks = self.task_service.list_active()

        for task in active_tasks:
            item = QListWidgetItem()
            item.setText(f"{task.name} ({task.task_id[:8]})")
            item.setToolTip(f"ID: {task.task_id}\nSession: {task.session_id}\nCreated: {task.created_at}")
            self.tasks_list.addItem(item)

    def _apply_filter(self):
        """Применение фильтра к отображению логов."""
        # В реальной реализации этот метод будет фильтровать отображаемые логи
        # Пока что просто обновляем отображение
        pass

    def _apply_advanced_filters(self):
        """Применение расширенных фильтров."""
        # В реальной реализации этот метод будет применять расширенные фильтры
        pass

    def _toggle_wrap_text(self, checked: bool):
        """Переключение режима переноса текста."""
        if checked:
            self.log_display.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        else:
            self.log_display.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def _on_bus_record(self, record: logging.LogRecord):
        """Вызывается из любого потока при поступлении лога."""
        self.emitter.record_received.emit(record)

    @Slot(logging.LogRecord)
    def _on_record_emitted(self, record: logging.LogRecord):
        """Накапливает записи в буфере в потоке UI."""
        self._buffer.append(record)

    def _flush_buffer(self):
        """Выводит накопленные логи в виджет."""
        if not self._buffer:
            return

        records = self._buffer
        self._buffer = []

        # Получаем текущие фильтры
        filter_text = self.filter_input.text().lower()
        level_filter = self.level_filter.currentText()

        self.log_display.setUpdatesEnabled(False)
        try:
            for record in records:
                msg = self._format_record(record)

                # Проверяем фильтр по тексту
                if filter_text and filter_text not in msg.lower():
                    continue

                # Проверяем фильтр по уровню
                if level_filter != "All":
                    if level_filter == "VERBOSE":
                        level_num = VERBOSE_LEVEL
                    else:
                        level_num = getattr(logging, level_filter, VERBOSE_LEVEL)
                    if record.levelno < level_num:
                        continue

                self._append_log(msg, record.levelno)
        finally:
            self.log_display.setUpdatesEnabled(True)

        if self.auto_scroll_cb.isChecked():
            self.log_display.verticalScrollBar().setValue(
                self.log_display.verticalScrollBar().maximum()
            )

    def _format_record(self, record: logging.LogRecord) -> str:
        asctime = logging.Formatter().formatTime(record)
        return f"{asctime} {record.levelname} [{record.name}] {record.getMessage()}"

    def set_theme_colors(self, colors: dict):
        """Обновляет цвета темы."""
        self._theme_colors = colors

    def _append_log(self, text: str, level: int):
        color = self._get_level_color(level)
        self.log_display.moveCursor(QTextCursor.End)

        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        else:
            text_color = self._theme_colors.get("text_primary", "#000000")
            fmt.setForeground(QColor(text_color))

        if level >= logging.ERROR:
            fmt.setFontWeight(QFont.Bold)

        self.log_display.setCurrentCharFormat(fmt)
        self.log_display.appendPlainText(text)

    def _get_level_color(self, level: int) -> Optional[str]:
        if level >= logging.CRITICAL:
            return self._theme_colors.get("danger", "#ff0000")
        if level >= logging.ERROR:
            return self._theme_colors.get("danger", "#e74c3c")
        if level >= logging.WARNING:
            return self._theme_colors.get("warning", "#f39c12")
        if level >= logging.INFO:
            return self._theme_colors.get("success", "#27ae60")
        if level >= VERBOSE_LEVEL:
            return self._theme_colors.get("text_secondary", "#7f8c8d")
        return None

    @Slot()
    def _on_copy_clicked(self):
        self.log_display.selectAll()
        self.log_display.copy()
        cursor = self.log_display.textCursor()
        cursor.clearSelection()
        self.log_display.setTextCursor(cursor)

    @Slot()
    def _on_clear_clicked(self):
        self.log_display.clear()

    def set_log_path(self, log_path: str) -> None:
        """Обновляет путь к лог-файлу. Может вызываться после инициализации виджета."""
        self._log_path = log_path

    def _current_log_file_path(self) -> Optional[str]:
        """Возвращает путь к выбранному лог-файлу или None, если log_path не задан."""
        if not self._log_path:
            return None
        log_type = self.type_filter.currentData()
        if not log_type:
            return None
        paths = resolve_log_paths(self._log_path)
        return paths.get(log_type)

    @Slot(int)
    def _on_log_type_changed(self, _index: int) -> None:
        """Переключает отображение логов при смене типа в комбобоксе."""
        path = self._current_log_file_path()
        if not path:
            self.log_display.setPlainText(t("desktop.log.no_log_path", "ru"))
            return
        if not os.path.exists(path):
            self.log_display.setPlainText(t("desktop.log.not_found", "ru"))
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            self.log_display.setPlainText(content)
            if self.auto_scroll_cb.isChecked():
                self.log_display.verticalScrollBar().setValue(
                    self.log_display.verticalScrollBar().maximum()
                )
        except OSError:
            logger.exception("Failed to read log file: %s", path)

    @Slot()
    def _on_download_clicked(self) -> None:
        """Открывает диалог сохранения файла и копирует выбранный лог."""
        path = self._current_log_file_path()
        if not path or not os.path.exists(path):
            return
        default_name = os.path.basename(path)
        target, _ = QFileDialog.getSaveFileName(self, "Save Log File", default_name)
        if not target:
            return
        try:
            shutil.copy2(path, target)
        except OSError:
            logger.exception("Failed to copy log file: %s -> %s", path, target)

    def closeEvent(self, event):
        self._flush_timer.stop()
        if hasattr(self, "_unsubscribe"):
            self._unsubscribe()
        super().closeEvent(event)

    def retranslate_ui(self, lang: str) -> None:
        self.tabs.setTabText(0, t("desktop.log.tab.main", lang))
        self.tabs.setTabText(1, t("desktop.log.tab.filters", lang))
        self.tabs.setTabText(2, t("desktop.log.tab.tasks", lang))
        self.filter_input.setPlaceholderText(t("desktop.log.filter_placeholder", lang))
        self.filter_label.setText(t("desktop.log.filter_label", lang))
        self.level_label.setText(t("desktop.log.level_label", lang))
        self.type_label.setText(t("desktop.log.type_label", lang))
        self.auto_scroll_cb.setText(t("desktop.log.auto_scroll", lang))
        self.wrap_text_cb.setText(t("desktop.log.wrap_text", lang))
        self.copy_btn.setText(t("desktop.btn.copy", lang))
        self.clear_btn.setText(t("desktop.btn.clear", lang))
        self.download_btn.setText(t("desktop.log.btn.download", lang))
        self.level_group.setTitle(t("desktop.log.group.level_filters", lang))
        self.module_group.setTitle(t("desktop.log.group.module_filters", lang))
        self.module_filter_input.setPlaceholderText(t("desktop.log.module_filter_placeholder", lang))
        self.apply_filters_btn.setText(t("desktop.log.btn.apply_filters", lang))
        self.tasks_monitor_title.setText(t("desktop.log.tasks_monitor_title", lang))
        self.refresh_tasks_btn.setText(t("desktop.log.btn.refresh_tasks", lang))
