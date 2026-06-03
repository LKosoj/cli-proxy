"""i18n service utilities — fire-and-forget language persistence."""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from config import AppConfig
    from app.services.config_service import ConfigService

logger = logging.getLogger(__name__)


async def maybe_persist_user_language(
    user_id: int,
    telegram_language_code: Optional[str],
    config: "AppConfig",
    config_service: "ConfigService",
) -> None:
    """Fire-and-forget: write auto-detected lang on first contact.

    Idempotent: no-op if user_id already in user_languages.
    Skips unknown language codes silently.
    Retry-on-revision-mismatch is delegated to ConfigService.set_user_language.
    Never raises — logs on error.
    """
    from i18n.resolver import map_telegram_language_code

    # Idempotency check on in-memory config (cheap)
    if user_id in config.telegram.user_languages:
        return

    lang = map_telegram_language_code(telegram_language_code)
    if lang is None:
        return  # unsupported/missing code — fallback will handle at runtime

    try:
        # set_user_language retries internally on revision mismatch (re-reading
        # the config between attempts), so a single call is sufficient here.
        result = await config_service.set_user_language(user_id, lang)
        if result.ok:
            # Keep the live in-memory config coherent so resolve_user_lang() sees
            # the persisted choice immediately, without waiting for a full reload.
            config.telegram.user_languages[user_id] = lang
        else:
            logger.warning(
                "maybe_persist_user_language failed user_id=%d lang=%s errors=%s",
                user_id, lang, result.errors,
            )
    except Exception:
        logger.exception(
            "maybe_persist_user_language unexpected error user_id=%d", user_id
        )
