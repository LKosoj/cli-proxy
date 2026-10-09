from __future__ import annotations

from html import escape

from telegram import InlineKeyboardMarkup


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


def build_rich_message_payload(markdown: str, reply_markup=None) -> dict[str, object]:
    """Embed callback/URL buttons; retain other Telegram keyboard types."""
    if not isinstance(reply_markup, InlineKeyboardMarkup) or any(
        set(button.to_dict()) - {"text", "callback_data", "url", "style"}
        or (button.callback_data is None and button.url is None)
        for row in reply_markup.inline_keyboard for button in row
    ):
        payload = {"rich_message": build_input_rich_message(markdown)}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return payload
    rows = []
    for row in reply_markup.inline_keyboard:
        buttons = []
        for button in row:
            attributes = (
                f'type="callback_data" data="{escape(button.callback_data, quote=True)}"'
                if button.callback_data is not None
                else f'type="url" url="{escape(button.url, quote=True)}"'
            )
            style = button.to_dict().get("style")
            if style:
                attributes += f' style="{escape(style, quote=True)}"'
            buttons.append(f'<tg-button {attributes}>{escape(button.text)}</tg-button>')
        rows.append("<tg-button-row>" + "".join(buttons) + "</tg-button-row>")
    content = markdown + "\n\n" + "\n\n".join(rows)
    return {
        "rich_message": build_input_rich_message(content, skip_entity_detection=True),
        "reply_markup": None,
    }
