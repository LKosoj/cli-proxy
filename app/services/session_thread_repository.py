from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.services.path_normalization import normalize_state_path
from app.services.state_repository import get_state_repository


logger = logging.getLogger(__name__)


class SessionThreadRepositoryError(RuntimeError):
    """Base error for thread mapping persistence."""


class SessionThreadMappingConflictError(SessionThreadRepositoryError):
    """Raised when a thread mapping conflicts with an existing session mapping."""


@dataclass(frozen=True)
class SessionThreadMappingRecord:
    owner_chat_id: int
    session_id: str
    session_uid: str
    topics_chat_id: int
    message_thread_id: int
    topic_name: str
    created_at: float
    updated_at: float


class SessionThreadRepository:
    TABLE_NAME = "session_thread_mappings"

    def __init__(self, state_path: str) -> None:
        self.db_path = str(get_state_repository(normalize_state_path(state_path)).db_path)
        self._lock = threading.RLock()
        self.ensure_schema()

    def _connect(self):
        from app.services.sqlite_connection import sqlite_session
        return sqlite_session(self.db_path)

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        owner_chat_id INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        session_uid TEXT NOT NULL,
                        topics_chat_id INTEGER NOT NULL,
                        message_thread_id INTEGER NOT NULL,
                        topic_name TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY(owner_chat_id, session_id),
                        UNIQUE(topics_chat_id, message_thread_id),
                        UNIQUE(session_uid)
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_topic
                    ON {self.TABLE_NAME}(topics_chat_id, message_thread_id)
                    """
                )

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> Optional[SessionThreadMappingRecord]:
        if row is None:
            return None
        return SessionThreadMappingRecord(
            owner_chat_id=int(row["owner_chat_id"]),
            session_id=str(row["session_id"] or ""),
            session_uid=str(row["session_uid"] or ""),
            topics_chat_id=int(row["topics_chat_id"]),
            message_thread_id=int(row["message_thread_id"]),
            topic_name=str(row["topic_name"] or ""),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )

    def upsert_mapping(
        self,
        *,
        owner_chat_id: int,
        session_id: str,
        session_uid: str,
        topics_chat_id: int,
        message_thread_id: int,
        topic_name: str,
    ) -> SessionThreadMappingRecord:
        owner = int(owner_chat_id)
        sid = str(session_id or "").strip()
        uid = str(session_uid or "").strip()
        topic_chat = int(topics_chat_id)
        thread_id = int(message_thread_id)
        title = str(topic_name or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        if not uid:
            raise ValueError("session_uid is required")
        if thread_id <= 0:
            raise ValueError("message_thread_id must be > 0")
        now = float(time.time())
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    f"""
                    SELECT owner_chat_id, session_id
                    FROM {self.TABLE_NAME}
                    WHERE topics_chat_id = ? AND message_thread_id = ?
                    """,
                    (topic_chat, thread_id),
                ).fetchone()
                if existing is not None and (
                    int(existing["owner_chat_id"]) != owner or str(existing["session_id"] or "") != sid
                ):
                    raise SessionThreadMappingConflictError(
                        f"thread mapping is already owned by another session: {topic_chat}:{thread_id}"
                    )

                created = conn.execute(
                    f"""
                    SELECT created_at
                    FROM {self.TABLE_NAME}
                    WHERE owner_chat_id = ? AND session_id = ?
                    """,
                    (owner, sid),
                ).fetchone()
                created_at = float(created["created_at"] or now) if created is not None else now
                try:
                    conn.execute(
                        f"""
                        INSERT INTO {self.TABLE_NAME}(
                            owner_chat_id,
                            session_id,
                            session_uid,
                            topics_chat_id,
                            message_thread_id,
                            topic_name,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(owner_chat_id, session_id)
                        DO UPDATE SET
                            session_uid=excluded.session_uid,
                            topics_chat_id=excluded.topics_chat_id,
                            message_thread_id=excluded.message_thread_id,
                            topic_name=excluded.topic_name,
                            updated_at=excluded.updated_at
                        """,
                        (owner, sid, uid, topic_chat, thread_id, title, created_at, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise SessionThreadMappingConflictError(
                        f"thread mapping conflict for session {owner}:{sid}"
                    ) from exc
                row = conn.execute(
                    f"""
                    SELECT owner_chat_id, session_id, session_uid, topics_chat_id, message_thread_id, topic_name, created_at, updated_at
                    FROM {self.TABLE_NAME}
                    WHERE owner_chat_id = ? AND session_id = ?
                    """,
                    (owner, sid),
                ).fetchone()
                assert row is not None
                return self._row_to_record(row)  # type: ignore[return-value]

    def get_by_session(self, *, owner_chat_id: int, session_id: str) -> Optional[SessionThreadMappingRecord]:
        owner = int(owner_chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return None
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT owner_chat_id, session_id, session_uid, topics_chat_id, message_thread_id, topic_name, created_at, updated_at
                    FROM {self.TABLE_NAME}
                    WHERE owner_chat_id = ? AND session_id = ?
                    """,
                    (owner, sid),
                ).fetchone()
        return self._row_to_record(row)

    def get_by_topic(self, *, topics_chat_id: int, message_thread_id: int) -> Optional[SessionThreadMappingRecord]:
        topic_chat = int(topics_chat_id)
        thread_id = int(message_thread_id)
        if thread_id <= 0:
            return None
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT owner_chat_id, session_id, session_uid, topics_chat_id, message_thread_id, topic_name, created_at, updated_at
                    FROM {self.TABLE_NAME}
                    WHERE topics_chat_id = ? AND message_thread_id = ?
                    """,
                    (topic_chat, thread_id),
                ).fetchone()
        return self._row_to_record(row)

    def list_mappings(self) -> list[SessionThreadMappingRecord]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT owner_chat_id, session_id, session_uid, topics_chat_id, message_thread_id, topic_name, created_at, updated_at
                    FROM {self.TABLE_NAME}
                    ORDER BY owner_chat_id, session_id
                    """
                ).fetchall()
        return [record for record in (self._row_to_record(row) for row in rows) if record is not None]

    def delete_by_session(self, *, owner_chat_id: int, session_id: str) -> None:
        owner = int(owner_chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"DELETE FROM {self.TABLE_NAME} WHERE owner_chat_id = ? AND session_id = ?",
                    (owner, sid),
                )
