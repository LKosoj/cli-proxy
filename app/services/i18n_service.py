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
    Retries up to 3 times on revision mismatch.
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
        for _attempt in range(3):
            result = await config_service.set_user_language(user_id, lang, max_retries=1)
            if result.ok:
                return
            if result.errors and "revision mismatch" in result.errors:
                continue  # retry
            logger.warning(
                "maybe_persist_user_language failed user_id=%d lang=%s errors=%s",
                user_id, lang, result.errors,
            )
            return
        logger.warning(
            "maybe_persist_user_language gave up after 3 retries user_id=%d lang=%s",
            user_id, lang,
        )
    except Exception:
        logger.exception(
            "maybe_persist_user_language unexpected error user_id=%d", user_id
        )
