from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Optional

from telegram.error import BadRequest

from sessions.conversation_scope import ConversationScope
from utils.ui import format_session_title

from app.services.session_thread_repository import (
    SessionThreadMappingRecord,
    SessionThreadRepository,
)


logger = logging.getLogger(__name__)


class SessionThreadManagerError(RuntimeError):
    """Base error for Telegram session topic mapping."""


class SessionThreadManager:
    _TOPIC_TITLE_LIMIT = 128
    _DEFAULT_REPAIR_INTERVAL_SEC = 300.0
    _REPAIR_RECORD_DELAY_SEC = 4.0
    _STALE_TOPIC_TTL_SEC = 3600.0

    def __init__(self, repository: SessionThreadRepository, session_manager, thread_mode_config) -> None:
        self._repository = repository
        self._session_manager = session_manager
        self._config = thread_mode_config
        self._uid_by_topic: dict[tuple[int, int], str] = {}
        self._stale_topics: dict[tuple[int, int], float] = {}
        self._repair_task: asyncio.Task[None] | None = None

    def is_enabled(self) -> bool:
        if not bool(getattr(self._config, "enabled", False)):
            return False
        mode = self._thread_mode()
        if mode == "private":
            return True
        if mode == "group":
            return getattr(self._config, "topics_chat_id", None) is not None
        return False

    def resolve_session_uid(self, *, chat_id: int, message_thread_id: int) -> Optional[str]:
        key = (int(chat_id), int(message_thread_id))
        if key in self._uid_by_topic:
            return self._uid_by_topic[key]
        record = self._repository.get_by_topic(topics_chat_id=key[0], message_thread_id=key[1])
        if record is None:
            return None
        self._cache_record(record)
        return record.session_uid

    def mark_topic_stale(self, *, topics_chat_id: int, message_thread_id: int, reason: str = "") -> None:
        key = (int(topics_chat_id), int(message_thread_id))
        self._stale_topics[key] = time.time()
        logger.warning(
            "session thread marked stale chat_id=%s message_thread_id=%s reason=%s",
            key[0],
            key[1],
            str(reason or ""),
        )

    def _recent_stale_records(self, *, owner_chat_id: int) -> list[SessionThreadMappingRecord]:
        now = time.time()
        expired = [
            key
            for key, marked_at in self._stale_topics.items()
            if now - float(marked_at or 0.0) > self._STALE_TOPIC_TTL_SEC
        ]
        for key in expired:
            self._stale_topics.pop(key, None)

        records: list[SessionThreadMappingRecord] = []
        for topics_chat_id, message_thread_id in self._stale_topics:
            record = self._repository.get_by_topic(
                topics_chat_id=int(topics_chat_id),
                message_thread_id=int(message_thread_id),
            )
            if record is not None and int(record.owner_chat_id) == int(owner_chat_id):
                records.append(record)
        return records

    def bind_existing_topic_for_session(
        self,
        *,
        owner_chat_id: int,
        session,
        topics_chat_id: int,
        message_thread_id: int,
    ) -> SessionThreadMappingRecord:
        old_scope = getattr(session, "conversation_scope", None)
        if isinstance(old_scope, ConversationScope) and old_scope.message_thread_id is not None:
            self._drop_cached_topic(
                topics_chat_id=int(old_scope.chat_id),
                message_thread_id=int(old_scope.message_thread_id),
            )
            self._stale_topics.pop((int(old_scope.chat_id), int(old_scope.message_thread_id)), None)
        self._apply_scope(session, topics_chat_id=int(topics_chat_id), message_thread_id=int(message_thread_id))
        self._session_manager.persist_session(int(owner_chat_id), session.id)
        record = self._sync_mapping(owner_chat_id=int(owner_chat_id), session=session)
        self._cache_record(record)
        return record

    def rebind_recent_stale_session(
        self,
        *,
        owner_chat_id: int,
        topics_chat_id: int,
        message_thread_id: int,
    ):
        if self._repository.get_by_topic(
            topics_chat_id=int(topics_chat_id),
            message_thread_id=int(message_thread_id),
        ) is not None:
            return None
        candidates = self._recent_stale_records(owner_chat_id=int(owner_chat_id))
        if len(candidates) != 1:
            if len(candidates) > 1:
                logger.warning(
                    "session thread stale rebind skipped: ambiguous candidates owner_chat_id=%s "
                    "new_chat_id=%s new_thread_id=%s candidate_count=%s",
                    owner_chat_id,
                    topics_chat_id,
                    message_thread_id,
                    len(candidates),
                )
            return None

        record = candidates[0]
        session = self._session_manager.get(int(record.owner_chat_id), record.session_id)
        if session is None:
            return None
        old_chat_id = int(record.topics_chat_id)
        old_thread_id = int(record.message_thread_id)
        rebound = self.bind_existing_topic_for_session(
            owner_chat_id=int(record.owner_chat_id),
            session=session,
            topics_chat_id=int(topics_chat_id),
            message_thread_id=int(message_thread_id),
        )
        logger.warning(
            "session thread rebound stale mapping owner_chat_id=%s session_id=%s "
            "old_chat_id=%s old_thread_id=%s new_chat_id=%s new_thread_id=%s",
            record.owner_chat_id,
            record.session_id,
            old_chat_id,
            old_thread_id,
            rebound.topics_chat_id,
            rebound.message_thread_id,
        )
        return session

    async def ensure_topic_for_session(self, *, owner_chat_id: int, session, bot) -> Optional[SessionThreadMappingRecord]:
        if not self.is_enabled():
            return None
        if bot is None:
            raise SessionThreadManagerError("telegram bot is required for thread-mode topic creation")

        existing = self._repository.get_by_session(owner_chat_id=int(owner_chat_id), session_id=session.id)
        if existing is not None:
            self._apply_scope(session, topics_chat_id=existing.topics_chat_id, message_thread_id=existing.message_thread_id)
            self._session_manager.persist_session(int(owner_chat_id), session.id)
            self._cache_record(existing)
            return existing

        scope = getattr(session, "conversation_scope", None)
        if isinstance(scope, ConversationScope) and scope.message_thread_id is not None:
            record = self._sync_mapping(owner_chat_id=int(owner_chat_id), session=session)
            self._cache_record(record)
            return record

        title = self.build_topic_title(session)
        topic_chat_id = self._target_chat_id(owner_chat_id=int(owner_chat_id))
        forum_topic = await bot.create_forum_topic(
            chat_id=topic_chat_id,
            name=title,
        )
        thread_id = int(getattr(forum_topic, "message_thread_id", 0) or 0)
        if thread_id <= 0:
            raise SessionThreadManagerError("telegram create_forum_topic returned invalid message_thread_id")

        self._apply_scope(session, topics_chat_id=topic_chat_id, message_thread_id=thread_id)
        self._session_manager.persist_session(int(owner_chat_id), session.id)
        record = self._sync_mapping(owner_chat_id=int(owner_chat_id), session=session, topic_name=title)
        self._cache_record(record)
        return record

    async def rename_topic_for_session(self, *, owner_chat_id: int, session, bot) -> Optional[SessionThreadMappingRecord]:
        if not self.is_enabled():
            return None
        if bot is None:
            raise SessionThreadManagerError("telegram bot is required for thread-mode topic rename")
        scope = getattr(session, "conversation_scope", None)
        if not isinstance(scope, ConversationScope) or scope.message_thread_id is None:
            return None
        title = self.build_topic_title(session)
        record = self._repository.get_by_session(owner_chat_id=int(owner_chat_id), session_id=session.id)
        if record is not None:
            repaired_record = await self._ensure_topic_present(
                owner_chat_id=int(owner_chat_id),
                session=session,
                bot=bot,
                record=record,
                topic_name=title,
            )
            self._cache_record(repaired_record)
            return repaired_record
        await bot.edit_forum_topic(
            chat_id=int(scope.chat_id),
            message_thread_id=int(scope.message_thread_id),
            name=title,
        )
        record = self._sync_mapping(owner_chat_id=int(owner_chat_id), session=session, topic_name=title)
        self._cache_record(record)
        return record

    async def reconcile(self) -> int:
        self._uid_by_topic.clear()
        if not self.is_enabled():
            return 0
        restored = 0
        for record in self._repository.list_mappings():
            session = self._session_manager.get(int(record.owner_chat_id), record.session_id)
            if session is None:
                self._prune_orphan_mapping(record, source="reconcile")
                logger.warning(
                    "session thread reconcile pruned orphan mapping owner_chat_id=%s session_id=%s session_uid=%s",
                    record.owner_chat_id,
                    record.session_id,
                    record.session_uid,
                )
                continue
            self._apply_scope(
                session,
                topics_chat_id=int(record.topics_chat_id),
                message_thread_id=int(record.message_thread_id),
            )
            self._session_manager.persist_session(int(record.owner_chat_id), session.id)
            self._cache_record(record)
            restored += 1

        for owner_chat_id, sessions in self._session_manager.sessions_by_chat.items():
            for session in sessions.values():
                scope = getattr(session, "conversation_scope", None)
                if not isinstance(scope, ConversationScope) or scope.message_thread_id is None:
                    continue
                record = self._repository.get_by_session(owner_chat_id=int(owner_chat_id), session_id=session.id)
                if record is None:
                    record = self._sync_mapping(owner_chat_id=int(owner_chat_id), session=session)
                self._cache_record(record)
        return restored

    async def backfill_session_topics(self, *, bot) -> int:
        if not self.is_enabled():
            return 0
        if bot is None:
            raise SessionThreadManagerError("telegram bot is required for thread-mode topic backfill")

        bound = 0
        for owner_chat_id, sessions in self._session_manager.sessions_by_chat.items():
            for session in sessions.values():
                scope = getattr(session, "conversation_scope", None)
                before_thread_id = scope.message_thread_id if isinstance(scope, ConversationScope) else None
                await self.ensure_topic_for_session(
                    owner_chat_id=int(owner_chat_id),
                    session=session,
                    bot=bot,
                )
                scope = getattr(session, "conversation_scope", None)
                after_thread_id = scope.message_thread_id if isinstance(scope, ConversationScope) else None
                if before_thread_id is None and after_thread_id is not None:
                    bound += 1
        return bound

    async def start_repair_job(self, *, bot, interval_sec: float | None = None) -> None:
        if not self.is_enabled():
            return
        if bot is None:
            raise SessionThreadManagerError("telegram bot is required for thread-mode repair job")
        if self._repair_task is not None and not self._repair_task.done():
            return
        interval = float(interval_sec or self._DEFAULT_REPAIR_INTERVAL_SEC)
        if interval <= 0:
            raise SessionThreadManagerError("thread-mode repair interval must be > 0")
        self._repair_task = asyncio.create_task(
            self._run_repair_loop(bot=bot, interval_sec=interval),
            name="session-thread-repair",
        )

    async def stop_repair_job(self) -> None:
        task = self._repair_task
        self._repair_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def repair_reconcile(self, *, bot) -> int:
        if not self.is_enabled():
            return 0
        if bot is None:
            raise SessionThreadManagerError("telegram bot is required for thread-mode repair reconcile")

        repaired = 0
        self._uid_by_topic.clear()
        pause_before_request = False
        for record in self._repository.list_mappings():
            session = self._session_manager.get(int(record.owner_chat_id), record.session_id)
            if session is None:
                self._prune_orphan_mapping(record, source="repair")
                logger.warning(
                    "session thread repair pruned orphan mapping owner_chat_id=%s session_id=%s session_uid=%s",
                    record.owner_chat_id,
                    record.session_id,
                    record.session_uid,
                )
                continue

            self._apply_scope(
                session,
                topics_chat_id=int(record.topics_chat_id),
                message_thread_id=int(record.message_thread_id),
            )
            expected_title = self.build_topic_title(session)

            if pause_before_request:
                await asyncio.sleep(self._REPAIR_RECORD_DELAY_SEC)
                pause_before_request = False

            try:
                repaired_record = await self._ensure_topic_present(
                    owner_chat_id=int(record.owner_chat_id),
                    session=session,
                    bot=bot,
                    record=record,
                    topic_name=expected_title,
                )
            except Exception:
                logger.exception(
                    "session thread repair failed: owner_chat_id=%s session_id=%s session_uid=%s",
                    record.owner_chat_id,
                    record.session_id,
                    record.session_uid,
                )
                pause_before_request = True
                continue

            if (
                int(repaired_record.message_thread_id) != int(record.message_thread_id)
                or str(repaired_record.session_uid) != str(record.session_uid)
            ):
                repaired += 1
            self._cache_record(repaired_record)
            pause_before_request = True

        return repaired

    def build_topic_title(self, session) -> str:
        return format_session_title(
            session,
            topic_title_prefix=str(getattr(self._config, "topic_title_prefix", "") or ""),
            max_length=self._TOPIC_TITLE_LIMIT,
        )

    def _sync_mapping(
        self,
        *,
        owner_chat_id: int,
        session,
        topic_name: Optional[str] = None,
    ) -> SessionThreadMappingRecord:
        scope = getattr(session, "conversation_scope", None)
        if not isinstance(scope, ConversationScope) or scope.message_thread_id is None:
            raise SessionThreadManagerError("session has no thread-bound conversation scope")
        return self._repository.upsert_mapping(
            owner_chat_id=int(owner_chat_id),
            session_id=str(session.id),
            session_uid=str(scope.session_uid),
            topics_chat_id=int(scope.chat_id),
            message_thread_id=int(scope.message_thread_id),
            topic_name=str(topic_name if topic_name is not None else self.build_topic_title(session)),
        )

    def _cache_record(self, record: SessionThreadMappingRecord) -> None:
        self._uid_by_topic[(int(record.topics_chat_id), int(record.message_thread_id))] = str(record.session_uid)

    def _drop_cached_topic(self, *, topics_chat_id: int, message_thread_id: int) -> None:
        self._uid_by_topic.pop((int(topics_chat_id), int(message_thread_id)), None)

    def _prune_orphan_mapping(self, record: SessionThreadMappingRecord, *, source: str) -> None:
        self._drop_cached_topic(
            topics_chat_id=int(record.topics_chat_id),
            message_thread_id=int(record.message_thread_id),
        )
        try:
            self._repository.delete_by_session(
                owner_chat_id=int(record.owner_chat_id),
                session_id=str(record.session_id),
            )
        except Exception:
            logger.exception(
                "session thread %s orphan cleanup failed owner_chat_id=%s session_id=%s session_uid=%s",
                source,
                record.owner_chat_id,
                record.session_id,
                record.session_uid,
            )

    async def cleanup_closed_session(
        self,
        *,
        owner_chat_id: int,
        session_id: str,
        bot=None,
        scope: Optional[ConversationScope] = None,
    ) -> None:
        owner = int(owner_chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return

        record = self._repository.get_by_session(owner_chat_id=owner, session_id=sid)
        target_chat_id: int | None = None
        target_thread_id: int | None = None

        if record is not None:
            target_chat_id = int(record.topics_chat_id)
            target_thread_id = int(record.message_thread_id)
            self._drop_cached_topic(
                topics_chat_id=int(record.topics_chat_id),
                message_thread_id=int(record.message_thread_id),
            )

        if target_thread_id is None and isinstance(scope, ConversationScope) and scope.message_thread_id is not None:
            target_chat_id = int(scope.chat_id)
            target_thread_id = int(scope.message_thread_id)
            self._drop_cached_topic(
                topics_chat_id=int(scope.chat_id),
                message_thread_id=int(scope.message_thread_id),
            )

        try:
            self._repository.delete_by_session(owner_chat_id=owner, session_id=sid)
        except Exception:
            logger.exception(
                "session thread cleanup failed while deleting mapping owner_chat_id=%s session_id=%s",
                owner,
                sid,
            )

        if bot is None or target_chat_id is None or target_thread_id is None:
            return

        try:
            await bot.delete_forum_topic(
                chat_id=int(target_chat_id),
                message_thread_id=int(target_thread_id),
            )
        except BadRequest as exc:
            if self._is_missing_topic_error(exc):
                return
            logger.exception(
                "session thread cleanup failed while deleting topic owner_chat_id=%s session_id=%s chat_id=%s thread_id=%s",
                owner,
                sid,
                target_chat_id,
                target_thread_id,
            )
        except Exception:
            logger.exception(
                "session thread cleanup failed while deleting topic owner_chat_id=%s session_id=%s chat_id=%s thread_id=%s",
                owner,
                sid,
                target_chat_id,
                target_thread_id,
            )

    @staticmethod
    def _apply_scope(session, *, topics_chat_id: int, message_thread_id: int) -> None:
        session.conversation_scope = ConversationScope.from_parts(int(topics_chat_id), int(message_thread_id))

    def _thread_mode(self) -> str:
        return str(getattr(self._config, "mode", "") or "").strip()

    def _target_chat_id(self, *, owner_chat_id: int) -> int:
        mode = self._thread_mode()
        if mode == "private":
            return int(owner_chat_id)
        if mode == "group":
            topics_chat_id = getattr(self._config, "topics_chat_id", None)
            if topics_chat_id is None:
                raise SessionThreadManagerError("thread_mode.topics_chat_id is required for group mode")
            return int(topics_chat_id)
        raise SessionThreadManagerError(f"unsupported thread_mode.mode: {mode or '<empty>'}")

    async def _run_repair_loop(self, *, bot, interval_sec: float) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                await self.repair_reconcile(bot=bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session thread repair loop iteration failed")

    async def _ensure_topic_present(
        self,
        *,
        owner_chat_id: int,
        session,
        bot,
        record: SessionThreadMappingRecord,
        topic_name: str,
    ) -> SessionThreadMappingRecord:
        normalized_topic_name = str(topic_name)
        if str(record.topic_name or "") == normalized_topic_name:
            if await self._topic_exists(bot=bot, record=record):
                return self._sync_mapping(
                    owner_chat_id=int(owner_chat_id),
                    session=session,
                    topic_name=normalized_topic_name,
                )
            return await self._recreate_topic(
                owner_chat_id=int(owner_chat_id),
                session=session,
                bot=bot,
                topic_name=normalized_topic_name,
            )
        try:
            await bot.edit_forum_topic(
                chat_id=int(record.topics_chat_id),
                message_thread_id=int(record.message_thread_id),
                name=normalized_topic_name,
            )
        except BadRequest as exc:
            if self._is_topic_not_modified_error(exc):
                return self._sync_mapping(
                    owner_chat_id=int(owner_chat_id),
                    session=session,
                    topic_name=normalized_topic_name,
                )
            if self._is_missing_topic_error(exc):
                return await self._recreate_topic(
                    owner_chat_id=int(owner_chat_id),
                    session=session,
                    bot=bot,
                    topic_name=normalized_topic_name,
                )
            raise
        return self._sync_mapping(
            owner_chat_id=int(owner_chat_id),
            session=session,
            topic_name=normalized_topic_name,
        )

    async def _topic_exists(self, *, bot, record: SessionThreadMappingRecord) -> bool:
        try:
            await bot.send_chat_action(
                chat_id=int(record.topics_chat_id),
                message_thread_id=int(record.message_thread_id),
                action="typing",
            )
        except BadRequest as exc:
            if self._is_missing_topic_error(exc):
                return False
            raise
        return True

    async def _recreate_topic(self, *, owner_chat_id: int, session, bot, topic_name: str) -> SessionThreadMappingRecord:
        topic_chat_id = self._target_chat_id(owner_chat_id=int(owner_chat_id))
        forum_topic = await bot.create_forum_topic(
            chat_id=topic_chat_id,
            name=str(topic_name),
        )
        thread_id = int(getattr(forum_topic, "message_thread_id", 0) or 0)
        if thread_id <= 0:
            raise SessionThreadManagerError("telegram create_forum_topic returned invalid message_thread_id during repair")
        self._apply_scope(session, topics_chat_id=topic_chat_id, message_thread_id=thread_id)
        self._session_manager.persist_session(int(owner_chat_id), session.id)
        return self._sync_mapping(owner_chat_id=int(owner_chat_id), session=session, topic_name=topic_name)

    @staticmethod
    def _is_missing_topic_error(exc: Exception) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            marker in message
            for marker in (
                "message thread not found",
                "thread not found",
                "topic was deleted",
                "topic deleted",
                "topic not found",
            )
        )

    @staticmethod
    def _is_topic_not_modified_error(exc: Exception) -> bool:
        message = str(exc or "").strip().lower()
        return "not modified" in message or "topic_not_modified" in message
