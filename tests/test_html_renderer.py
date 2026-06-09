import os
from unittest.mock import MagicMock

import pytest

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


def test_full_html_document_includes_responsive_mobile_layout() -> None:
    result = ansi_to_html("# Title\n\n| Column | Value |\n| --- | --- |\n| key | value |\n\n```text\nx\n```")

    assert "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">" in result
    assert "html{box-sizing:border-box;-webkit-text-size-adjust:100%;}" in result
    assert "main{max-width:100%;}" in result
    assert "table{border-collapse:collapse;margin:12px 0;display:block;max-width:100%;" in result
    assert "overflow:auto;max-width:100%;white-space:pre;-webkit-overflow-scrolling:touch;" in result
    assert "img,svg{max-width:100%;height:auto;}" in result
    assert "@media (max-width:600px)" in result
    assert "<body><main>" in result
    assert "</main></body></html>" in result


def test_html_fragment_does_not_include_document_mobile_shell() -> None:
    result = ansi_to_html("# Title\n\nText", fragment=True)

    assert "<!doctype html>" not in result
    assert "<meta name=\"viewport\"" not in result
    assert "<body><main>" not in result


def test_botapp_has_no_html_markdown_renderer_methods() -> None:
    from bot import BotApp

    assert "ansi_to_html" not in BotApp.__dict__
    assert "make_html_file" not in BotApp.__dict__


_MERMAID_SRC = "```mermaid\ngraph TD\n  A --> B\n```"


def test_mermaid_no_network_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """При allow_network_fetch=False (дефолт) requests.get не вызывается."""
    mock_get = MagicMock()
    monkeypatch.setattr("utils.html_renderer.requests.get", mock_get)

    result = ansi_to_html(_MERMAID_SRC)

    mock_get.assert_not_called()
    # Исходный блок сохраняется как есть (попадает в pre/code через markdown-it)
    assert "mermaid" in result


def test_mermaid_network_called_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """При allow_network_fetch=True requests.get вызывается с URL mermaid.ink."""
    mock_response = MagicMock()
    mock_response.ok = False  # SVG не вернём — нас интересует только факт вызова
    mock_get = MagicMock(return_value=mock_response)
    monkeypatch.setattr("utils.html_renderer.requests.get", mock_get)

    ansi_to_html(_MERMAID_SRC, allow_network_fetch=True)

    mock_get.assert_called_once()
    called_url: str = mock_get.call_args[0][0]
    assert "mermaid.ink" in called_url


def test_render_html_no_network_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """render_html тоже не делает сетевых запросов без явного разрешения."""
    mock_get = MagicMock()
    monkeypatch.setattr("utils.html_renderer.requests.get", mock_get)

    render_html(_MERMAID_SRC)

    mock_get.assert_not_called()


def test_plain_text_renders_correctly() -> None:
    """Обычный текст рендерится в HTML как раньше, независимо от флага."""
    src = "**bold** and _italic_"
    result_default = ansi_to_html(src)
    result_explicit = ansi_to_html(src, allow_network_fetch=True)
    # Оба варианта должны содержать HTML-тег strong/em
    assert "<strong>" in result_default
    assert result_default == result_explicit
