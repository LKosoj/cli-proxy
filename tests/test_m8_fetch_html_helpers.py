"""M8 group-1: проверяет устранение дублирования html-хелперов в fetch-логике."""
from __future__ import annotations


def test_web_research_has_no_own_extract_title():
    """WebResearchTool не должен содержать собственный метод _extract_title."""
    from agent.plugins.web_research import WebResearchTool

    assert not hasattr(WebResearchTool, "_extract_title"), (
        "WebResearchTool должен использовать helpers._extract_html_title, а не иметь собственный _extract_title"
    )


def test_web_research_has_no_own_clean_html_content():
    """WebResearchTool не должен содержать собственный метод _clean_html_content."""
    from agent.plugins.web_research import WebResearchTool

    assert not hasattr(WebResearchTool, "_clean_html_content"), (
        "WebResearchTool должен использовать helpers._clean_html_with_bs4, а не иметь собственный _clean_html_content"
    )


def test_web_research_has_no_own_clean_extra_spaces():
    """WebResearchTool не должен содержать собственный метод _clean_extra_spaces."""
    from agent.plugins.web_research import WebResearchTool

    assert not hasattr(WebResearchTool, "_clean_extra_spaces"), (
        "WebResearchTool должен использовать helpers.clean_extra_spaces, а не иметь собственный _clean_extra_spaces"
    )


def test_web_research_imports_helpers():
    """Модуль web_research должен импортировать хелперы из agent.tooling.helpers."""
    import agent.plugins.web_research as mod

    assert hasattr(mod, "_helpers_extract_title"), "Должен быть импортирован _helpers_extract_title из helpers"
    assert hasattr(mod, "_helpers_clean_html"), "Должен быть импортирован _helpers_clean_html из helpers"
    assert hasattr(mod, "_helpers_clean_extra_spaces"), "Должен быть импортирован _helpers_clean_extra_spaces из helpers"


def test_extract_title_empty_html_returns_bez_zagolovka():
    """При пустом html helpers._extract_html_title возвращает '', а web_research-обёртка — 'Без заголовка'."""
    from agent.tooling.helpers import extract_html_title

    raw_result = extract_html_title("")
    assert raw_result == "", f"helpers.extract_html_title('') должен вернуть '', получено: {raw_result!r}"

    # Поведение web_research: `extract_html_title(html) or "Без заголовка"`
    result = extract_html_title("") or "Без заголовка"
    assert result == "Без заголовка", f"Ожидалось 'Без заголовка', получено: {result!r}"


def test_extract_title_with_title_tag():
    """helpers.extract_html_title извлекает заголовок из <title>."""
    from agent.tooling.helpers import extract_html_title

    html = "<html><head><title>Test Page</title></head><body></body></html>"
    result = extract_html_title(html)
    assert result == "Test Page", f"Ожидалось 'Test Page', получено: {result!r}"

    # Такой же результат через web_research-обёртку
    result_wr = extract_html_title(html) or "Без заголовка"
    assert result_wr == "Test Page"


def test_clean_html_on_exception_fallback_returns_original():
    """
    При ошибке парсинга helpers.clean_html_with_bs4 возвращает '' (пусто).
    web_research-обёртка: `_helpers_clean_html(html) or html` — т.е. при пустом результате
    возвращает исходный html (сохранение поведения оригинального _clean_html_content).
    """
    from unittest.mock import patch

    from agent.tooling.helpers import clean_html_with_bs4

    original_html = "<bad-html>content</bad-html>"

    # bs4 импортируется локально внутри функции; патчим через bs4 напрямую
    with patch("bs4.BeautifulSoup", side_effect=RuntimeError("bs4 сломан")):
        result = clean_html_with_bs4(original_html)
    assert result == "", f"helpers.clean_html_with_bs4 при исключении должен вернуть '', получено: {result!r}"

    # web_research-обёртка при пустом результате должна вернуть исходный html
    fallback = result or original_html
    assert fallback == original_html, f"Ожидался исходный html как fallback, получено: {fallback!r}"


def test_clean_html_with_bs4_removes_scripts():
    """helpers.clean_html_with_bs4 корректно удаляет <script> и возвращает текст."""
    from agent.tooling.helpers import clean_html_with_bs4

    html = "<html><body><script>alert(1)</script><p>Привет</p></body></html>"
    result = clean_html_with_bs4(html)
    assert "alert" not in result, "Script-теги должны быть удалены"
    assert "Привет" in result, "Основной текст должен сохраниться"


def test_helpers_public_aliases_exist():
    """helpers.py должен экспортировать публичные алиасы extract_html_title, clean_html_with_bs4, clean_extra_spaces."""
    import agent.tooling.helpers as helpers_mod

    assert callable(getattr(helpers_mod, "extract_html_title", None)), "extract_html_title должен быть публичным в helpers"
    assert callable(getattr(helpers_mod, "clean_html_with_bs4", None)), "clean_html_with_bs4 должен быть публичным в helpers"
    assert callable(getattr(helpers_mod, "clean_extra_spaces", None)), "clean_extra_spaces должен быть публичным в helpers"
