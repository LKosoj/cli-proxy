import os

from tg.markdown import to_markdown_v2
from utils.html_renderer import ansi_to_html, make_html_file, render_html, render_markdown


def test_render_html_matches_ansi_to_html_output() -> None:
    src = "# Заголовок\nТекст"
    assert render_html(src) == ansi_to_html(src)


def test_render_markdown_uses_telegram_markdown_v2_converter() -> None:
    src = "**bold** [link](https://example.com)"
    assert render_markdown(src) == to_markdown_v2(src)


def test_make_html_file_creates_file_with_content() -> None:
    path = make_html_file("<b>ok</b>", "renderer-test")
    try:
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == "<b>ok</b>"
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_botapp_has_no_html_markdown_renderer_methods() -> None:
    from bot import BotApp

    assert "ansi_to_html" not in BotApp.__dict__
    assert "make_html_file" not in BotApp.__dict__
