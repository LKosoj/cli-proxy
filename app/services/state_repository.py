from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, URL

from app.services.path_normalization import normalize_state_path
from app.services.session_state import SessionState
from modes.sdk.runtime.json_normalizer import loads_safe
from sessions.conversation_scope import ConversationScope

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "2"
_CHAT_META_KEYS = frozenset({"counter"})


class JsonStateRepository:
    """Unified access layer over shared runtime state using SQLite."""

    def __init__(self, path: Any):
        normalized_path = normalize_state_path(path)
        self.path = normalized_path
        self.db_path = self._derive_db_path(normalized_path)
        self._lock = threading.RLock()
        self._engine = self._create_engine(self.db_path)
        self._ensure_schema()

    @staticmethod
    def _derive_db_path(path: Any) -> str:
        normalized = normalize_state_path(path)
        lower = normalized.lower()
        if lower.endswith(".sqlite") or lower.endswith(".sqlite3") or lower.endswith(".db"):
            return normalized
        return f"{normalized}.sqlite3"

    @staticmethod
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    def _create_engine(self, db_path: str) -> Engine:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        url = URL.create("sqlite", database=db_path)
        engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
        )
        event.listen(engine, "connect", self._on_connect)
        return engine

    def _ensure_schema(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        chat_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        session_uid TEXT NOT NULL DEFAULT '',
                        session_surface TEXT NOT NULL DEFAULT 'chat',
                        updated_at REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY(chat_id, session_id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS chat_meta (
                        chat_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        PRIMARY KEY(chat_id, key)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS namespaces (
                        namespace TEXT NOT NULL,
                        item_key TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(namespace, item_key)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS repository_meta (
                        key TEXT NOT NULL PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id)"))
            self._validate_session_scope_columns(conn)
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(session_uid)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sessions_surface ON sessions(session_surface)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_namespaces_namespace ON namespaces(namespace)"))
            self._set_meta_conn(conn, "schema_version", _SCHEMA_VERSION)

    def _validate_session_scope_columns(self, conn: Connection) -> None:
        rows = conn.execute(text("PRAGMA table_info(sessions)")).mappings().all()
        columns = {str(row.get("name") or "") for row in rows}
        required = {"session_uid", "session_surface"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(
                "state repository schema is outdated: sessions table is missing required columns "
                + ", ".join(missing)
            )

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads_any(raw: Any, *, fallback: Any) -> Any:
        if not isinstance(raw, str):
            return fallback
        try:
            return loads_safe(raw, strict_first=True)
        except Exception:
            logger.exception("state repository failed to parse stored json")
            return fallback

    @classmethod
    def _loads_dict(cls, raw: Any) -> Dict[str, Any]:
        parsed = cls._loads_any(raw, fallback={})
        return dict(parsed) if isinstance(parsed, dict) else {}

    @classmethod
    def _loads_float(cls, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _normalize_session_payload(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        return dict(payload)

    @classmethod
    def _derive_session_scope(cls, *, chat_id: Any, payload: Any) -> ConversationScope:
        return ConversationScope.from_payload(chat_id, cls._normalize_session_payload(payload))

    @classmethod
    def _derive_session_scope_fields(cls, *, chat_id: Any, payload: Any) -> Dict[str, str]:
        normalized_payload = cls._normalize_session_payload(payload)
        session_uid = str(normalized_payload.get("session_uid") or "").strip()
        session_surface = str(normalized_payload.get("session_surface") or "").strip()
        if session_uid and session_surface:
            return {
                "session_uid": session_uid,
                "session_surface": session_surface,
            }
        scope = cls._derive_session_scope(chat_id=chat_id, payload=normalized_payload)
        return {
            "session_uid": scope.session_uid,
            "session_surface": scope.session_surface,
        }

    def _set_meta_conn(self, conn: Connection, key: str, value: str) -> None:
        conn.execute(
            text(
                """
                INSERT INTO repository_meta(key, value)
                VALUES (:key, :value)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """
            ),
            {"key": str(key), "value": str(value)},
        )

    @staticmethod
    def _normalize_by_chat(by_chat: Any) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        if not isinstance(by_chat, dict):
            return normalized
        for chat_key, raw_entry in by_chat.items():
            if not isinstance(raw_entry, dict):
                continue
            entry: Dict[str, Any] = {}
            sessions_payload = raw_entry.get("sessions")
            sessions_dict: Dict[str, Dict[str, Any]] = {}
            if isinstance(sessions_payload, dict):
                for session_id, session_payload in sessions_payload.items():
                    if isinstance(session_payload, dict):
                        sessions_dict[str(session_id)] = dict(session_payload)
            entry["sessions"] = sessions_dict
            if "counter" in raw_entry:
                entry["counter"] = raw_entry.get("counter")
            normalized[str(chat_key)] = entry
        return normalized

    @staticmethod
    def _prune_chat_meta_conn(conn: Connection, chat_id: str) -> None:
        rows = conn.execute(
            text(
                """
                SELECT key
                FROM chat_meta
                WHERE chat_id=:chat_id
                """
            ),
            {"chat_id": str(chat_id)},
        ).mappings()
        for row in rows:
            key = str(row.get("key") or "").strip()
            if key in _CHAT_META_KEYS:
                continue
            conn.execute(
                text("DELETE FROM chat_meta WHERE chat_id=:chat_id AND key=:key"),
                {"chat_id": str(chat_id), "key": key},
            )

    def _insert_chat_entry_conn(self, conn: Connection, chat_id: str, entry: Dict[str, Any]) -> None:
        sessions = dict(entry.get("sessions") or {})
        for session_id, payload in sessions.items():
            session_payload = self._normalize_session_payload(payload)
            scope_fields = self._derive_session_scope_fields(chat_id=chat_id, payload=session_payload)
            conn.execute(
                text(
                    """
                    INSERT INTO sessions(chat_id, session_id, payload, session_uid, session_surface, updated_at)
                    VALUES (:chat_id, :session_id, :payload, :session_uid, :session_surface, :updated_at)
                    ON CONFLICT(chat_id, session_id)
                    DO UPDATE SET payload=excluded.payload,
                                  session_uid=excluded.session_uid,
                                  session_surface=excluded.session_surface,
                                  updated_at=excluded.updated_at
                    """
                ),
                {
                    "chat_id": str(chat_id),
                    "session_id": str(session_id),
                    "payload": self._dumps(session_payload),
                    "session_uid": scope_fields["session_uid"],
                    "session_surface": scope_fields["session_surface"],
                    "updated_at": self._loads_float(session_payload.get("updated_at")),
                },
            )

        for key, value in entry.items():
            if str(key) == "sessions" or str(key) not in _CHAT_META_KEYS:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO chat_meta(chat_id, key, value)
                    VALUES (:chat_id, :key, :value)
                    ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value
                    """
                ),
                {
                    "chat_id": str(chat_id),
                    "key": str(key),
                    "value": self._dumps(value),
                },
            )

    def _replace_all_chats_conn(self, conn: Connection, by_chat: Dict[str, Dict[str, Any]]) -> None:
        conn.execute(text("DELETE FROM sessions"))
        conn.execute(text("DELETE FROM chat_meta"))
        for chat_id, entry in by_chat.items():
            self._insert_chat_entry_conn(conn, str(chat_id), dict(entry))

    def _read_namespace_conn(self, conn: Connection, namespace: str) -> Dict[str, Any]:
        rows = conn.execute(
            text(
                """
                SELECT item_key, payload
                FROM namespaces
                WHERE namespace=:namespace
                ORDER BY item_key
                """
            ),
            {"namespace": str(namespace)},
        ).mappings()
        bucket: Dict[str, Any] = {}
        for row in rows:
            payload = self._loads_any(row.get("payload"), fallback=None)
            bucket[str(row.get("item_key") or "")] = payload
        return bucket

    def _replace_namespace_conn(self, conn: Connection, namespace: str, bucket: Dict[str, Any]) -> None:
        ns = str(namespace or "").strip()
        if not ns:
            return
        conn.execute(text("DELETE FROM namespaces WHERE namespace=:namespace"), {"namespace": ns})
        for key, value in dict(bucket or {}).items():
            conn.execute(
                text(
                    """
                    INSERT INTO namespaces(namespace, item_key, payload)
                    VALUES (:namespace, :item_key, :payload)
                    ON CONFLICT(namespace, item_key)
                    DO UPDATE SET payload=excluded.payload
                    """
                ),
                {
                    "namespace": ns,
                    "item_key": str(key),
                    "payload": self._dumps(value),
                },
            )

    def load_state(self, *, chat_id: Optional[int]) -> Dict[str, SessionState]:
        result: Dict[str, SessionState] = {}
        if chat_id is None:
            return result

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT session_id, payload
                    FROM sessions
                    WHERE chat_id=:chat_id
                    ORDER BY session_id
                    """
                ),
                {"chat_id": str(chat_id)},
            ).mappings()
            for row in rows:
                payload = self._loads_dict(row.get("payload"))
                cli_payload = payload.get("cli")
                if not isinstance(cli_payload, dict):
                    cli_payload = {}
                tool = str(cli_payload.get("active_cli", payload.get("active_cli", "")) or "")
                workdir = str(payload.get("workdir", "") or "")
                if not tool or not workdir:
                    continue
                resume_tokens = cli_payload.get("resume_tokens", payload.get("resume_tokens"))
                resume_token = None
                if isinstance(resume_tokens, dict):
                    resume_token = resume_tokens.get(tool)
                sid = str(row.get("session_id") or "")
                if not sid:
                    continue
                result[sid] = SessionState(
                    session_id=sid,
                    tool=tool,
                    workdir=workdir,
                    resume_token=resume_token,
                    summary=payload.get("summary"),
                    updated_at=self._loads_float(payload.get("updated_at")),
                    name=payload.get("name"),
                )
        return result

    def get_state(
        self,
        *,
        tool: str,
        workdir: str,
        session_id: Optional[str] = None,
        chat_id: Optional[int] = None,
    ) -> Optional[SessionState]:
        data = self.load_state(chat_id=chat_id)
        if session_id:
            st = data.get(str(session_id))
            if st:
                return st
        matches = [st for st in data.values() if st.tool == tool and st.workdir == workdir]
        if len(matches) == 1:
            return matches[0]
        return None

    def load_sessions_by_chat(self) -> Dict[str, Any]:
        by_chat: Dict[str, Dict[str, Any]] = {}
        with self._engine.connect() as conn:
            meta_rows = conn.execute(
                text(
                    """
                    SELECT chat_id, key, value
                    FROM chat_meta
                    ORDER BY chat_id, key
                    """
                )
            ).mappings()
            for row in meta_rows:
                chat_id = str(row.get("chat_id") or "")
                if not chat_id:
                    continue
                key = str(row.get("key") or "")
                if key not in _CHAT_META_KEYS:
                    continue
                entry = by_chat.setdefault(chat_id, {"sessions": {}})
                entry[key] = self._loads_any(row.get("value"), fallback=None)

            session_rows = conn.execute(
                text(
                    """
                    SELECT chat_id, session_id, payload, session_uid, session_surface
                    FROM sessions
                    ORDER BY chat_id, session_id
                    """
                )
            ).mappings()
            for row in session_rows:
                chat_id = str(row.get("chat_id") or "")
                session_id = str(row.get("session_id") or "")
                if not chat_id or not session_id:
                    continue
                entry = by_chat.setdefault(chat_id, {"sessions": {}})
                sessions = entry.setdefault("sessions", {})
                if not isinstance(sessions, dict):
                    sessions = {}
                    entry["sessions"] = sessions
                payload = self._loads_dict(row.get("payload"))
                if payload:
                    if not str(payload.get("session_uid") or "").strip():
                        payload["session_uid"] = str(row.get("session_uid") or "")
                    if not str(payload.get("session_surface") or "").strip():
                        payload["session_surface"] = str(row.get("session_surface") or "chat")
                sessions[session_id] = payload

        for entry in by_chat.values():
            if not isinstance(entry.get("sessions"), dict):
                entry["sessions"] = {}
        return by_chat

    def save_sessions_by_chat(self, by_chat: Dict[str, Any]) -> None:
        normalized = self._normalize_by_chat(by_chat)
        with self._lock:
            with self._engine.begin() as conn:
                self._replace_all_chats_conn(conn, normalized)

    def replace_chat_entry(self, *, chat_id: int, entry: Dict[str, Any]) -> None:
        chat_key = str(chat_id)
        normalized_all = self._normalize_by_chat({chat_key: entry})
        normalized_entry = normalized_all.get(chat_key, {"sessions": {}})
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(text("DELETE FROM sessions WHERE chat_id=:chat_id"), {"chat_id": chat_key})
                conn.execute(text("DELETE FROM chat_meta WHERE chat_id=:chat_id"), {"chat_id": chat_key})
                self._insert_chat_entry_conn(conn, chat_key, normalized_entry)

    def update_session_fields(self, *, chat_id: int, session_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        chat_key = str(chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return {}
        patch = dict(updates or {})

        with self._lock:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT payload
                        FROM sessions
                        WHERE chat_id=:chat_id AND session_id=:session_id
                        """
                    ),
                    {"chat_id": chat_key, "session_id": sid},
                ).mappings().first()
                current = self._loads_dict(row.get("payload")) if row is not None else {}
                current.update(patch)
                scope_fields = self._derive_session_scope_fields(chat_id=chat_key, payload=current)
                conn.execute(
                    text(
                        """
                        INSERT INTO sessions(chat_id, session_id, payload, session_uid, session_surface, updated_at)
                        VALUES (:chat_id, :session_id, :payload, :session_uid, :session_surface, :updated_at)
                        ON CONFLICT(chat_id, session_id)
                        DO UPDATE SET payload=excluded.payload,
                                      session_uid=excluded.session_uid,
                                      session_surface=excluded.session_surface,
                                      updated_at=excluded.updated_at
                        """
                    ),
                    {
                        "chat_id": chat_key,
                        "session_id": sid,
                        "payload": self._dumps(current),
                        "session_uid": scope_fields["session_uid"],
                        "session_surface": scope_fields["session_surface"],
                        "updated_at": self._loads_float(current.get("updated_at")),
                    },
                )
                self._prune_chat_meta_conn(conn, chat_key)
                return dict(current)

    def delete_session(self, *, chat_id: int, session_id: str) -> None:
        chat_key = str(chat_id)
        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM sessions WHERE chat_id=:chat_id AND session_id=:session_id"),
                    {"chat_id": chat_key, "session_id": sid},
                )

    def set_chat_counter(self, *, chat_id: int, counter: int) -> None:
        self._set_chat_meta(chat_id=chat_id, key="counter", value=int(counter))

    def _set_chat_meta(self, *, chat_id: int, key: str, value: Any) -> None:
        chat_key = str(chat_id)
        skey = str(key or "").strip()
        if not skey:
            return
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO chat_meta(chat_id, key, value)
                        VALUES (:chat_id, :key, :value)
                        ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value
                        """
                    ),
                    {
                        "chat_id": chat_key,
                        "key": skey,
                        "value": self._dumps(value),
                    },
                )
                self._prune_chat_meta_conn(conn, chat_key)

    def read_namespace(self, namespace: str) -> Dict[str, Any]:
        key = str(namespace or "").strip()
        if not key:
            return {}
        with self._engine.connect() as conn:
            return self._read_namespace_conn(conn, key)

    def update_namespace(
        self,
        namespace: str,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        key = str(namespace or "").strip()
        if not key:
            return {}

        with self._lock:
            with self._engine.begin() as conn:
                current = self._read_namespace_conn(conn, key)
                next_bucket = updater(dict(current))
                if not isinstance(next_bucket, dict):
                    next_bucket = current
                normalized: Dict[str, Any] = {
                    str(item_key): payload
                    for item_key, payload in next_bucket.items()
                }
                self._replace_namespace_conn(conn, key, normalized)
                return dict(normalized)

    def write_namespace(self, namespace: str, value: Dict[str, Any]) -> Dict[str, Any]:
        next_value = dict(value) if isinstance(value, dict) else {}
        return self.update_namespace(namespace, lambda _bucket: next_value)

    def load_pending_commands(self) -> Dict[str, Dict[str, Any]]:
        raw = self.read_namespace("_pending_commands")
        result: Dict[str, Dict[str, Any]] = {}
        for cmd_id, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            result[str(cmd_id)] = dict(payload)
        return result

    def replace_pending_commands(self, pending: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        for cmd_id, payload in (pending or {}).items():
            if isinstance(payload, dict):
                normalized[str(cmd_id)] = dict(payload)
        self.write_namespace("_pending_commands", normalized)
        return normalized

    def set_pending_command(self, cmd_id: str, payload: Dict[str, Any]) -> None:
        key = str(cmd_id or "").strip()
        if not key:
            return

        def _set(bucket: Dict[str, Any]) -> Dict[str, Any]:
            bucket[key] = dict(payload) if isinstance(payload, dict) else {}
            return bucket

        self.update_namespace("_pending_commands", _set)

    def pop_pending_command(self, cmd_id: str) -> Optional[Dict[str, Any]]:
        key = str(cmd_id or "").strip()
        if not key:
            return None
        popped: Dict[str, Any] = {}

        def _pop(bucket: Dict[str, Any]) -> Dict[str, Any]:
            value = bucket.pop(key, None)
            if isinstance(value, dict):
                popped.update(value)
            return bucket

        self.update_namespace("_pending_commands", _pop)
        return dict(popped) if popped else None


_REPOS_BY_PATH: Dict[str, JsonStateRepository] = {}
_REPOS_LOCK = threading.RLock()


def get_state_repository(path: Any) -> JsonStateRepository:
    normalized_path = normalize_state_path(path)
    with _REPOS_LOCK:
        repo = _REPOS_BY_PATH.get(normalized_path)
        if repo is None:
            repo = JsonStateRepository(normalized_path)
            _REPOS_BY_PATH[normalized_path] = repo
        return repo
