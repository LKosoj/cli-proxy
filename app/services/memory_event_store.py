from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.services.redaction import redact_text, redact_value
from app.services.path_normalization import normalize_optional_state_path
from app.services.state_repository import get_state_repository
from modes.sdk.runtime.json_normalizer import loads_safe


logger = logging.getLogger(__name__)


class MemoryEventStoreError(RuntimeError):
    """Raised when memory event persistence cannot be initialized."""


@dataclass(frozen=True)
class MemoryEventRecord:
    event_id: str
    event_type: str
    source: str
    session_uid: str
    run_id: str
    mode_id: str
    phase: str
    unit_id: str
    created_at: float
    prompt_hash: str
    payload: dict[str, Any]
    payload_truncated: bool
    redacted: bool


class MemoryEventStore:
    TABLE_NAME = "memory_events"
    STATE_VERSION = 1

    def __init__(
        self,
        state_path: str | None = None,
        *,
        sqlite_path: str | None = None,
        max_payload_chars: int = 6000,
        redaction_enabled: bool = True,
    ) -> None:
        self.db_path = self._resolve_db_path(state_path=state_path, sqlite_path=sqlite_path)
        self.max_payload_chars = max(256, int(max_payload_chars or 6000))
        self.redaction_enabled = bool(redaction_enabled)
        self._lock = threading.RLock()
        self.ensure_schema()

    @classmethod
    def from_config(cls, config: Any) -> "MemoryEventStore":
        defaults = getattr(config, "defaults", None)
        return cls(
            getattr(defaults, "state_path", None),
            max_payload_chars=int(getattr(defaults, "memory_events_max_payload_chars", 6000) or 6000),
            redaction_enabled=bool(getattr(defaults, "memory_events_redaction_enabled", True)),
        )

    @staticmethod
    def _resolve_db_path(*, state_path: str | None, sqlite_path: str | None) -> str:
        explicit_path = str(sqlite_path or "").strip()
        if explicit_path:
            normalized = os.path.abspath(explicit_path)
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            return normalized
        try:
            normalized_state_path = normalize_optional_state_path(state_path)
        except TypeError as exc:
            raise MemoryEventStoreError("memory event storage path is invalid") from exc
        if not normalized_state_path:
            raise MemoryEventStoreError("memory event storage path is not configured")
        return str(get_state_repository(normalized_state_path).db_path)

    def _connect(self):
        from app.services.sqlite_connection import sqlite_session
        return sqlite_session(self.db_path)

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        event_id TEXT PRIMARY KEY,
                        version INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        source TEXT NOT NULL,
                        session_uid TEXT NOT NULL DEFAULT '',
                        run_id TEXT NOT NULL DEFAULT '',
                        mode_id TEXT NOT NULL DEFAULT '',
                        phase TEXT NOT NULL DEFAULT '',
                        unit_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        prompt_hash TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{{}}',
                        payload_truncated INTEGER NOT NULL DEFAULT 0,
                        redacted INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_created
                    ON {self.TABLE_NAME}(created_at DESC)
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_run
                    ON {self.TABLE_NAME}(session_uid, run_id, created_at)
                    """
                )

    def record_event(
        self,
        *,
        event_type: str,
        source: str,
        session_uid: str = "",
        run_id: str = "",
        mode_id: str = "",
        phase: str = "",
        unit_id: str = "",
        prompt_hash: str = "",
        payload: dict[str, Any] | None = None,
        dedupe_key: str = "",
        created_at: float | None = None,
    ) -> tuple[MemoryEventRecord, bool]:
        clean_event_type = str(event_type or "").strip()
        clean_source = str(source or "").strip()
        if not clean_event_type:
            raise ValueError("event_type is required")
        if not clean_source:
            raise ValueError("source is required")
        stamp = float(created_at or time.time())
        clean_session_uid = str(session_uid or "").strip()
        clean_run_id = str(run_id or "").strip()
        clean_mode_id = str(mode_id or "").strip()
        clean_phase = str(phase or "").strip()
        clean_unit_id = str(unit_id or "").strip()
        clean_prompt_hash = str(prompt_hash or "").strip()
        clean_payload, payload_truncated, redacted = self._prepare_payload(payload)
        event_id = self._event_id(
            event_type=clean_event_type,
            source=clean_source,
            session_uid=clean_session_uid,
            run_id=clean_run_id,
            mode_id=clean_mode_id,
            phase=clean_phase,
            unit_id=clean_unit_id,
            dedupe_key=str(dedupe_key or ""),
            created_at=stamp,
            payload=clean_payload,
        )
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {self.TABLE_NAME}(
                        event_id, version, event_type, source, session_uid, run_id, mode_id, phase,
                        unit_id, created_at, prompt_hash, payload_json, payload_truncated, redacted
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        self.STATE_VERSION,
                        clean_event_type,
                        clean_source,
                        clean_session_uid,
                        clean_run_id,
                        clean_mode_id,
                        clean_phase,
                        clean_unit_id,
                        stamp,
                        clean_prompt_hash,
                        self._dumps(clean_payload),
                        int(payload_truncated),
                        int(redacted),
                    ),
                )
                inserted = int(cursor.rowcount or 0) > 0
                row = self._get_event_conn(conn, event_id)
        if row is None:
            raise MemoryEventStoreError("memory event insert failed")
        return self._row_to_record(row), bool(inserted)

    def get_event(self, event_id: str) -> Optional[MemoryEventRecord]:
        token = str(event_id or "").strip()
        if not token:
            return None
        with self._lock:
            with self._connect() as conn:
                row = self._get_event_conn(conn, token)
        return self._row_to_record(row) if row is not None else None

    def list_events(
        self,
        *,
        session_uid: str = "",
        run_id: str = "",
        limit: int = 50,
    ) -> list[MemoryEventRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        clean_session_uid = str(session_uid or "").strip()
        clean_run_id = str(run_id or "").strip()
        if clean_session_uid:
            conditions.append("session_uid = ?")
            params.append(clean_session_uid)
        if clean_run_id:
            conditions.append("run_id = ?")
            params.append(clean_run_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, int(limit or 50)))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM {self.TABLE_NAME}
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def prune_older_than(self, *, retention_days: int, now: float | None = None) -> int:
        cutoff = float(now or time.time()) - max(1, int(retention_days or 1)) * 86400
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"DELETE FROM {self.TABLE_NAME} WHERE created_at < ?",
                    (cutoff,),
                )
                return int(cursor.rowcount or 0)

    @staticmethod
    def _get_event_conn(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        return conn.execute(
            f"""
            SELECT *
            FROM {MemoryEventStore.TABLE_NAME}
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row) -> MemoryEventRecord:
        return MemoryEventRecord(
            event_id=str(row["event_id"] or ""),
            event_type=str(row["event_type"] or ""),
            source=str(row["source"] or ""),
            session_uid=str(row["session_uid"] or ""),
            run_id=str(row["run_id"] or ""),
            mode_id=str(row["mode_id"] or ""),
            phase=str(row["phase"] or ""),
            unit_id=str(row["unit_id"] or ""),
            created_at=float(row["created_at"] or 0.0),
            prompt_hash=str(row["prompt_hash"] or ""),
            payload=cls._loads_dict(row["payload_json"]),
            payload_truncated=bool(int(row["payload_truncated"] or 0)),
            redacted=bool(int(row["redacted"] or 0)),
        )

    def _prepare_payload(self, payload: dict[str, Any] | None) -> tuple[dict[str, Any], bool, bool]:
        original = dict(payload or {})
        redacted_payload = self._redact_value(original) if self.redaction_enabled else original
        redacted = redacted_payload != original
        serialized = self._dumps(redacted_payload)
        if len(serialized) <= self.max_payload_chars:
            return redacted_payload, False, redacted
        preview = serialized[: max(0, self.max_payload_chars)]
        truncated = {"truncated": True, "preview": preview}
        while len(self._dumps(truncated)) > self.max_payload_chars and preview:
            preview = preview[:-1]
            truncated["preview"] = preview
        return truncated, True, redacted

    @classmethod
    def _redact_value(cls, value: Any) -> Any:
        return redact_value(value)

    @staticmethod
    def _redact_text(value: str) -> str:
        return redact_text(value)

    @staticmethod
    def _event_id(
        *,
        event_type: str,
        source: str,
        session_uid: str,
        run_id: str,
        mode_id: str,
        phase: str,
        unit_id: str,
        dedupe_key: str,
        created_at: float,
        payload: dict[str, Any],
    ) -> str:
        if dedupe_key:
            payload_key = str(dedupe_key)
        else:
            payload_key = f"{created_at:.6f}|{MemoryEventStore._dumps(payload)}"
        digest = hashlib.sha256(
            "|".join(
                [
                    source,
                    event_type,
                    session_uid,
                    run_id,
                    mode_id,
                    phase,
                    unit_id,
                    payload_key,
                ]
            ).encode("utf-8", errors="ignore")
        ).hexdigest()
        return f"memevt:{digest}"

    @staticmethod
    def _dumps(value: dict[str, Any] | None) -> str:
        return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _loads_dict(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = loads_safe(raw, strict_first=True)
        except Exception:
            logger.exception("memory event store failed to parse stored json")
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
