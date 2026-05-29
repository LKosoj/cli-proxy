import pytest
import os
import tempfile
from desktop.widgets.chat_view import ChatViewWidget


@pytest.fixture
def temp_image():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    # Create a minimal valid PNG
    with open(path, "wb") as f:
        # Minimal 1x1 black PNG
        hex_data = (
            "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e0135"
            "0000000a4944415408d76360000000020001e221bc330000000049454e44ae426082"
        )
        f.write(bytes.fromhex(hex_data))
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_chat_view_render_image_attachment(qtbot, temp_image):
    """Проверка рендеринга изображения во вложении в истории."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    attachments = [
        {
            "kind": "image",
            "name": "test.png",
            "stored_path": temp_image,
            "ext": ".png",
            "size_bytes": 1024
        }
    ]

    widget.append_message("user", "Look at this image", attachments=attachments)

    html = widget.history_browser.toHtml()
    assert "test.png" in html
    assert "file://" in html
    assert "img" in html.lower()
    # Path should be in the src. Replace backslashes for comparison.
    assert temp_image.replace("\\", "/") in html.replace("\\", "/")


def test_chat_view_render_file_attachment(qtbot):
    """Проверка рендеринга файла во вложении в истории."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    attachments = [
        {
            "kind": "file",
            "name": "data.txt",
            "original_path": "/tmp/data.txt",
            "ext": ".txt",
            "size_bytes": 2048
        }
    ]

    widget.append_message("agent", "I have attached the logs", attachments=attachments)

    html = widget.history_browser.toHtml()
    assert "data.txt" in html
    assert "📎" in html
    assert "2.0 KB" in html
    assert "TXT" in html


def test_chat_view_render_mixed_attachments(qtbot, temp_image):
    """Проверка рендеринга смешанных вложений."""
    widget = ChatViewWidget()
    qtbot.addWidget(widget)

    attachments = [
        {
            "kind": "image",
            "name": "photo.jpg",
            "stored_path": temp_image,
            "ext": ".jpg",
            "size_bytes": 50000
        },
        {
            "kind": "file",
            "name": "report.pdf",
            "ext": ".pdf",
            "size_bytes": 1000000
        }
    ]

    widget.append_message("user", "Multiple files", attachments=attachments)

    html = widget.history_browser.toHtml()
    assert "photo.jpg" in html
    assert "report.pdf" in html
    assert "48.8 KB" in html
    assert "976.6 KB" in html
    assert "PDF" in html
