"""Language resolution utilities for background tasks and modes."""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from config import AppConfig

FALLBACK_LANG = "ru"


def resolve_user_lang(
    config: "AppConfig",
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
) -> str:
    """Resolve language for background task initiator.

    Tries user_languages[user_id], then user_languages[chat_id]
    (for private chats they are the same), then default_language, then "ru".

    Does NOT auto-detect from Telegram language_code — that is done at inbound
    boundary. This helper is for use in modes/agent/summary where no update object
    is available.
    """
    from i18n.resolver import SUPPORTED_LANGS

    user_languages: dict = getattr(getattr(config, "telegram", None), "user_languages", None) or {}

    if user_id is not None:
        lang = user_languages.get(user_id)
        if lang and lang in SUPPORTED_LANGS:
            return lang

    if chat_id is not None and chat_id != user_id:
        lang = user_languages.get(chat_id)
        if lang and lang in SUPPORTED_LANGS:
            return lang

    default = getattr(getattr(config, "defaults", None), "default_language", FALLBACK_LANG)
    if default and default in SUPPORTED_LANGS:
        return default

    return FALLBACK_LANG
