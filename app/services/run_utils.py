from __future__ import annotations

from typing import Any, Optional


MISSING = object()


def clean_text(value: Any, *, max_len: int = 256) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def clean_optional_text(value: Any, *, max_len: int = 256) -> Optional[str]:
    text = clean_text(value, max_len=max_len)
    return text or None


def as_list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = clean_text(item, max_len=128)
        if not cleaned or cleaned in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned)
    return result


def nested_get(payload: Any, path: str) -> Any:
    current = payload
    for token in [part for part in str(path or "").split(".") if part]:
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        return MISSING
    return current
