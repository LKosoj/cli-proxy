"""Public API for the i18n package."""
from i18n.resolver import (
    SUPPORTED_LANGS,
    FALLBACK_LANG,
    map_telegram_language_code,
    resolve_language,
)
from i18n.translator import t, get_catalog, reload_catalogs
from i18n.plural import plural
from i18n.language_names import get_language_name, LANGUAGE_NAMES

__all__ = [
    "SUPPORTED_LANGS",
    "FALLBACK_LANG",
    "map_telegram_language_code",
    "resolve_language",
    "t",
    "get_catalog",
    "reload_catalogs",
    "plural",
    "get_language_name",
    "LANGUAGE_NAMES",
]
