"""Language resolution — reads config, no writes."""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from config import AppConfig
    from telegram import Update

SUPPORTED_LANGS: frozenset[str] = frozenset({"ru", "en", "zh", "de"})
FALLBACK_LANG: str = "ru"

# BCP-47 prefix → supported lang code.
_TELEGRAM_CODE_MAP: dict[str, str] = {
    "ru": "ru",
    "en": "en",
    "zh": "zh",
    "de": "de",
    # common variants
    "zh-hans": "zh",
    "zh-hant": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh-hk": "zh",
    "en-us": "en",
    "en-gb": "en",
    "en-au": "en",
    "de-at": "de",
    "de-ch": "de",
}


def map_telegram_language_code(code: Optional[str]) -> Optional[str]:
    """Map a BCP-47 Telegram language_code to a supported lang.

    Returns None if no mapping exists. Case-insensitive.
    """
    if not code or not isinstance(code, str):
        return None
    normalized = code.strip().lower()
    if normalized in _TELEGRAM_CODE_MAP:
        return _TELEGRAM_CODE_MAP[normalized]
    # Try prefix match: "en-something" → "en"
    prefix = normalized.split("-")[0]
    if prefix in SUPPORTED_LANGS:
        return prefix
    return None


def resolve_language(
    user_id: Optional[int],
    telegram_language_code: Optional[str],
    config: "AppConfig",
) -> str:
    """Canonical 4-step language resolution (design doc §2).

    1. Explicit saved value in config.telegram.user_languages[user_id].
    2. Auto-detect from telegram_language_code (NOT saved here — caller's job).
    3. config.defaults.default_language.
    4. Hard fallback "ru".
    """
    # Step 1: explicit per-user preference
    if user_id is not None:
        user_languages = getattr(getattr(config, "telegram", None), "user_languages", None) or {}
        saved = user_languages.get(user_id)
        if saved and saved in SUPPORTED_LANGS:
            return saved

    # Step 2: auto-detect
    detected = map_telegram_language_code(telegram_language_code)
    if detected:
        return detected

    # Step 3: app-level default
    default = getattr(getattr(config, "defaults", None), "default_language", FALLBACK_LANG)
    if default and default in SUPPORTED_LANGS:
        return default

    # Step 4: hard fallback
    return FALLBACK_LANG


def lang_from_update(update: "Update", config: "AppConfig") -> str:
    """Resolve language from a Telegram Update.

    Priority: user_languages[effective_user.id] → auto → default → ru.
    """
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    lang_code = getattr(user, "language_code", None)
    return resolve_language(user_id, lang_code, config)


def lang_from_query(query: object, config: "AppConfig") -> str:
    """Resolve language from a CallbackQuery.

    Uses query.from_user.id — correct in both private chats and groups.
    """
    from_user = getattr(query, "from_user", None)
    user_id = getattr(from_user, "id", None)
    lang_code = getattr(from_user, "language_code", None)
    return resolve_language(user_id, lang_code, config)
