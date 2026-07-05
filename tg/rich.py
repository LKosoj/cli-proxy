from __future__ import annotations


RICH_MARKDOWN_CHAR_LIMIT = 32768


def rich_markdown_chars(markdown: str) -> int:
    return len(str(markdown or ""))


def is_rich_markdown_eligible(
    markdown: str,
    *,
    max_chars: int = RICH_MARKDOWN_CHAR_LIMIT,
) -> bool:
    return rich_markdown_chars(markdown) <= max_chars


def build_input_rich_message(
    markdown: str,
    *,
    skip_entity_detection: bool | None = None,
    is_rtl: bool | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"markdown": markdown}
    if skip_entity_detection is not None:
        payload["skip_entity_detection"] = bool(skip_entity_detection)
    if is_rtl is not None:
        payload["is_rtl"] = bool(is_rtl)
    return payload
