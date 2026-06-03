"""Catalog-based translator. Thread-safe, cached, dot-notation keys."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCALES_DIR = Path(__file__).parent.parent / "locales"
_FALLBACK_LANG = "ru"

_cache: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _load_catalog(lang: str) -> dict[str, Any]:
    path = _LOCALES_DIR / f"{lang}.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("i18n catalog not found: %s", path)
        return {}
    except json.JSONDecodeError as exc:
        logger.error("i18n catalog JSON error lang=%s: %s", lang, exc)
        return {}


def get_catalog(lang: str) -> dict[str, Any]:
    """Return loaded catalog for *lang*, loading on first call (cached)."""
    with _lock:
        if lang not in _cache:
            _cache[lang] = _load_catalog(lang)
        return _cache[lang]


def reload_catalogs() -> None:
    """Invalidate in-memory catalog cache (all languages)."""
    with _lock:
        _cache.clear()


def _resolve_key(catalog: dict[str, Any], key: str) -> Any:
    """Traverse dot-notation key in nested dict. Returns None if missing."""
    parts = key.split(".")
    current: Any = catalog
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def t(key: str, lang: str, /, **params: Any) -> str:
    """Translate *key* for *lang* with optional parameter substitution.

    ``lang`` is positional-only (PEP 570) to allow ``lang`` as a template param:
    ``t("msg.lang.changed", "en", lang="English")``.

    Fallback chain:
      1. catalog[lang][key]
      2. catalog["ru"][key]
      3. key itself (+ logger.warning)

    Parameter substitution via str.format(**params).
    On missing parameter: warning + return template without substitution.
    On unsupported lang: treat as "ru".
    """
    from i18n.resolver import SUPPORTED_LANGS  # avoid circular at module level

    _lang = lang
    if _lang not in SUPPORTED_LANGS:
        logger.warning("i18n: unsupported lang=%r, falling back to ru", _lang)
        _lang = _FALLBACK_LANG

    value = _resolve_key(get_catalog(_lang), key)
    if value is None and _lang != _FALLBACK_LANG:
        logger.warning("i18n: key %r missing in lang=%r, falling back to ru", key, _lang)
        value = _resolve_key(get_catalog(_FALLBACK_LANG), key)
    if value is None:
        logger.warning("i18n: key %r missing in all catalogs, returning key", key)
        return key

    if not isinstance(value, str):
        # key points to a nested dict or list — caller error
        logger.warning(
            "i18n: key %r resolved to non-string %r, returning key", key, type(value).__name__
        )
        return key

    if not params:
        return value

    try:
        return value.format(**params)
    except (KeyError, IndexError) as exc:
        logger.warning(
            "i18n: substitution failed for key=%r params=%r: %s", key, params, exc
        )
        return value
