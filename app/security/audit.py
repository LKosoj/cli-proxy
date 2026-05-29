from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Any, Mapping, Optional

from app.events.bus import SystemEventBus
from app.services.path_normalization import normalize_optional_path, normalize_optional_state_path
from app.services.state_repository import get_state_repository
from modes.sdk.runtime.json_normalizer import loads_safe

from .interfaces import AuditRecord


class AuditStoreError(RuntimeError):
    """Raised when persistent audit storage cannot be initialized."""


class EventBusAuditService:
    EVENT_NAME = "security.audit"

    def __init__(
        self,
        *,
        system_event_bus: Optional[SystemEventBus] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._system_event_bus = system_event_bus
        self._logger = logger or logging.getLogger(__name__)

    async def emit(self, record: AuditRecord) -> None:
        payload = record.to_payload()
        context = dict(payload.get("context") or {})
        self._logger.info(
            "security audit emitted",
            extra={
                "chat_id": self._safe_int(context.get("chat_id")),
                "user_id": str(payload.get("user_id", "") or ""),
                "action": f"security.{payload.get('action', '')}",
                "path": str(payload.get("scope", "") or ""),
                "status": str(payload.get("status", "") or ""),
                "error": str(payload.get("reason", "") or ""),
            },
        )
        if self._system_event_bus is None:
            return
        try:
            await self._system_event_bus.publish(self.EVENT_NAME, payload)
        except Exception:
            self._logger.exception("security audit publish failed event=%s", self.EVENT_NAME)

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    def list_records(
        self,
        *,
        limit: int = 100,
        action: str = "",
        user_id: str | int | None = None,
        status: str = "",
        subject: str = "",
    ) -> list[AuditRecord]:
        del limit, action, user_id, status, subject
        return []


class SqliteAuditLogStore:
    TABLE_NAME = "security_audit_logs"

    def __init__(
        self,
        *,
        state_path: str | None = None,
        sqlite_path: str | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self.db_path = self._resolve_db_path(state_path=state_path, sqlite_path=sqlite_path)
        self._lock = threading.RLock()
        self.ensure_schema()

    @staticmethod
    def _resolve_db_path(*, state_path: str | None, sqlite_path: str | None) -> str:
        try:
            explicit_path = normalize_optional_path(sqlite_path, field_name="sqlite_path")
        except TypeError as exc:
            raise AuditStoreError("audit sqlite_path is invalid") from exc
        if explicit_path:
            normalized = os.path.abspath(explicit_path)
            parent = os.path.dirname(normalized)
            if parent:
                os.makedirs(parent, exist_ok=True)
            return normalized

        try:
            state_token = normalize_optional_state_path(state_path)
        except TypeError as exc:
            raise AuditStoreError("audit storage path is invalid") from exc
        if not state_token:
            raise AuditStoreError("audit storage path is not configured")
        return str(get_state_repository(state_token).db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _dumps(value: Mapping[str, Any] | None) -> str:
        return json.dumps(dict(value or {}), ensure_ascii=False, separators=(",", ":"))

    def _loads_dict(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = loads_safe(raw, strict_first=True)
        except Exception:
            self._logger.exception("audit log store failed to parse stored json")
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        category TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        user_id TEXT NOT NULL DEFAULT '',
                        subject TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        timestamp REAL NOT NULL,
                        context_json TEXT NOT NULL DEFAULT '{{}}',
                        details_json TEXT NOT NULL DEFAULT '{{}}'
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_timestamp
                    ON {self.TABLE_NAME}(timestamp DESC, id DESC)
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_user_action
                    ON {self.TABLE_NAME}(user_id, action)
                    """
                )

    def append(self, record: AuditRecord) -> int:
        payload = record.to_payload()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    INSERT INTO {self.TABLE_NAME}(
                        category, action, status, user_id, subject, scope, reason,
                        timestamp, context_json, details_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("category", "") or ""),
                        str(payload.get("action", "") or ""),
                        str(payload.get("status", "") or ""),
                        str(payload.get("user_id", "") or ""),
                        str(payload.get("subject", "") or ""),
                        str(payload.get("scope", "") or ""),
                        str(payload.get("reason", "") or ""),
                        float(payload.get("timestamp") or 0.0),
                        self._dumps(payload.get("context")),
                        self._dumps(payload.get("details")),
                    ),
                )
                return int(cursor.lastrowid or 0)

    def list_records(
        self,
        *,
        limit: int = 100,
        action: str = "",
        user_id: str | int | None = None,
        status: str = "",
        subject: str = "",
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[Any] = []

        action_token = str(action or "").strip()
        if action_token:
            clauses.append("action = ?")
            params.append(action_token)

        user_token = str(user_id or "").strip()
        if user_token:
            clauses.append("user_id = ?")
            params.append(user_token)

        status_token = str(status or "").strip()
        if status_token:
            clauses.append("status = ?")
            params.append(status_token)

        subject_token = str(subject or "").strip()
        if subject_token:
            clauses.append("subject = ?")
            params.append(subject_token)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(int(limit or 0), 1))

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT category, action, status, user_id, subject, scope, reason,
                       timestamp, context_json, details_json
                FROM {self.TABLE_NAME}
                {where_sql}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [self._row_to_record(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> AuditRecord:
        return AuditRecord(
            category=str(row["category"] or ""),
            action=str(row["action"] or ""),
            status=str(row["status"] or ""),
            user_id=str(row["user_id"] or ""),
            subject=str(row["subject"] or ""),
            scope=str(row["scope"] or ""),
            reason=str(row["reason"] or ""),
            timestamp=float(row["timestamp"] or 0.0),
            context=self._loads_dict(row["context_json"]),
            details=self._loads_dict(row["details_json"]),
        )


class PersistentAuditService:
    def __init__(
        self,
        *,
        store: SqliteAuditLogStore,
        event_bus_service: EventBusAuditService | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._event_bus_service = event_bus_service or EventBusAuditService(logger=logger)
        self._logger = logger or logging.getLogger(__name__)

    async def emit(self, record: AuditRecord) -> None:
        try:
            self._store.append(record)
        except Exception:
            self._logger.exception("security audit persistence failed")
        await self._event_bus_service.emit(record)

    def list_records(
        self,
        *,
        limit: int = 100,
        action: str = "",
        user_id: str | int | None = None,
        status: str = "",
        subject: str = "",
    ) -> list[AuditRecord]:
        return self._store.list_records(
            limit=limit,
            action=action,
            user_id=user_id,
            status=status,
            subject=subject,
        )


def build_audit_service(
    audit_config: Mapping[str, Any] | None,
    *,
    system_event_bus: Optional[SystemEventBus] = None,
    logger: Optional[logging.Logger] = None,
    default_state_path: str | None = None,
) -> EventBusAuditService | PersistentAuditService:
    config = dict(audit_config or {})
    bus_service = EventBusAuditService(system_event_bus=system_event_bus, logger=logger)
    if config.get("persist") is False:
        return bus_service

    try:
        sqlite_path = normalize_optional_path(config.get("sqlite_path"), field_name="sqlite_path") or ""
        state_path = (
            normalize_optional_state_path(config.get("state_path"))
            or normalize_optional_state_path(default_state_path)
            or ""
        )
    except TypeError as exc:
        raise AuditStoreError("audit storage path is invalid") from exc
    if not sqlite_path and not state_path:
        return bus_service

    store = SqliteAuditLogStore(
        state_path=state_path or None,
        sqlite_path=sqlite_path or None,
        logger=logger,
    )
    return PersistentAuditService(
        store=store,
        event_bus_service=bus_service,
        logger=logger,
    )


__all__ = [
    "AuditStoreError",
    "EventBusAuditService",
    "PersistentAuditService",
    "SqliteAuditLogStore",
    "build_audit_service",
]
