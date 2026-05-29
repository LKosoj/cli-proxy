from __future__ import annotations

import logging
from typing import Any, Optional

from config import AppConfig


logger = logging.getLogger(__name__)


class ThreadModeCapabilityChecker:
    def __init__(
        self,
        config: AppConfig,
        *,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._logger = logger_ or logger

    def requires_topics(self) -> bool:
        thread_mode = getattr(self._config, "thread_mode", None)
        if thread_mode is None:
            return False
        return bool(getattr(thread_mode, "enabled", False))

    async def ensure_supported(self, bot: Any) -> None:
        if not self.requires_topics():
            return

        me = await bot.get_me()
        thread_mode = getattr(self._config, "thread_mode", None)
        mode = str(getattr(thread_mode, "mode", "") or "").strip()
        topics_chat_id = getattr(thread_mode, "topics_chat_id", None)
        identity = str(getattr(me, "username", "") or getattr(me, "id", "") or "-")
        if getattr(me, "has_topics_enabled", None) is not True:
            self._fail(
                identity=identity,
                mode=mode,
                topics_chat_id=topics_chat_id,
                reason="bot.get_me().has_topics_enabled must be True",
            )

        if mode not in {"private", "group"}:
            self._fail(
                identity=identity,
                mode=mode,
                topics_chat_id=topics_chat_id,
                reason="thread_mode.mode must be either private or group",
            )

        if not callable(getattr(bot, "create_forum_topic", None)) or not callable(getattr(bot, "edit_forum_topic", None)):
            self._fail(
                identity=identity,
                mode=mode,
                topics_chat_id=topics_chat_id,
                reason="telegram topic API methods create_forum_topic/edit_forum_topic are unavailable",
            )

        if mode == "private":
            return

        if topics_chat_id is None:
            self._fail(
                identity=identity,
                mode=mode,
                topics_chat_id=topics_chat_id,
                reason="thread_mode.topics_chat_id is required for group mode",
            )

        chat = await bot.get_chat(int(topics_chat_id))
        if bool(getattr(chat, "is_forum", False)):
            return

        self._fail(
            identity=identity,
            mode=mode,
            topics_chat_id=topics_chat_id,
            reason="target topics chat is not a forum-enabled supergroup (Chat.is_forum must be True)",
        )

    def _fail(self, *, identity: str, mode: str, topics_chat_id: Any, reason: str) -> None:
        self._logger.critical(
            "thread mode capability check failed: thread_mode.mode=%s requires Thread Mode support, but bot=%s "
            "did not pass startup validation (%s, topics_chat_id=%s). Enable Thread Mode in BotFather and restart: "
            "BotFather -> /mybots -> <bot> -> Bot Settings -> Thread Mode.",
            str(mode or ""),
            identity,
            str(reason or ""),
            topics_chat_id,
        )
        raise SystemExit(1)
