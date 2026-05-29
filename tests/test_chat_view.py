from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
from desktop.widgets.chat_view import ChatViewWidget


def test_chat_view_init(qtbot):
    """Проверка инициализации ChatViewWidget."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    assert widget.status_label.text() == "Ready"
    assert widget.send_button.isEnabled()
    assert widget.message_input.toPlainText() == ""


def test_chat_view_send_signal(qtbot):
    """Проверка сигнала messageSent при нажатии кнопки Send."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    spy = []
    widget.messageSent.connect(lambda text: spy.append(text))

    widget.message_input.setPlainText("Hello Agent")
    qtbot.mouseClick(widget.send_button, Qt.MouseButton.LeftButton)

    assert len(spy) == 1
    assert spy[0] == "Hello Agent"
    assert widget.message_input.toPlainText() == ""


def test_chat_view_append_messages(qtbot):
    """Проверка добавления сообщений в историю."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    widget.append_message("user", "Hello")
    widget.append_message("agent", "Hi there")

    content = widget.history_browser.toHtml()
    assert "You:" in content
    assert "Hello" in content
    assert "Agent:" in content
    assert "Hi there" in content


def test_chat_view_loading_state(qtbot):
    """Проверка состояния загрузки."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    widget.set_loading(True)
    assert not widget.send_button.isEnabled()
    assert not widget.message_input.isEnabled()
    assert widget.status_label.text() == "Thinking..."

    widget.set_loading(False)
    assert widget.send_button.isEnabled()
    assert widget.message_input.isEnabled()
    assert widget.status_label.text() == "Ready"


def test_chat_view_clear(qtbot):
    """Проверка очистки истории."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    widget.append_message("user", "Hello")
    assert "Hello" in widget.history_browser.toPlainText()

    widget.clear_history()
    assert widget.history_browser.toPlainText().strip() == ""


def test_chat_view_autoscroll(qtbot):
    """Проверка автоматической прокрутки при добавлении сообщений."""
    widget = ChatViewWidget()
    widget.resize(400, 300)
    qtbot.addWidget(widget)
    widget.show()

    # Добавляем много текста, чтобы появился скролл
    for i in range(50):
        widget.append_message("agent", f"Line {i}")

    scrollbar = widget.history_browser.verticalScrollBar()
    assert scrollbar.value() == scrollbar.maximum()


def test_chat_view_markdown_rendering(qtbot):
    """Проверка рендеринга Markdown (жирный текст, код)."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    widget.append_message("agent", "**Bold Text** and `code block`")

    # Проверяем что текст вообще есть
    plain_text = widget.history_browser.toPlainText()
    assert "Bold Text" in plain_text
    assert "code block" in plain_text

    # Проверяем HTML на наличие признаков форматирования.
    # Qt переваривает <strong> в стили или свои теги.
    html = widget.history_browser.toHtml()
    assert "Bold Text" in html
    assert "code block" in html
    # Обычно код оборачивается в <code> или моноширинный шрифт
    assert "code" in html.lower() or "monospace" in html.lower()


def test_chat_view_attach_signal(qtbot):
    """Проверка сигнала filesSelected при выборе файлов."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    spy = []
    widget.filesSelected.connect(lambda files: spy.append(files))

    # Т.к. QFileDialog.getOpenFileNames - статический метод, который открывает модальное окно,
    # нам нужно его подменить (mock), чтобы тест не завис.
    with patch("PySide6.QtWidgets.QFileDialog.getOpenFileNames", return_value=(["file1.txt", "file2.png"], "All Files (*)")):
        qtbot.mouseClick(widget.attach_button, Qt.MouseButton.LeftButton)

    assert len(spy) == 1
    assert spy[0] == ["file1.txt", "file2.png"]


def test_chat_view_attachments_preview(qtbot):
    """Проверка отображения превью вложений в области ввода."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)
    widget.show()

    # Изначально скрыто
    assert widget.attachments_scroll.isHidden()

    # Имитируем выбор файлов
    files = ["test.txt", "test.png"]
    with patch("PySide6.QtWidgets.QFileDialog.getOpenFileNames", return_value=(files, "All Files (*)")):
        qtbot.mouseClick(widget.attach_button, Qt.MouseButton.LeftButton)

    assert not widget.attachments_scroll.isHidden()
    # count() - 1 потому что в конце stretch
    assert widget.attachments_layout.count() == 3

    # Проверка удаления
    # Ищем кнопку удаления в первом виджете
    preview_widget = widget.attachments_layout.itemAt(0).widget()
    remove_btn = preview_widget.findChild(QPushButton)
    assert remove_btn.text() == "×"

    qtbot.mouseClick(remove_btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: widget.attachments_layout.count() == 2, timeout=1000)
    assert len(widget._attachments) == 1

    # Проверка очистки при отправке
    widget.message_input.setPlainText("test")
    qtbot.mouseClick(widget.send_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: widget.attachments_scroll.isHidden(), timeout=1000)
    assert len(widget._attachments) == 0
