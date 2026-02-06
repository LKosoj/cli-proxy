from __future__ import annotations

import re


_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_RE = re.compile(r"([\\_*\\[\\]()~`>#+\\-=|{}.!])")


def to_markdown_v2(text: str) -> str:
    """
    Convert/escape text for Telegram MarkdownV2.

    Preferred implementation uses md2tgmd (handles a few Markdown patterns).
    Fallback escapes all MarkdownV2 specials conservatively.
    """
    if text is None:
        return ""
    try:
        import md2tgmd  # type: ignore

        # md2tgmd.escape() escapes MarkdownV2 special chars and normalizes some Markdown patterns.
        return md2tgmd.escape(str(text))
    except Exception:
        s = str(text)
        # Escape all specials. This makes the message render as plain text (safe default).
        return _MDV2_RE.sub(r"\\\\\\1", s)

