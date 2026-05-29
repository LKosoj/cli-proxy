from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, Optional

from app.services.path_normalization import normalize_state_path
from app.services.state_repository import get_state_repository
from modes.sdk.runtime.json_normalizer import loads_safe

_log = logging.getLogger(__name__)
_UNSET = object()


class AdminStateStoreError(RuntimeError):
    """Raised when admin state store operation fails."""


class AdminStateStore:
    def __init__(self, state_path: str) -> None:
        self._repo = get_state_repository(normalize_state_path(state_path))
        self.db_path = str(self._repo.db_path)
        self._lock = threading.RLock()
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _now_ts() -> float:
        return float(time.time())

    @staticmethod
    def _normalize_id(value: str, *, field: str) -> str:
        out = str(value or "").strip()
        if not out:
            raise AdminStateStoreError(f"{field} is empty")
        return out

    @staticmethod
    def _normalize_chat_id(value: Any) -> int:
        try:
            return int(value)
        except Exception as exc:
            raise AdminStateStoreError("chat_id is invalid") from exc

    @staticmethod
    def _normalize_optional_chat_id(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _encode_payload(payload: Optional[Dict[str, Any]]) -> str:
        data = dict(payload or {})
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_payload(raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = loads_safe(raw, strict_first=True)
        except Exception:
            _log.exception("admin state store: invalid payload JSON")
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
        return {}

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"] or "") for row in rows}

    def ensure_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                self._ensure_admin_session_state_schema(conn)
                self._ensure_entity_table(conn, table="admin_incidents", id_col="incident_id")
                self._ensure_entity_table(conn, table="admin_actions", id_col="action_id")
                self._ensure_entity_table(conn, table="admin_alerts_state", id_col="alert_id")
                self._ensure_entity_table(conn, table="admin_acknowledgements", id_col="acknowledgement_id")
                self._ensure_entity_table(conn, table="admin_approved_overrides", id_col="override_id")
                self._ensure_entity_table(conn, table="admin_digests", id_col="digest_id")

    def _ensure_admin_session_state_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_session_state (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL,
              session_id TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 0,
              watch_enabled INTEGER NOT NULL DEFAULT 0,
              dry_run INTEGER NOT NULL DEFAULT 1,
              muted_until_ts REAL,
              updated_at REAL NOT NULL,
              updated_by INTEGER,
              last_error TEXT,
              UNIQUE(chat_id, session_id)
            )
            """
        )
        required_cols = {
            "id",
            "chat_id",
            "session_id",
            "enabled",
            "watch_enabled",
            "dry_run",
            "muted_until_ts",
            "updated_at",
            "updated_by",
            "last_error",
        }
        cols = self._table_columns(conn, "admin_session_state")
        if required_cols.issubset(cols):
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_admin_session_state_chat_session
                  ON admin_session_state(chat_id, session_id)
                """
            )
            return
        missing = sorted(required_cols - cols)
        raise AdminStateStoreError(
            "admin state schema is outdated: admin_session_state is missing required columns "
            + ", ".join(missing)
        )

    def _ensure_entity_table(self, conn: sqlite3.Connection, *, table: str, id_col: str) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                {id_col} TEXT NOT NULL PRIMARY KEY,
                chat_id INTEGER NOT NULL DEFAULT 0,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{{}}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        cols = self._table_columns(conn, table)
        if "chat_id" not in cols:
            raise AdminStateStoreError(
                f"admin state schema is outdated: {table} is missing required column chat_id"
            )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_chat_session ON {table}(chat_id, session_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_session ON {table}(session_id)")

    # admin_session_state
    def get_session_state(self, session_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        sid = self._normalize_id(session_id, field="session_id")
        resolved_chat_id = self._normalize_optional_chat_id(chat_id)
        with self._lock:
            with self._connect() as conn:
                if resolved_chat_id is None:
                    row = conn.execute(
                        """
                        SELECT id, chat_id, session_id, enabled, watch_enabled, dry_run,
                               muted_until_ts, updated_at, updated_by, last_error
                        FROM admin_session_state
                        WHERE session_id=?
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (sid,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT id, chat_id, session_id, enabled, watch_enabled, dry_run,
                               muted_until_ts, updated_at, updated_by, last_error
                        FROM admin_session_state
                        WHERE chat_id=? AND session_id=?
                        """,
                        (resolved_chat_id, sid),
                    ).fetchone()
        if row is None:
            return None
        enabled = bool(int(row["enabled"] or 0))
        return {
            "id": int(row["id"] or 0),
            "chat_id": int(row["chat_id"] or 0),
            "session_id": str(row["session_id"] or ""),
            "enabled": enabled,
            "watch_enabled": bool(int(row["watch_enabled"] or 0)),
            "dry_run": bool(int(row["dry_run"] or 0)),
            "muted_until_ts": row["muted_until_ts"],
            "updated_at": float(row["updated_at"] or 0.0),
            "updated_by": row["updated_by"],
            "last_error": row["last_error"],
            # Convenience projection for UI/status consumers.
            "status": "enabled" if enabled else "disabled",
            "payload": {
                "watch_enabled": bool(int(row["watch_enabled"] or 0)),
                "dry_run": bool(int(row["dry_run"] or 0)),
                "last_error": row["last_error"],
            },
        }

    def upsert_session_state(
        self,
        session_id: str,
        *,
        chat_id: Any = 0,
        enabled: Optional[bool] = None,
        watch_enabled: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        muted_until_ts: Any = _UNSET,
        updated_by: Any = _UNSET,
        last_error: Any = _UNSET,
        status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sid = self._normalize_id(session_id, field="session_id")
        cid = self._normalize_chat_id(chat_id)
        current = self.get_session_state(sid, chat_id=cid) or {}
        merged_payload = dict(current.get("payload") or {})
        merged_payload.update(dict(payload or {}))

        enabled_value = enabled
        if enabled_value is None and status is not None:
            enabled_value = str(status or "").strip().lower() in {"enabled", "active", "on", "true", "1"}
        if enabled_value is None and "enabled" in merged_payload:
            enabled_value = bool(merged_payload.get("enabled"))
        if enabled_value is None:
            enabled_value = bool(current.get("enabled", False))

        watch_value = watch_enabled
        if watch_value is None and "watch_enabled" in merged_payload:
            watch_value = bool(merged_payload.get("watch_enabled"))
        if watch_value is None:
            watch_value = bool(current.get("watch_enabled", False))

        dry_run_value = dry_run
        if dry_run_value is None and "dry_run" in merged_payload:
            dry_run_value = bool(merged_payload.get("dry_run"))
        if dry_run_value is None:
            dry_run_value = bool(current.get("dry_run", True))

        if muted_until_ts is _UNSET:
            muted_value = current.get("muted_until_ts")
        elif muted_until_ts is None:
            muted_value = None
        else:
            muted_value = float(muted_until_ts)

        if updated_by is _UNSET:
            updated_by_value = current.get("updated_by")
        elif updated_by is None:
            updated_by_value = None
        else:
            try:
                updated_by_value = int(updated_by)
            except Exception as exc:
                raise AdminStateStoreError("updated_by is invalid") from exc

        if last_error is _UNSET:
            last_error_value = current.get("last_error")
            if "last_error" in merged_payload:
                last_error_value = merged_payload.get("last_error")
        elif last_error is None:
            last_error_value = None
        else:
            last_error_value = str(last_error)

        now = self._now_ts()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO admin_session_state(
                        chat_id, session_id, enabled, watch_enabled, dry_run,
                        muted_until_ts, updated_at, updated_by, last_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, session_id) DO UPDATE SET
                        enabled=excluded.enabled,
                        watch_enabled=excluded.watch_enabled,
                        dry_run=excluded.dry_run,
                        muted_until_ts=excluded.muted_until_ts,
                        updated_at=excluded.updated_at,
                        updated_by=excluded.updated_by,
                        last_error=excluded.last_error
                    """,
                    (
                        cid,
                        sid,
                        1 if enabled_value else 0,
                        1 if watch_value else 0,
                        1 if dry_run_value else 0,
                        muted_value,
                        now,
                        updated_by_value,
                        last_error_value,
                    ),
                )
        return self.get_session_state(sid, chat_id=cid) or {}

    def delete_session_state(self, session_id: str, *, chat_id: Any = None) -> bool:
        sid = self._normalize_id(session_id, field="session_id")
        resolved_chat_id = self._normalize_optional_chat_id(chat_id)
        with self._lock:
            with self._connect() as conn:
                if resolved_chat_id is None:
                    cur = conn.execute("DELETE FROM admin_session_state WHERE session_id=?", (sid,))
                else:
                    cur = conn.execute(
                        "DELETE FROM admin_session_state WHERE chat_id=? AND session_id=?",
                        (resolved_chat_id, sid),
                    )
        return int(cur.rowcount or 0) > 0

    def mute_session(self, session_id: str, muted_until_ts: float, *, chat_id: Any = 0) -> Dict[str, Any]:
        return self.upsert_session_state(
            session_id,
            chat_id=chat_id,
            muted_until_ts=float(muted_until_ts),
        )

    def unmute_session(self, session_id: str, *, chat_id: Any = 0) -> Dict[str, Any]:
        return self.upsert_session_state(session_id, chat_id=chat_id, muted_until_ts=None)

    # generic CRUD for entity tables
    def _create_entity(
        self,
        *,
        table: str,
        id_col: str,
        entity_id: str,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        eid = self._normalize_id(entity_id, field=id_col)
        sid = self._normalize_id(session_id, field="session_id")
        cid = self._normalize_chat_id(chat_id)
        now = self._now_ts()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {table}({id_col}, chat_id, session_id, payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT({id_col}) DO UPDATE SET
                        chat_id=excluded.chat_id,
                        session_id=excluded.session_id,
                        payload=excluded.payload,
                        updated_at=excluded.updated_at
                    """,
                    (eid, cid, sid, self._encode_payload(payload), now, now),
                )
        row = self._get_entity(table=table, id_col=id_col, entity_id=eid)
        return row or {}

    def _get_entity(
        self,
        *,
        table: str,
        id_col: str,
        entity_id: str,
        chat_id: Any = None,
    ) -> Optional[Dict[str, Any]]:
        eid = self._normalize_id(entity_id, field=id_col)
        resolved_chat_id = self._normalize_optional_chat_id(chat_id)
        with self._lock:
            with self._connect() as conn:
                if resolved_chat_id is None:
                    row = conn.execute(
                        f"""
                        SELECT {id_col}, chat_id, session_id, payload, created_at, updated_at
                        FROM {table}
                        WHERE {id_col}=?
                        """,
                        (eid,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"""
                        SELECT {id_col}, chat_id, session_id, payload, created_at, updated_at
                        FROM {table}
                        WHERE {id_col}=? AND chat_id=?
                        """,
                        (eid, resolved_chat_id),
                    ).fetchone()
        if row is None:
            return None
        return {
            id_col: str(row[id_col] or ""),
            "chat_id": int(row["chat_id"] or 0),
            "session_id": str(row["session_id"] or ""),
            "payload": self._decode_payload(row["payload"]),
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def _update_entity(
        self,
        *,
        table: str,
        id_col: str,
        entity_id: str,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        current = self._get_entity(table=table, id_col=id_col, entity_id=entity_id)
        if current is None:
            return None
        sid = str(session_id if session_id is not None else current.get("session_id") or "").strip()
        if not sid:
            raise AdminStateStoreError("session_id is empty")
        if chat_id is _UNSET:
            cid = int(current.get("chat_id") or 0)
        else:
            cid = self._normalize_chat_id(chat_id)
        next_payload = dict(payload if payload is not None else current.get("payload") or {})
        now = self._now_ts()
        eid = self._normalize_id(entity_id, field=id_col)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE {table}
                    SET chat_id=?, session_id=?, payload=?, updated_at=?
                    WHERE {id_col}=?
                    """,
                    (cid, sid, self._encode_payload(next_payload), now, eid),
                )
        return self._get_entity(table=table, id_col=id_col, entity_id=eid)

    def _delete_entity(self, *, table: str, id_col: str, entity_id: str) -> bool:
        eid = self._normalize_id(entity_id, field=id_col)
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(f"DELETE FROM {table} WHERE {id_col}=?", (eid,))
        return int(cur.rowcount or 0) > 0

    def _list_entities(
        self,
        *,
        table: str,
        id_col: str,
        session_id: str,
        chat_id: Any = None,
        limit: int = 20,
    ) -> list[Dict[str, Any]]:
        sid = self._normalize_id(session_id, field="session_id")
        resolved_chat_id = self._normalize_optional_chat_id(chat_id)
        safe_limit = max(1, min(int(limit or 20), 200))
        with self._lock:
            with self._connect() as conn:
                if resolved_chat_id is None:
                    rows = conn.execute(
                        f"""
                        SELECT {id_col}, chat_id, session_id, payload, created_at, updated_at
                        FROM {table}
                        WHERE session_id=?
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (sid, safe_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT {id_col}, chat_id, session_id, payload, created_at, updated_at
                        FROM {table}
                        WHERE chat_id=? AND session_id=?
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (resolved_chat_id, sid, safe_limit),
                    ).fetchall()
        return [
            {
                id_col: str(row[id_col] or ""),
                "chat_id": int(row["chat_id"] or 0),
                "session_id": str(row["session_id"] or ""),
                "payload": self._decode_payload(row["payload"]),
                "created_at": float(row["created_at"] or 0.0),
                "updated_at": float(row["updated_at"] or 0.0),
            }
            for row in rows
        ]

    def _clear_entities_by_session(self, *, table: str, session_id: str, chat_id: Any = None) -> int:
        sid = self._normalize_id(session_id, field="session_id")
        resolved_chat_id = self._normalize_optional_chat_id(chat_id)
        with self._lock:
            with self._connect() as conn:
                if resolved_chat_id is None:
                    cur = conn.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE session_id=?
                        """,
                        (sid,),
                    )
                else:
                    cur = conn.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE chat_id=? AND session_id=?
                        """,
                        (resolved_chat_id, sid),
                    )
        return int(cur.rowcount or 0)

    # admin_incidents
    def create_incident(
        self,
        incident_id: str,
        *,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._create_entity(
            table="admin_incidents",
            id_col="incident_id",
            entity_id=incident_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def get_incident(self, incident_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        return self._get_entity(table="admin_incidents", id_col="incident_id", entity_id=incident_id, chat_id=chat_id)

    def update_incident(
        self,
        incident_id: str,
        *,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._update_entity(
            table="admin_incidents",
            id_col="incident_id",
            entity_id=incident_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def delete_incident(self, incident_id: str) -> bool:
        return self._delete_entity(table="admin_incidents", id_col="incident_id", entity_id=incident_id)

    def list_incidents(self, session_id: str, *, chat_id: Any = None, limit: int = 20) -> list[Dict[str, Any]]:
        return self._list_entities(
            table="admin_incidents",
            id_col="incident_id",
            session_id=session_id,
            chat_id=chat_id,
            limit=limit,
        )

    # admin_actions
    def create_action(
        self,
        action_id: str,
        *,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._create_entity(
            table="admin_actions",
            id_col="action_id",
            entity_id=action_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def get_action(self, action_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        return self._get_entity(table="admin_actions", id_col="action_id", entity_id=action_id, chat_id=chat_id)

    def update_action(
        self,
        action_id: str,
        *,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._update_entity(
            table="admin_actions",
            id_col="action_id",
            entity_id=action_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def delete_action(self, action_id: str) -> bool:
        return self._delete_entity(table="admin_actions", id_col="action_id", entity_id=action_id)

    def list_actions(self, session_id: str, *, chat_id: Any = None, limit: int = 20) -> list[Dict[str, Any]]:
        return self._list_entities(
            table="admin_actions",
            id_col="action_id",
            session_id=session_id,
            chat_id=chat_id,
            limit=limit,
        )

    # admin_alerts_state
    def create_alert_state(
        self,
        alert_id: str,
        *,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._create_entity(
            table="admin_alerts_state",
            id_col="alert_id",
            entity_id=alert_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def get_alert_state(self, alert_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        return self._get_entity(table="admin_alerts_state", id_col="alert_id", entity_id=alert_id, chat_id=chat_id)

    def update_alert_state(
        self,
        alert_id: str,
        *,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._update_entity(
            table="admin_alerts_state",
            id_col="alert_id",
            entity_id=alert_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def delete_alert_state(self, alert_id: str) -> bool:
        return self._delete_entity(table="admin_alerts_state", id_col="alert_id", entity_id=alert_id)

    # admin_acknowledgements
    def create_acknowledgement(
        self,
        acknowledgement_id: str,
        *,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._create_entity(
            table="admin_acknowledgements",
            id_col="acknowledgement_id",
            entity_id=acknowledgement_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def get_acknowledgement(self, acknowledgement_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        return self._get_entity(
            table="admin_acknowledgements",
            id_col="acknowledgement_id",
            entity_id=acknowledgement_id,
            chat_id=chat_id,
        )

    def update_acknowledgement(
        self,
        acknowledgement_id: str,
        *,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._update_entity(
            table="admin_acknowledgements",
            id_col="acknowledgement_id",
            entity_id=acknowledgement_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def delete_acknowledgement(self, acknowledgement_id: str) -> bool:
        return self._delete_entity(
            table="admin_acknowledgements",
            id_col="acknowledgement_id",
            entity_id=acknowledgement_id,
        )

    # admin_approved_overrides
    def create_approved_override(
        self,
        override_id: str,
        *,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._create_entity(
            table="admin_approved_overrides",
            id_col="override_id",
            entity_id=override_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def get_approved_override(self, override_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        return self._get_entity(
            table="admin_approved_overrides",
            id_col="override_id",
            entity_id=override_id,
            chat_id=chat_id,
        )

    def update_approved_override(
        self,
        override_id: str,
        *,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._update_entity(
            table="admin_approved_overrides",
            id_col="override_id",
            entity_id=override_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def delete_approved_override(self, override_id: str) -> bool:
        return self._delete_entity(
            table="admin_approved_overrides",
            id_col="override_id",
            entity_id=override_id,
        )

    def list_approved_overrides(
        self,
        session_id: str,
        *,
        chat_id: Any = None,
        limit: int = 20,
    ) -> list[Dict[str, Any]]:
        return self._list_entities(
            table="admin_approved_overrides",
            id_col="override_id",
            session_id=session_id,
            chat_id=chat_id,
            limit=limit,
        )

    def clear_approved_overrides(self, session_id: str, *, chat_id: Any = None) -> int:
        return self._clear_entities_by_session(
            table="admin_approved_overrides",
            session_id=session_id,
            chat_id=chat_id,
        )

    # admin_digests
    def create_digest(
        self,
        digest_id: str,
        *,
        session_id: str,
        chat_id: Any = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._create_entity(
            table="admin_digests",
            id_col="digest_id",
            entity_id=digest_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def get_digest(self, digest_id: str, *, chat_id: Any = None) -> Optional[Dict[str, Any]]:
        return self._get_entity(table="admin_digests", id_col="digest_id", entity_id=digest_id, chat_id=chat_id)

    def update_digest(
        self,
        digest_id: str,
        *,
        session_id: Optional[str] = None,
        chat_id: Any = _UNSET,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._update_entity(
            table="admin_digests",
            id_col="digest_id",
            entity_id=digest_id,
            session_id=session_id,
            chat_id=chat_id,
            payload=payload,
        )

    def delete_digest(self, digest_id: str) -> bool:
        return self._delete_entity(table="admin_digests", id_col="digest_id", entity_id=digest_id)
