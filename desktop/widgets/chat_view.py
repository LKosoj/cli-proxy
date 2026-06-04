from __future__ import annotations
import logging
import os
import time
from typing import Optional, List, Dict, Any
from PySide6.QtCore import Qt, Signal, Slot, QUrl
from PySide6.QtGui import QTextCursor, QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTextBrowser,
    QTextEdit,
    QPushButton,
    QLabel,
    QFrame,
    QFileDialog,
    QScrollArea,
    QProgressBar,
    QComboBox
)
from i18n import t
from utils.html_renderer import ansi_to_html


class ChatViewWidget(QWidget):
    """Улучшенный виджет отображения истории чата и ввода сообщений."""
    messageSent = Signal(str)  # Сигнал отправки сообщения пользователем
    messageSentWithAttachments = Signal(str, list)  # Текст + список путей вложений (Desktop-only)
    filesSelected = Signal(list)  # Сигнал выбора файлов пользователем
    taskCancelled = Signal()   # Сигнал прерывания текущей задачи
    quickActionTriggered = Signal(str)  # Сигнал для быстрых действий
    askOptionSelected = Signal(str)  # Сигнал выбора варианта ask_user
    progressMessageId = Signal(str, str)  # (session_uid, role) — id обновлённого progress-сообщения

    def __init__(
        self,
        logger: Optional[logging.Logger] = None
    ):
        super().__init__()
        self.logger = logger or logging.getLogger(__name__)
        self._attachments: List[str] = []
        self._theme_colors = {
            "chat_user_bg": "#e8f4fd",
            "chat_agent_bg": "#f2f9f4",
            "text_primary": "#2c3e50",
            "text_secondary": "#7f8c8d",
            "border": "#bdc3c7",
            "bg_secondary": "#f8f9fa",
            "accent": "#3498db",
            "success": "#27ae60",
        }
        # progress-сообщения: session_uid -> message id (для редактирования вместо append)
        # используем time-based id, отдельный от facade._desktop_message_id
        self._progress_message_id: Optional[str] = None
        # QTextCursor со selection внутри document(), отслеживает границы progress-блока
        # (Qt автоматически сдвигает позиции при вставках/удалениях в документе)
        self._progress_cursor: Optional[QTextCursor] = None
        self._lang: str = "ru"
        self._setup_ui()
        self._setup_quick_actions()

    def _setup_quick_actions(self):
        """Настройка быстрых действий."""
        # Добавляем список возможных команд для автодополнения
        self.commands = [
            "/status", "/commit", "/pull", "/push", "/help",
            "/reset", "/rename", "/switch_cli", "/resume"
        ]
        # QTextEdit не поддерживает setCompleter, используем QLineEdit для автодополнения
        # или просто оставим команды для быстрого доступа

    def set_theme_colors(self, colors: Dict[str, str]):
        """Обновляет цвета темы для отрисовки HTML-сообщений."""
        self._theme_colors.update(colors)

        # Default stylesheet для QTextBrowser — базовые цвета всех HTML-элементов
        tc = colors.get("text_primary", "#2c3e50")
        tc2 = colors.get("text_secondary", "#7f8c8d")
        bg_t = colors.get("bg_tertiary", "#f6f8fa")
        bg_s = colors.get("bg_secondary", "#f8f9fa")
        brd = colors.get("border", "#dee2e6")
        mono = "ui-monospace,SFMono-Regular,Consolas,Monaco,Menlo,monospace"
        if hasattr(self, "history_browser"):
            self.history_browser.document().setDefaultStyleSheet(
                f"body {{ color: {tc}; }}"
                f"p, li, ul, ol, h1, h2, h3, h4, h5, h6, div, span, b, i, em, strong "
                f"{{ color: {tc}; }}"
                f"pre {{ background-color: {bg_t}; color: {tc}; padding: 12px;"
                f" font-family: {mono}; }}"
                f"code {{ background-color: {bg_t}; color: {tc}; padding: 2px 4px;"
                f" font-family: {mono}; }}"
                f"th {{ background-color: {bg_s}; border: 1px solid {brd};"
                f" padding: 6px 10px; color: {tc}; }}"
                f"td {{ border: 1px solid {brd}; padding: 6px 10px; color: {tc}; }}"
                f"blockquote {{ border-left: 4px solid {brd}; padding-left: 12px;"
                f" color: {tc2}; }}"
                f"a {{ color: {colors.get('accent', '#3498db')}; }}"
            )
        if hasattr(self, "assistant_preview_browser"):
            self.assistant_preview_browser.document().setDefaultStyleSheet(
                f"body {{ color: {tc}; }}"
                f"p, li, ul, ol, h1, h2, h3, h4, h5, h6, div, span, b, i, em, strong "
                f"{{ color: {tc}; }}"
                f"pre {{ background-color: {bg_t}; color: {tc}; padding: 12px;"
                f" font-family: {mono}; }}"
                f"code {{ background-color: {bg_t}; color: {tc}; padding: 2px 4px;"
                f" font-family: {mono}; }}"
                f"th {{ background-color: {bg_s}; border: 1px solid {brd};"
                f" padding: 6px 10px; color: {tc}; }}"
                f"td {{ border: 1px solid {brd}; padding: 6px 10px; color: {tc}; }}"
                f"blockquote {{ border-left: 4px solid {brd}; padding-left: 12px;"
                f" color: {tc2}; }}"
                f"a {{ color: {colors.get('accent', '#3498db')}; }}"
            )

        # Обновляем стили компонентов
        bg_secondary = colors.get("bg_secondary", "#f8f9fa")
        border_color = colors.get("border", "#dee2e6")
        if hasattr(self, "input_container"):
            style = (
                f"QWidget#input_container {{ background-color: {bg_secondary}; "
                f"border-top: 1px solid {border_color}; }}"
            )
            self.input_container.setStyleSheet(style)
        if hasattr(self, "separator"):
            style = f"QFrame#separator {{ background-color: {border_color}; border: none; height: 1px; }}"
            self.separator.setStyleSheet(style)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # История чата
        self.history_browser = QTextBrowser()
        self.history_browser.setObjectName("history_browser")
        self.history_browser.setOpenExternalLinks(True)
        self.history_browser.setReadOnly(True)
        self.history_browser.setAcceptRichText(True)
        layout.addWidget(self.history_browser, 1)
        self.assistant_preview_browser = QTextBrowser()
        self.assistant_preview_browser.setObjectName("assistant_preview_browser")
        self.assistant_preview_browser.setOpenExternalLinks(True)
        self.assistant_preview_browser.setReadOnly(True)
        self.assistant_preview_browser.setAcceptRichText(True)
        self.assistant_preview_browser.hide()
        layout.addWidget(self.assistant_preview_browser, 0)
        # Разделитель
        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.Shape.HLine)
        self.separator.setFrameShadow(QFrame.Shadow.Sunken)
        self.separator.setObjectName("separator")
        layout.addWidget(self.separator, 0)
        # Панель кнопок ask_user (варианты ответа)
        self.ask_options_container = QWidget()
        self.ask_options_container.setObjectName("ask_options_container")
        self._ask_options_layout = QVBoxLayout(self.ask_options_container)
        self._ask_options_layout.setContentsMargins(8, 6, 8, 6)
        self._ask_options_layout.setSpacing(4)
        self.ask_options_container.hide()
        layout.addWidget(self.ask_options_container, 0)

        # Область ввода
        self.input_container = QWidget()
        self.input_container.setObjectName("input_container")
        input_layout = QVBoxLayout(self.input_container)
        input_layout.setContentsMargins(8, 6, 8, 6)
        input_layout.setSpacing(4)
        # Область предпросмотра вложений
        self.attachments_scroll = QScrollArea()
        self.attachments_scroll.setWidgetResizable(True)
        self.attachments_scroll.setFixedHeight(80)
        self.attachments_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.attachments_scroll.hide()
        self.attachments_widget = QWidget()
        self.attachments_layout = QHBoxLayout(self.attachments_widget)
        self.attachments_layout.setContentsMargins(0, 0, 0, 0)
        self.attachments_layout.setSpacing(10)
        self.attachments_layout.addStretch()
        self.attachments_scroll.setWidget(self.attachments_widget)
        input_layout.addWidget(self.attachments_scroll)
        # Quick Actions combo (hidden by default; use Ctrl+K command palette instead)
        self.commands_combo = QComboBox()
        self.commands_combo.addItems([
            "Quick Actions...",
            "Show Status",
            "Commit Changes",
            "Pull Updates",
            "Push Changes",
            "Reset Session",
            "More Commands..."
        ])
        self.commands_combo.currentTextChanged.connect(self._on_command_selected)
        self.commands_combo.hide()
        input_layout.addWidget(self.commands_combo)
        # Многострочное поле ввода
        self.message_input = QTextEdit()
        self.message_input.setObjectName("message_input")
        self.message_input.setPlaceholderText(t("desktop.chat.input_placeholder", "ru"))
        self.message_input.setMaximumHeight(80)
        self.message_input.setMinimumHeight(36)
        # Обработка Ctrl+Enter и клавиш вверх/вниз для истории
        self.message_input.installEventFilter(self)
        # Поддержка истории сообщений
        self._message_history = []
        self._history_index = -1
        input_layout.addWidget(self.message_input)
        # Панель кнопок
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)
        self.status_label = QLabel(t("desktop.chat.status_ready", "ru"))
        self.status_label.setObjectName("status_label")
        btn_layout.addWidget(self.status_label)
        btn_layout.addStretch()
        self.stop_button = QPushButton(t("desktop.chat.btn_stop", "ru"))
        self.stop_button.setObjectName("stop_button")
        self.stop_button.setFixedWidth(80)
        self.stop_button.clicked.connect(lambda: self.taskCancelled.emit())
        self.stop_button.hide()
        btn_layout.addWidget(self.stop_button)
        self.attach_button = QPushButton("+")
        self.attach_button.setObjectName("attach_button")
        self.attach_button.setFixedWidth(36)
        self.attach_button.setFixedHeight(28)
        self.attach_button.clicked.connect(self._on_attach_clicked)
        btn_layout.addWidget(self.attach_button)
        self.send_button = QPushButton(t("desktop.btn.send", "ru"))
        self.send_button.setObjectName("send_button")
        self.send_button.setFixedWidth(72)
        self.send_button.setFixedHeight(28)
        self.send_button.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_button)
        input_layout.addLayout(btn_layout)
        from PySide6.QtWidgets import QSizePolicy
        self.input_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.input_container, 0)

    def eventFilter(self, obj, event):
        """Перехват клавиш для отправки и навигации по истории."""
        if obj is self.message_input:
            if event.type() == event.Type.KeyPress:
                # Обработка Ctrl+Enter для отправки
                if event.key() == Qt.Key.Key_Return and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    self._on_send_clicked()
                    return True
                # Обработка клавиш вверх/вниз для навигации по истории
                if event.key() == Qt.Key.Key_Up:
                    self._navigate_history(backward=True)
                    return True
                elif event.key() == Qt.Key.Key_Down:
                    self._navigate_history(backward=False)
                    return True
        return super().eventFilter(obj, event)

    def _navigate_history(self, backward: bool):
        """Навигация по истории сообщений."""
        if not self._message_history:
            return
        if backward:
            if self._history_index < len(self._message_history) - 1:
                self._history_index += 1
        else:
            if self._history_index > -1:
                self._history_index -= 1
        # Устанавливаем текст из истории (-1 означает пустое поле)
        if self._history_index >= 0:
            self.message_input.setPlainText(self._message_history[self._history_index])
            self.message_input.moveCursor(QTextCursor.MoveOperation.End)
        else:
            self.message_input.clear()

    def _on_command_selected(self, text: str):
        """Обработка выбора команды из выпадающего списка."""
        if text == "Quick Actions...":
            return
        elif text == "Show Status":
            self.message_input.setPlainText("/status")
        elif text == "Commit Changes":
            self.message_input.setPlainText("/commit")
        elif text == "Pull Updates":
            self.message_input.setPlainText("/pull")
        elif text == "Push Changes":
            self.message_input.setPlainText("/push")
        elif text == "Reset Session":
            self.message_input.setPlainText("/reset")
        elif text == "More Commands...":
            # Показываем все доступные команды
            all_commands = "\n".join([
                "/status - Show repository status",
                "/commit - Commit changes",
                "/pull - Pull updates",
                "/push - Push changes",
                "/reset - Reset session",
                "/rename - Rename session",
                "/switch_cli - Switch CLI tool",
                "/resume - Resume from token"
            ])
            self.append_message("agent", f"Available commands:\n{all_commands}")
            self.commands_combo.setCurrentIndex(0)  # Сброс в "Quick Actions..."
            return
        # Сбрасываем выбор в комбобоксе
        self.commands_combo.setCurrentIndex(0)

    @Slot()
    def _on_send_clicked(self):
        text = self.message_input.toPlainText().strip()
        if text or self._attachments:
            # Добавляем сообщение в историю (если не является командой)
            if not text.startswith('/'):
                self._message_history.insert(0, text)
                # Ограничиваем размер истории
                if len(self._message_history) > 50:
                    self._message_history = self._message_history[:50]
            self._history_index = -1  # Сбрасываем индекс истории
            attachments = list(self._attachments)
            self.message_input.clear()
            self.messageSent.emit(text)
            self.messageSentWithAttachments.emit(text, attachments)
            self._attachments = []
            self._update_attachments_ui()

    @Slot()
    def _on_attach_clicked(self):
        """Обработка нажатия кнопки вложений."""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            t("desktop.chat.select_files", self._lang),
            "",
            t("desktop.chat.all_files", self._lang)
        )
        if files:
            self._attachments.extend(files)
            self._update_attachments_ui()
            self.filesSelected.emit(files)

    def _update_attachments_ui(self):
        """Обновление UI предпросмотра вложений."""
        # Очистка текущего лейаута
        while self.attachments_layout.count() > 1:
            item = self.attachments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._attachments:
            self.attachments_scroll.hide()
            return
        self.attachments_scroll.show()
        for file_path in self._attachments:
            widget = self._create_attachment_preview(file_path)
            self.attachments_layout.insertWidget(self.attachments_layout.count() - 1, widget)

    def _create_attachment_preview(self, file_path: str) -> QWidget:
        """Создание виджета предпросмотра для одного файла."""
        container = QFrame()
        container.setObjectName("attachment_preview")
        container.setFixedSize(70, 70)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        # Кнопка удаления
        remove_btn = QPushButton("×")
        remove_btn.setObjectName("attachment_remove_btn")
        remove_btn.setFixedSize(16, 16)
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.clicked.connect(lambda checked=False, f=file_path: self._remove_attachment(f))
        # Позиционируем кнопку удаления в углу
        remove_layout = QHBoxLayout()
        remove_layout.setContentsMargins(0, 0, 0, 0)
        remove_layout.addStretch()
        remove_layout.addWidget(remove_btn)
        layout.addLayout(remove_layout)
        # Контент (миниатюра или иконка)
        content_label = QLabel()
        content_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ext = os.path.splitext(file_path)[1].lower()
        is_image = ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']
        if is_image:
            pixmap = QPixmap(file_path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    50, 50,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                content_label.setPixmap(scaled)
            else:
                content_label.setText("IMG")
        else:
            content_label.setText(ext[1:].upper() or "FILE")
            content_label.setObjectName("attachment_text_preview")
        layout.addWidget(content_label)
        # Прогресс-индикатор (имитация подготовки)
        progress = QProgressBar()
        progress.setObjectName("attachment_progress")
        progress.setFixedHeight(4)
        progress.setTextVisible(False)
        progress.setRange(0, 100)
        progress.setValue(100)  # В реальности тут могла быть асинхронная загрузка
        layout.addWidget(progress)
        return container

    def _remove_attachment(self, file_path: str):
        """Удаление вложения из списка."""
        if file_path in self._attachments:
            self._attachments.remove(file_path)
            self._update_attachments_ui()

    def append_progress_message(self, role: str, text: str) -> int:
        """Добавление progress-сообщения; границы блока сохраняются в self._progress_cursor."""
        # QTextBrowser не поддерживает runJavaScript — работаем через QTextCursor.
        msg_id = str(int(time.time() * 1000))
        formatted_msg = self._format_message(role, text, attachments=None)
        doc = self.history_browser.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # отделяем progress новым параграфом, если документ не пуст
        if not doc.isEmpty():
            cursor.insertBlock()
        start = cursor.position()
        cursor.insertHtml(formatted_msg)
        end = cursor.position()
        # сохраняем cursor со selection — Qt автоматически сдвигает позиции при правках документа
        self._progress_cursor = QTextCursor(doc)
        self._progress_cursor.setPosition(start)
        self._progress_cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._progress_message_id = msg_id
        self._scroll_to_bottom()
        return int(msg_id)

    def update_progress_message(self, role: str, text: str) -> bool:
        """Обновление текущего progress-блока. Возвращает True если обновлено."""
        if self._progress_message_id is None or self._progress_cursor is None:
            return False
        formatted_msg = self._format_message(role, text, attachments=None)
        # удаляем старое содержимое и вставляем новое, сохраняя selection
        self._progress_cursor.removeSelectedText()
        start = self._progress_cursor.position()
        self._progress_cursor.insertHtml(formatted_msg)
        end = self._progress_cursor.position()
        self._progress_cursor.setPosition(start)
        self._progress_cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._scroll_to_bottom()
        return True

    def clear_progress_message(self) -> None:
        """Удаление progress-блока из документа."""
        if self._progress_message_id is None or self._progress_cursor is None:
            self._progress_message_id = None
            self._progress_cursor = None
            return
        # удаляем содержимое selection
        self._progress_cursor.removeSelectedText()
        pos = self._progress_cursor.position()
        # удаляем paragraph separator, вставленный insertBlock() при append
        if pos > 0:
            tmp = QTextCursor(self.history_browser.document())
            tmp.setPosition(pos - 1)
            tmp.setPosition(pos, QTextCursor.MoveMode.KeepAnchor)
            # QTextDocument разделяет блоки символом U+2029 (PARAGRAPH SEPARATOR)
            if tmp.selectedText() in (" ", "\n"):
                tmp.removeSelectedText()
        self._progress_message_id = None
        self._progress_cursor = None

    def append_message(self, role: str, text: str, attachments: Optional[List[Dict[str, Any]]] = None):
        """Добавление сообщения в историю. Сбрасывает progress_message_id."""
        formatted_msg = self._format_message(role, text, attachments=attachments)
        self.history_browser.append(formatted_msg)
        self._scroll_to_bottom()
        # нормальное сообщение сбрасывает progress-состояние
        self._progress_message_id = None
        self._progress_cursor = None

    def update_last_message(self, role: str, text: str):
        """Обновление содержимого последнего сообщения (для стриминга)."""
        cursor = self.history_browser.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # Находим начало последнего блока (нашего div)
        # QTextBrowser.append добавляет новый блок.
        # Мы хотим удалить последний вставленный фрагмент.
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        # Также удаляем пустую строку которую мог оставить append
        cursor.deletePreviousChar()
        self.append_message(role, text)

    def set_assistant_preview(self, text: str) -> None:
        formatted_msg = self._format_message("agent", text)
        self.assistant_preview_browser.setHtml(formatted_msg)
        self.assistant_preview_browser.show()
        self._scroll_to_bottom()

    def clear_assistant_preview(self) -> None:
        self.assistant_preview_browser.clear()
        self.assistant_preview_browser.hide()

    def _format_message(self, role: str, text: str, attachments: Optional[List[Dict[str, Any]]] = None) -> str:
        accent_color = self._theme_colors.get("accent", "#3498db")
        success_color = self._theme_colors.get("success", "#27ae60")
        if role == "user":
            prefix = f"<b style='color: {accent_color};'>{t('desktop.chat.prefix_you', self._lang)}</b>"
            bg_color = self._theme_colors.get("chat_user_bg", "#e8f4fd")
        else:
            prefix = f"<b style='color: {success_color};'>{t('desktop.chat.prefix_agent', self._lang)}</b>"
            bg_color = self._theme_colors.get("chat_agent_bg", "#f2f9f4")
        text_color = self._theme_colors.get("text_primary", "#2c3e50")
        html_content = ansi_to_html(text, theme_colors=self._theme_colors, fragment=True)
        attachments_html = self._render_attachment_html(attachments) if attachments else ""
        return (
            f"<div style='background-color: {bg_color}; color: {text_color}; "
            f"padding: 10px; border-radius: 5px; margin-bottom: 10px;'>"
            f"{prefix}<br/>{html_content}{attachments_html}</div>"
        )

    def _render_attachment_html(self, attachments: List[Dict[str, Any]]) -> str:
        """Рендеринг вложений в HTML."""
        if not attachments:
            return ""
        border_color = self._theme_colors.get("border", "#ddd")
        text_secondary = self._theme_colors.get("text_secondary", "#7f8c8d")
        html = f"<div style='margin-top: 10px; border-top: 1px solid {border_color}; padding-top: 8px;'>"
        for a in attachments:
            kind = a.get("kind")
            name = a.get("name", "file")
            path = a.get("stored_path") or a.get("original_path") or ""
            # В Desktop используем QUrl для корректного формирования file:// на всех платформах
            img_url = ""
            if path:
                img_url = QUrl.fromLocalFile(path).toString()
            if kind == "image" and img_url:
                size_kb = round(int(a.get("size_bytes") or 0) / 1024, 1)
                html += f"""
                <div style='margin-bottom: 8px;'>
                    <img src='{img_url}' width='100%' style='border-radius: 3px; border: 1px solid {border_color};' />
                    <div style='font-size: 10px; color: {text_secondary};'>{name} ({size_kb} KB)</div>
                </div>
                """
            else:
                ext = str(a.get("ext") or "").upper() or "FILE"
                size_kb = round(int(a.get("size_bytes") or 0) / 1024, 1)
                html += (
                    f"<div style='display: inline-block; background-color: rgba(128,128,128,0.1); "
                    f"border: 1px solid {border_color}; border-radius: 3px; padding: 4px 8px; "
                    f"margin-right: 5px; margin-bottom: 5px;'>"
                    f"<span style='font-weight: bold;'>📎 {name}</span>"
                    f"<span style='font-size: 10px; color: {text_secondary}; margin-left: 5px;'>"
                    f"({ext}, {size_kb} KB)</span></div>"
                )
        html += "</div>"
        return html

    # ---- ask_user option buttons ----

    def show_ask_options(self, options: List[str]):
        """Показать кнопки вариантов ответа для ask_user."""
        self.hide_ask_options()
        accent = self._theme_colors.get("accent", "#3498db")
        accent_hover = self._theme_colors.get("accent_hover", "#2980b9")
        text_secondary = self._theme_colors.get(
            "text_secondary", "#7f8c8d"
        )
        for opt in options:
            btn = QPushButton(opt)
            btn.setObjectName("ask_option_btn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton#ask_option_btn {{"
                f"  background-color: {accent}; color: #ffffff;"
                f"  border: none; border-radius: 6px;"
                f"  padding: 8px 16px; font-size: 13px;"
                f"}}"
                f"QPushButton#ask_option_btn:hover {{"
                f"  background-color: {accent_hover};"
                f"}}"
            )
            btn.clicked.connect(
                lambda checked=False, text=opt: self._on_ask_option_clicked(text)
            )
            self._ask_options_layout.addWidget(btn)
        # Подсказка о возможности ввести свой вариант
        hint = QLabel(t("desktop.chat.ask_hint", self._lang))
        hint.setObjectName("ask_options_hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(
            f"QLabel#ask_options_hint {{"
            f"  color: {text_secondary}; font-size: 11px;"
            f"  padding: 2px 0px;"
            f"}}"
        )
        self._ask_options_layout.addWidget(hint)
        self.ask_options_container.show()
        # Разблокируем ввод, чтобы пользователь мог напечатать свой вариант
        self.send_button.setEnabled(True)
        self.message_input.setEnabled(True)
        self.message_input.setFocus()
        self.status_label.setText(t("desktop.chat.status_waiting_answer", self._lang))

    def hide_ask_options(self):
        """Скрыть и очистить панель кнопок ask_user."""
        while self._ask_options_layout.count():
            item = self._ask_options_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.ask_options_container.hide()

    def _on_ask_option_clicked(self, text: str):
        self.askOptionSelected.emit(text)
        self.hide_ask_options()

    # ---- end ask_user ----

    def _scroll_to_bottom(self):
        self.history_browser.verticalScrollBar().setValue(
            self.history_browser.verticalScrollBar().maximum()
        )

    def clear_history(self):
        """Очистка истории."""
        self.history_browser.clear()
        self._message_history = []  # Очищаем также локальную историю

    def set_loading(self, loading: bool):
        """Индикация процесса загрузки."""
        self.send_button.setEnabled(not loading)
        self.attach_button.setEnabled(not loading)
        self.message_input.setEnabled(not loading)
        if loading:
            self.status_label.setText(t("desktop.chat.status_thinking", self._lang))
            self.stop_button.show()
        else:
            self.status_label.setText(t("desktop.chat.status_ready", self._lang))
            self.stop_button.hide()

    def retranslate_ui(self, lang: str) -> None:
        self._lang = lang
        self.message_input.setPlaceholderText(t("desktop.chat.input_placeholder", lang))
        self.send_button.setText(t("desktop.btn.send", lang))
        self.stop_button.setText(t("desktop.chat.btn_stop", lang))
        self.status_label.setText(t("desktop.chat.status_ready", lang))
