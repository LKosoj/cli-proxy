from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.services.path_normalization import normalize_optional_state_path
from app.services.state_repository import get_state_repository
from modes.sdk.runtime.json_normalizer import loads_safe


class WebhookDeliveryRepositoryError(RuntimeError):
    """Raised when webhook delivery persistence cannot be initialized."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookDeliveryRecord:
    source: str
    delivery_id: str
    received_at: float
    payload: dict[str, Any]


class WebhookDeliveryRepository:
    TABLE_NAME = "webhook_delivery_dedup"

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
            raise WebhookDeliveryRepositoryError("webhook delivery storage path is invalid") from exc
        if not normalized_state_path:
            raise WebhookDeliveryRepositoryError("webhook delivery storage path is not configured")
        return str(get_state_repository(normalized_state_path).db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _dumps(value: dict[str, Any] | None) -> str:
        return json.dumps(dict(value or {}), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads_dict(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = loads_safe(raw, strict_first=True)
        except Exception:
            logger.exception("webhook delivery repository failed to parse stored json")
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                        source TEXT NOT NULL,
                        delivery_id TEXT NOT NULL,
                        received_at REAL NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{{}}',
                        PRIMARY KEY(source, delivery_id)
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_received
                    ON {self.TABLE_NAME}(received_at DESC)
                    """
                )

    def claim_delivery(
        self,
        *,
        source: str,
        delivery_id: str,
        payload: dict[str, Any] | None = None,
        received_at: float | None = None,
    ) -> bool:
        token_source = str(source or "").strip()
        token_delivery = str(delivery_id or "").strip()
        if not token_source:
            raise ValueError("source is required")
        if not token_delivery:
            raise ValueError("delivery_id is required")
        stamp = float(received_at or time.time())
        with self._lock:
            with self._connect() as conn:
                try:
                    conn.execute(
                        f"""
                        INSERT INTO {self.TABLE_NAME}(source, delivery_id, received_at, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (token_source, token_delivery, stamp, self._dumps(payload)),
                    )
                    return True
                except sqlite3.IntegrityError:
                    return False

    def get_delivery(self, *, source: str, delivery_id: str) -> Optional[WebhookDeliveryRecord]:
        token_source = str(source or "").strip()
        token_delivery = str(delivery_id or "").strip()
        if not token_source or not token_delivery:
            return None
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT source, delivery_id, received_at, payload_json
                    FROM {self.TABLE_NAME}
                    WHERE source = ? AND delivery_id = ?
                    """,
                    (token_source, token_delivery),
                ).fetchone()
        if row is None:
            return None
        return WebhookDeliveryRecord(
            source=str(row["source"] or ""),
            delivery_id=str(row["delivery_id"] or ""),
            received_at=float(row["received_at"] or 0.0),
            payload=self._loads_dict(row["payload_json"]),
        )
