"""Входящие Rich Messages (Bot API 10.1+): плоский текст из блоков.

python-telegram-bot до Bot API 9.5 включительно не знает поле ``Message.rich_message``
и складывает его как есть в ``message.api_kwargs``; ``text`` у такого сообщения пуст,
поэтому ``filters.TEXT`` его не ловит. Здесь блоки разворачиваются в обычный текст,
который дальше идёт по обычному пути обработки.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from telegram.ext import filters

_INLINE_TEXT_TYPES = frozenset(
    {
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "date_time",
        "text_mention",
        "subscript",
        "superscript",
        "marked",
        "code",
        "url",
        "email_address",
        "phone_number",
        "bank_card_number",
        "mention",
        "hashtag",
        "cashtag",
        "bot_command",
        "anchor_link",
        "reference",
        "reference_link",
    }
)


def rich_message_payload(message: Any) -> Optional[dict]:
    api_kwargs = getattr(message, "api_kwargs", None) or {}
    payload = api_kwargs.get("rich_message") if isinstance(api_kwargs, Mapping) else None
    return payload if isinstance(payload, dict) else None


def rich_text_to_plain(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(rich_text_to_plain(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind = str(node.get("type") or "")
    if kind == "url":
        text = rich_text_to_plain(node.get("text"))
        url = str(node.get("url") or "")
        return f"{text} ({url})" if url and url != text.strip() else text
    if kind in _INLINE_TEXT_TYPES:
        return rich_text_to_plain(node.get("text"))
    if kind == "custom_emoji":
        return str(node.get("alternative_text") or "")
    if kind == "mathematical_expression":
        return str(node.get("expression") or "")
    # anchor, button и неизвестные типы текста не несут: пропускаем.
    return ""


def _caption_text(block: dict) -> str:
    caption = block.get("caption")
    if isinstance(caption, dict):
        return rich_text_to_plain(caption.get("text")).strip()
    return ""


def _blocks_to_lines(blocks: Any) -> list[str]:
    lines: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in ("paragraph", "heading", "footer", "pullquote", "expandable_blockquote"):
            text = rich_text_to_plain(block.get("text")).strip()
            if text:
                lines.append(text)
        elif kind == "pre":
            code = rich_text_to_plain(block.get("text")).strip("\n")
            if code:
                language = str(block.get("language") or "")
                lines.append(f"```{language}\n{code}\n```")
        elif kind == "divider":
            lines.append("---")
        elif kind == "mathematical_expression":
            expression = str(block.get("expression") or "").strip()
            if expression:
                lines.append(expression)
        elif kind == "list":
            for item in block.get("items") if isinstance(block.get("items"), list) else []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "-").strip() or "-"
                body = "\n".join(_blocks_to_lines(item.get("blocks")))
                if body:
                    lines.append(f"{label} {body}")
        elif kind == "blockquote":
            inner = "\n".join(_blocks_to_lines(block.get("blocks")))
            if inner:
                lines.append("\n".join(f"> {line}" for line in inner.splitlines()))
        elif kind == "details":
            summary = rich_text_to_plain(block.get("summary")).strip()
            if summary:
                lines.append(summary)
            lines.extend(_blocks_to_lines(block.get("blocks")))
        elif kind == "table":
            rows = block.get("cells") if isinstance(block.get("cells"), list) else []
            for row in rows:
                cells = [
                    rich_text_to_plain(cell.get("text")).strip()
                    for cell in (row if isinstance(row, list) else [])
                    if isinstance(cell, dict)
                ]
                if any(cells):
                    lines.append(" | ".join(cells))
            caption = _caption_text(block)
            if caption:
                lines.append(caption)
        elif kind in ("collage", "slideshow"):
            lines.extend(_blocks_to_lines(block.get("blocks")))
            caption = _caption_text(block)
            if caption:
                lines.append(caption)
        else:
            # photo, video, animation, audio, voice_note, document, map, buttons, anchor:
            # берём только подпись (без credit), сами вложения через rich message не скачиваем.
            caption = _caption_text(block)
            if caption:
                lines.append(caption)
    return lines


def rich_message_to_text(payload: Optional[dict]) -> str:
    if not isinstance(payload, dict):
        return ""
    return "\n\n".join(_blocks_to_lines(payload.get("blocks"))).strip()


def rich_message_text(message: Any) -> Optional[str]:
    """Плоский текст rich-сообщения или None, если это не rich-сообщение."""
    payload = rich_message_payload(message)
    if payload is None:
        return None
    return rich_message_to_text(payload)


class RichMessageFilter(filters.MessageFilter):
    """Сообщение с ``rich_message`` и без обычного ``text``."""

    def filter(self, message: Any) -> bool:
        return not getattr(message, "text", None) and rich_message_payload(message) is not None


RICH_MESSAGE = RichMessageFilter(name="tg.rich_message")
