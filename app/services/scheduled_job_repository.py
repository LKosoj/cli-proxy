from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.services.actor_identity import normalize_actor_id
from app.services.path_normalization import normalize_optional_state_path
from app.services.state_repository import get_state_repository
from modes.sdk.runtime.json_normalizer import loads_safe


class ScheduledJobRepositoryError(RuntimeError):
    """Raised when scheduled job persistence cannot be initialized."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledJobRecord:
    job_id: str
    job_name: str
    scheduled_for: float
    payload: dict[str, Any]
    enabled: bool
    cron: str = ""
    target_mode: str = ""
    owner_id: str = ""
    notification_target: dict[str, Any] = field(default_factory=dict)
    next_run_at: float = 0.0
    last_fired_at: float = 0.0
    last_status: str = ""
    last_error: str = ""
    run_count: int = 0


@dataclass(frozen=True)
class ScheduledJobAuditRecord:
    audit_id: int
    correlation_id: str
    action: str
    owner_id: str
    job_id: str
    job_name: str
    origin: str
    provider: str
    timestamp: float
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)


class ScheduledJobRepository:
    TABLE_NAME = "scheduled_jobs"
    AUDIT_TABLE_NAME = "scheduled_job_audit_trail"
    REQUIRED_JOB_COLUMNS = frozenset(
        {
            "job_id",
            "job_name",
            "scheduled_for",
            "payload_json",
            "enabled",
            "cron",
            "target_mode",
            "owner_id",
            "notification_target_json",
            "next_run_at",
            "last_fired_at",
            "last_status",
            "last_error",
            "run_count",
            "created_at",
            "updated_at",
        }
    )
    REQUIRED_AUDIT_COLUMNS = frozenset(
        {
            "id",
            "correlation_id",
            "action",
            "owner_id",
            "job_id",
            "job_name",
            "origin",
            "provider",
            "timestamp",
            "before_json",
            "after_json",
        }
    )

    def __init__(self, state_path: str | None = None, *, sqlite_path: str | None = None) -> None:
        self.db_path = self._resolve_db_path(state_path=state_path, sqlite_path=sqlite_path)
        self._lock = threading.RLock()
        self.ensure_schema()

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
            raise ScheduledJobRepositoryError("scheduled job storage path is invalid") from exc
        if not normalized_state_path:
            raise ScheduledJobRepositoryError("scheduled job storage path is not configured")
        return str(get_state_repository(normalized_state_path).db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _dumps(value: Mapping[str, Any] | None) -> str:
        return json.dumps(
            copy.deepcopy(dict(value or {})),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _loads_dict(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = loads_safe(raw, strict_first=True)
        except Exception:
            logger.exception("scheduled job repository failed to parse stored json")
            return {}
        return copy.deepcopy(dict(parsed)) if isinstance(parsed, dict) else {}

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        return {
            str(row["name"] or "")
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }

    @classmethod
    def _validate_required_columns(
        cls,
        *,
        columns: set[str],
        table_name: str,
        required: frozenset[str],
    ) -> None:
        missing_columns = sorted(required - columns)
        if missing_columns:
            raise ScheduledJobRepositoryError(
                f"scheduled job schema is outdated: {table_name} table is missing required columns "
                + ", ".join(missing_columns)
            )

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        job_id TEXT PRIMARY KEY,
                        job_name TEXT NOT NULL,
                        scheduled_for REAL NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{{}}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        cron TEXT NOT NULL DEFAULT '',
                        target_mode TEXT NOT NULL DEFAULT '',
                        owner_id TEXT NOT NULL DEFAULT '',
                        notification_target_json TEXT NOT NULL DEFAULT '{{}}',
                        next_run_at REAL NOT NULL DEFAULT 0,
                        last_fired_at REAL NOT NULL DEFAULT 0,
                        last_status TEXT NOT NULL DEFAULT '',
                        last_error TEXT NOT NULL DEFAULT '',
                        run_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL DEFAULT 0
                    )
                    """
                )
                job_columns = self._table_columns(conn, self.TABLE_NAME)
                self._validate_required_columns(
                    columns=job_columns,
                    table_name=self.TABLE_NAME,
                    required=self.REQUIRED_JOB_COLUMNS,
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_enabled_schedule
                    ON {self.TABLE_NAME}(enabled, scheduled_for, job_id)
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_owner_enabled
                    ON {self.TABLE_NAME}(owner_id, enabled, job_id)
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_enabled_next
                    ON {self.TABLE_NAME}(enabled, next_run_at, job_id)
                    """
                )
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.AUDIT_TABLE_NAME} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        correlation_id TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL,
                        owner_id TEXT NOT NULL DEFAULT '',
                        job_id TEXT NOT NULL,
                        job_name TEXT NOT NULL DEFAULT '',
                        origin TEXT NOT NULL DEFAULT '',
                        provider TEXT NOT NULL DEFAULT '',
                        timestamp REAL NOT NULL DEFAULT 0,
                        before_json TEXT NOT NULL DEFAULT '{{}}',
                        after_json TEXT NOT NULL DEFAULT '{{}}'
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.AUDIT_TABLE_NAME}_job_time
                    ON {self.AUDIT_TABLE_NAME}(job_id, timestamp DESC, id DESC)
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.AUDIT_TABLE_NAME}_owner_time
                    ON {self.AUDIT_TABLE_NAME}(owner_id, timestamp DESC, id DESC)
                    """
                )
                audit_columns = self._table_columns(conn, self.AUDIT_TABLE_NAME)
                self._validate_required_columns(
                    columns=audit_columns,
                    table_name=self.AUDIT_TABLE_NAME,
                    required=self.REQUIRED_AUDIT_COLUMNS,
                )

    def upsert_job(
        self,
        *,
        job_id: str,
        job_name: str,
        scheduled_for: float,
        payload: Mapping[str, Any] | None = None,
        enabled: bool = True,
        cron: str | None = None,
        target_mode: str | None = None,
        owner_id: str | int = "",
        notification_target: Mapping[str, Any] | None = None,
        next_run_at: float | None = None,
        last_fired_at: float | None = None,
        last_status: str | None = None,
        last_error: str | None = None,
        run_count: int | None = None,
    ) -> ScheduledJobRecord:
        token_job_id = str(job_id or "").strip()
        token_job_name = str(job_name or "").strip()
        if not token_job_id:
            raise ValueError("job_id is required")
        if not token_job_name:
            raise ValueError("job_name is required")
        run_at = float(scheduled_for)
        stamp = float(time.time())
        enabled_int = 1 if bool(enabled) else 0
        next_run_value = run_at if next_run_at is None else float(next_run_at)
        last_fired_value = 0.0 if last_fired_at is None else float(last_fired_at)
        normalized_owner_id = normalize_actor_id(owner_id, default_surface="telegram")
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {self.TABLE_NAME}(
                        job_id, job_name, scheduled_for, payload_json, enabled,
                        cron, target_mode, owner_id, notification_target_json, next_run_at, last_fired_at,
                        last_status, last_error, run_count,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        job_name=excluded.job_name,
                        scheduled_for=excluded.scheduled_for,
                        payload_json=excluded.payload_json,
                        enabled=excluded.enabled,
                        cron=excluded.cron,
                        target_mode=excluded.target_mode,
                        owner_id=excluded.owner_id,
                        notification_target_json=excluded.notification_target_json,
                        next_run_at=excluded.next_run_at,
                        last_fired_at=excluded.last_fired_at,
                        last_status=excluded.last_status,
                        last_error=excluded.last_error,
                        run_count=excluded.run_count,
                        updated_at=excluded.updated_at
                    """,
                    (
                        token_job_id,
                        token_job_name,
                        run_at,
                        self._dumps(payload),
                        enabled_int,
                        str(cron or "").strip(),
                        str(target_mode or "").strip(),
                        normalized_owner_id,
                        self._dumps(notification_target),
                        next_run_value,
                        last_fired_value,
                        str(last_status or "").strip(),
                        str(last_error or "").strip(),
                        max(int(run_count or 0), 0),
                        stamp,
                        stamp,
                    ),
                )
        record = self.get_job(token_job_id)
        assert record is not None
        return record

    def get_job(self, job_id: str) -> Optional[ScheduledJobRecord]:
        token_job_id = str(job_id or "").strip()
        if not token_job_id:
            return None
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT job_id, job_name, scheduled_for, payload_json, enabled,
                           cron, target_mode, owner_id, notification_target_json, next_run_at, last_fired_at,
                           last_status, last_error, run_count
                    FROM {self.TABLE_NAME}
                    WHERE job_id = ?
                    """,
                    (token_job_id,),
                ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_jobs(
        self,
        *,
        enabled_only: bool = False,
        owner_id: str | int | None = None,
    ) -> list[ScheduledJobRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(normalize_actor_id(owner_id, default_surface="telegram"))
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT job_id, job_name, scheduled_for, payload_json, enabled,
                           cron, target_mode, owner_id, notification_target_json, next_run_at, last_fired_at,
                           last_status, last_error, run_count
                    FROM {self.TABLE_NAME}
                    {where_sql}
                    ORDER BY scheduled_for ASC, job_id ASC
                    """,
                    tuple(params),
                ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_due_jobs(self, *, as_of: float, limit: int | None = None) -> list[ScheduledJobRecord]:
        sql = f"""
            SELECT job_id, job_name, scheduled_for, payload_json, enabled,
                   cron, target_mode, owner_id, notification_target_json, next_run_at, last_fired_at,
                   last_status, last_error, run_count
            FROM {self.TABLE_NAME}
            WHERE enabled = 1
              AND cron != ''
              AND next_run_at > 0
              AND next_run_at <= ?
            ORDER BY next_run_at ASC, job_id ASC
        """
        params: list[object] = [float(as_of)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_record(row) for row in rows]

    def update_schedule(
        self,
        *,
        job_id: str,
        expected_next_run_at: float,
        next_run_at: float,
        scheduled_for: float,
        fired_at: float | None = None,
        last_status: str | None = None,
        last_error: str | None = None,
        increment_run_count: bool = False,
    ) -> bool:
        token_job_id = str(job_id or "").strip()
        if not token_job_id:
            return False
        with self._lock:
            with self._connect() as conn:
                if fired_at is None:
                    cursor = conn.execute(
                        f"""
                        UPDATE {self.TABLE_NAME}
                        SET next_run_at = ?, scheduled_for = ?, updated_at = ?
                        WHERE job_id = ? AND enabled = 1 AND next_run_at = ?
                        """,
                        (
                            float(next_run_at),
                            float(scheduled_for),
                            float(time.time()),
                            token_job_id,
                            float(expected_next_run_at),
                        ),
                    )
                else:
                    cursor = conn.execute(
                        f"""
                        UPDATE {self.TABLE_NAME}
                        SET last_fired_at = ?, next_run_at = ?, scheduled_for = ?, updated_at = ?,
                            last_status = ?, last_error = ?, run_count = run_count + ?
                        WHERE job_id = ? AND enabled = 1 AND next_run_at = ?
                        """,
                        (
                            float(fired_at),
                            float(next_run_at),
                            float(scheduled_for),
                            float(time.time()),
                            str(last_status or "").strip(),
                            str(last_error or "").strip(),
                            1 if increment_run_count else 0,
                            token_job_id,
                            float(expected_next_run_at),
                        ),
                    )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def update_runtime_status(
        self,
        *,
        job_id: str,
        last_status: str,
        last_error: str = "",
        last_run_at: float | None = None,
        increment_run_count: bool = False,
    ) -> bool:
        token_job_id = str(job_id or "").strip()
        if not token_job_id:
            return False
        clauses = [
            "last_status = ?",
            "last_error = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [
            str(last_status or "").strip(),
            str(last_error or "").strip(),
            float(time.time()),
        ]
        if last_run_at is not None:
            clauses.append("last_fired_at = ?")
            params.append(float(last_run_at))
        if increment_run_count:
            clauses.append("run_count = run_count + 1")
        params.append(token_job_id)
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE {self.TABLE_NAME}
                    SET {", ".join(clauses)}
                    WHERE job_id = ?
                    """,
                    tuple(params),
                )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def delete_job(self, job_id: str) -> bool:
        token_job_id = str(job_id or "").strip()
        if not token_job_id:
            return False
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"DELETE FROM {self.TABLE_NAME} WHERE job_id = ?",
                    (token_job_id,),
                )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def append_audit_record(
        self,
        *,
        correlation_id: str,
        action: str,
        owner_id: str | int,
        job_id: str,
        job_name: str,
        origin: str = "scheduler",
        provider: str = "scheduler",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> ScheduledJobAuditRecord:
        audit_action = str(action or "").strip()
        token_job_id = str(job_id or "").strip()
        if not audit_action:
            raise ValueError("action is required")
        if not token_job_id:
            raise ValueError("job_id is required")
        normalized_owner_id = normalize_actor_id(owner_id, default_surface="telegram")
        stamp = float(time.time() if timestamp is None else timestamp)
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    INSERT INTO {self.AUDIT_TABLE_NAME}(
                        correlation_id, action, owner_id, job_id, job_name, origin, provider,
                        timestamp, before_json, after_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(correlation_id or "").strip(),
                        audit_action,
                        normalized_owner_id,
                        token_job_id,
                        str(job_name or "").strip(),
                        str(origin or "").strip(),
                        str(provider or "").strip(),
                        stamp,
                        self._dumps(before),
                        self._dumps(after),
                    ),
                )
        return ScheduledJobAuditRecord(
            audit_id=int(cursor.lastrowid or 0),
            correlation_id=str(correlation_id or "").strip(),
            action=audit_action,
            owner_id=normalized_owner_id,
            job_id=token_job_id,
            job_name=str(job_name or "").strip(),
            origin=str(origin or "").strip(),
            provider=str(provider or "").strip(),
            timestamp=stamp,
            before=dict(before or {}),
            after=dict(after or {}),
        )

    def list_audit_records(
        self,
        *,
        limit: int = 100,
        owner_id: str | int | None = None,
        job_id: str = "",
        action: str = "",
    ) -> list[ScheduledJobAuditRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(normalize_actor_id(owner_id, default_surface="telegram"))
        token_job_id = str(job_id or "").strip()
        if token_job_id:
            clauses.append("job_id = ?")
            params.append(token_job_id)
        token_action = str(action or "").strip()
        if token_action:
            clauses.append("action = ?")
            params.append(token_action)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(int(limit or 0), 1))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT id, correlation_id, action, owner_id, job_id, job_name, origin, provider,
                           timestamp, before_json, after_json
                    FROM {self.AUDIT_TABLE_NAME}
                    {where_sql}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
        return [self._row_to_audit_record(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> ScheduledJobRecord:
        return ScheduledJobRecord(
            job_id=str(row["job_id"] or ""),
            job_name=str(row["job_name"] or ""),
            scheduled_for=float(row["scheduled_for"] or 0.0),
            payload=self._loads_dict(row["payload_json"]),
            enabled=bool(int(row["enabled"] or 0)),
            cron=str(row["cron"] or ""),
            target_mode=str(row["target_mode"] or ""),
            owner_id=str(row["owner_id"] or ""),
            notification_target=self._loads_dict(row["notification_target_json"]),
            next_run_at=float(row["next_run_at"] or 0.0),
            last_fired_at=float(row["last_fired_at"] or 0.0),
            last_status=str(row["last_status"] or ""),
            last_error=str(row["last_error"] or ""),
            run_count=max(int(row["run_count"] or 0), 0),
        )

    def _row_to_audit_record(self, row: sqlite3.Row) -> ScheduledJobAuditRecord:
        return ScheduledJobAuditRecord(
            audit_id=int(row["id"] or 0),
            correlation_id=str(row["correlation_id"] or ""),
            action=str(row["action"] or ""),
            owner_id=str(row["owner_id"] or ""),
            job_id=str(row["job_id"] or ""),
            job_name=str(row["job_name"] or ""),
            origin=str(row["origin"] or ""),
            provider=str(row["provider"] or ""),
            timestamp=float(row["timestamp"] or 0.0),
            before=self._loads_dict(row["before_json"]),
            after=self._loads_dict(row["after_json"]),
        )
