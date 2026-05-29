from __future__ import annotations

import re
from typing import Iterable

from telegram import MessageEntity as TelegramMessageEntity


_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_LOCAL_PATH_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((/[^)\n]+)\)")


def _normalize_local_path_links(text: str) -> str:
    """
    Convert local workspace markdown links to plain inline code form.

    Telegram MarkdownV2 often rejects links with local paths like `/srv/...`.
    We keep content readable instead of sending a broken entity.
    """
    s = str(text or "")

    def _replace(match: re.Match) -> str:
        label = str(match.group(1) or "").strip()
        path = str(match.group(2) or "").strip()
        if not label:
            return f"`{path}`"
        return f"`{label}` ({path})"

    return _LOCAL_PATH_LINK_RE.sub(_replace, s)


def escape_markdown_v2_all(text: str) -> str:
    """
    Escape *all* Telegram MarkdownV2 special characters.

    This is the "safe" mode: it prevents entity parse errors at the cost of
    disabling any intended Markdown formatting in the input.
    """
    if text is None:
        return ""
    s = _normalize_local_path_links(text)
    # Prefix every special char with a single backslash.
    return _MDV2_RE.sub(r"\\\1", s)


def _library_entity_to_telegram(entity) -> TelegramMessageEntity:
    return TelegramMessageEntity(
        type=str(getattr(entity, "type", "") or ""),
        offset=int(getattr(entity, "offset", 0) or 0),
        length=int(getattr(entity, "length", 0) or 0),
        url=getattr(entity, "url", None),
        language=getattr(entity, "language", None),
        custom_emoji_id=getattr(entity, "custom_emoji_id", None),
    )


def _telegram_entity_to_library(entity):
    from telegramify_markdown.entity import MessageEntity as LibraryMessageEntity

    return LibraryMessageEntity(
        type=str(getattr(entity, "type", "") or ""),
        offset=int(getattr(entity, "offset", 0) or 0),
        length=int(getattr(entity, "length", 0) or 0),
        url=getattr(entity, "url", None),
        language=getattr(entity, "language", None),
        custom_emoji_id=getattr(entity, "custom_emoji_id", None),
    )


def to_telegram_entities(text: str) -> tuple[str, list[TelegramMessageEntity]]:
    """
    Convert Markdown-ish text to Telegram plain text + entities.

    Falls back to literal text without entities if telegramify-markdown is not
    available or conversion fails.
    """
    if text is None:
        return "", []
    normalized = _normalize_local_path_links(text)
    try:
        from telegramify_markdown import convert

        plain_text, entities = convert(str(normalized))
        return str(plain_text or ""), [_library_entity_to_telegram(entity) for entity in (entities or [])]
    except Exception:
        return str(normalized), []


def split_telegram_entities(
    text: str,
    entities: Iterable[TelegramMessageEntity] | None,
    *,
    max_utf16_len: int,
) -> list[tuple[str, list[TelegramMessageEntity]]]:
    """
    Split Telegram text/entities into chunks under the UTF-16 limit.

    Falls back to a single chunk if telegramify-markdown is unavailable.
    """
    if text is None:
        return [("", [])]
    try:
        from telegramify_markdown import split_entities

        library_entities = [_telegram_entity_to_library(entity) for entity in (entities or [])]
        chunks = split_entities(str(text), library_entities, int(max_utf16_len))
        return [
            (str(chunk_text or ""), [_library_entity_to_telegram(entity) for entity in (chunk_entities or [])])
            for chunk_text, chunk_entities in chunks
        ]
    except Exception:
        return [(str(text), list(entities or []))]


def utf16_length(text: str) -> int:
    if text is None:
        return 0
    try:
        from telegramify_markdown import utf16_len

        return int(utf16_len(str(text)))
    except Exception:
        return len(str(text))


def to_markdown_v2(text: str) -> str:
    """
    Convert/escape text for Telegram MarkdownV2.

    Preferred implementation uses telegramify-markdown and converts its
    entity output back into MarkdownV2. Fallback escapes all specials.
    """
    if text is None:
        return ""
    normalized = _normalize_local_path_links(text)
    try:
        from telegramify_markdown import convert, entities_to_markdownv2

        plain_text, entities = convert(str(normalized))
        return entities_to_markdownv2(str(plain_text or ""), entities or [])
    except Exception:
        # Safe fallback: escape all specials (no formatting, but always parseable).
        return escape_markdown_v2_all(normalized)
