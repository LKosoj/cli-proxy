from __future__ import annotations

import hashlib
import json

from modes.sdk.runtime.json_normalizer import loads_safe
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

_log = logging.getLogger(__name__)

_SERVER_ID_RE = re.compile(r"[^A-Za-z0-9_.\-]+")

SEVERITY_NOISE = "noise"
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ALARM = "alarm"

SEVERITY_RANK = {
    SEVERITY_NOISE: 0,
    SEVERITY_INFO: 1,
    SEVERITY_WARN: 2,
    SEVERITY_ALARM: 3,
}

DEFAULT_RETENTION_DAYS = 30


class AdminSnapshotStoreError(RuntimeError):
    """Raised when per-server snapshot store operation fails."""


def safe_server_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise AdminSnapshotStoreError("server_id is empty")
    cleaned = _SERVER_ID_RE.sub("_", raw)
    if not cleaned or cleaned in {".", ".."}:
        raise AdminSnapshotStoreError(f"server_id is invalid: {raw!r}")
    return cleaned[:128]


def admin_root(workdir: str) -> Path:
    base = Path(str(workdir or "")).expanduser()
    return base / ".cli-proxy" / ".admin"


def server_dir(workdir: str, server_id: str) -> Path:
    return admin_root(workdir) / "servers" / safe_server_id(server_id)


def snapshot_db_path(workdir: str, server_id: str) -> str:
    return str(server_dir(workdir, server_id) / "snapshots.sqlite")


def canonical_hash(value: Any) -> str:
    try:
        normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        normalized = repr(value)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


class AdminSnapshotStore:
    """
    Per-server time-series store для snapshot'ов check-ов и зафиксированных drift'ов.

    Один файл SQLite на сервер: <workdir>/.cli-proxy/.admin/servers/<server_id>/snapshots.sqlite.
    Retention по умолчанию 30 дней.
    """

    def __init__(self, db_path: str, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        clean = str(db_path or "").strip()
        if not clean:
            raise AdminSnapshotStoreError("db_path is empty")
        self.db_path = clean
        self.retention_days = int(retention_days) if retention_days else DEFAULT_RETENTION_DAYS
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.ensure_schema()

    @classmethod
    def for_server(
        cls,
        workdir: str,
        server_id: str,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> "AdminSnapshotStore":
        return cls(snapshot_db_path(workdir, server_id), retention_days=retention_days)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         INTEGER NOT NULL,
                    check_id   TEXT    NOT NULL,
                    value_json TEXT    NOT NULL,
                    value_hash TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_check_ts
                    ON snapshots(check_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_snapshots_ts
                    ON snapshots(ts DESC);

                CREATE TABLE IF NOT EXISTS drifts (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           INTEGER NOT NULL,
                    check_id     TEXT    NOT NULL,
                    severity     TEXT    NOT NULL,
                    prev_hash    TEXT,
                    new_hash     TEXT    NOT NULL,
                    prev_value   TEXT,
                    new_value    TEXT    NOT NULL,
                    details_json TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    ack_ts       INTEGER,
                    ack_by       TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_drifts_ts
                    ON drifts(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_drifts_sev_ack
                    ON drifts(severity, acknowledged, ts DESC);

                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    # --- snapshots ---

    def insert_snapshot(
        self,
        *,
        check_id: str,
        value: Any,
        ts: Optional[int] = None,
    ) -> Tuple[int, str]:
        cid = self._normalize_id(check_id, field="check_id")
        ts_value = int(ts if ts is not None else time.time())
        value_json = _json_dumps(value)
        value_hash = canonical_hash(value)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO snapshots (ts, check_id, value_json, value_hash) VALUES (?, ?, ?, ?)",
                (ts_value, cid, value_json, value_hash),
            )
            conn.commit()
            return int(cur.lastrowid or 0), value_hash

    def latest_snapshot(self, check_id: str) -> Optional[Dict[str, Any]]:
        cid = self._normalize_id(check_id, field="check_id")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, ts, check_id, value_json, value_hash "
                "FROM snapshots WHERE check_id=? ORDER BY ts DESC LIMIT 1",
                (cid,),
            ).fetchone()
        return _snapshot_row_to_dict(row)

    def snapshots_in_window(
        self,
        check_id: str,
        *,
        since_ts: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        cid = self._normalize_id(check_id, field="check_id")
        limit = max(1, min(int(limit or 100), 10000))
        with self._lock, self._connect() as conn:
            if since_ts is None:
                rows = conn.execute(
                    "SELECT id, ts, check_id, value_json, value_hash "
                    "FROM snapshots WHERE check_id=? ORDER BY ts DESC LIMIT ?",
                    (cid, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, check_id, value_json, value_hash "
                    "FROM snapshots WHERE check_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                    (cid, int(since_ts), limit),
                ).fetchall()
        return [r for r in (_snapshot_row_to_dict(row) for row in rows) if r is not None]

    def all_check_ids(self) -> List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT check_id FROM snapshots ORDER BY check_id").fetchall()
        return [str(row["check_id"] or "") for row in rows if row["check_id"]]

    def last_snapshot_ts(self) -> Optional[int]:
        """Unix ts самого свежего snapshot'а среди всех check_id (None если база пуста)."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT MAX(ts) AS ts FROM snapshots").fetchone()
        if row is None:
            return None
        ts = row["ts"]
        return int(ts) if ts is not None else None

    # --- drifts ---

    def insert_drift(
        self,
        *,
        check_id: str,
        severity: str,
        new_value: Any,
        new_hash: Optional[str] = None,
        prev_value: Any = None,
        prev_hash: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        ts: Optional[int] = None,
    ) -> int:
        cid = self._normalize_id(check_id, field="check_id")
        sev = self._normalize_severity(severity)
        ts_value = int(ts if ts is not None else time.time())
        new_json = _json_dumps(new_value)
        new_hash_val = new_hash or canonical_hash(new_value)
        prev_json = _json_dumps(prev_value) if prev_value is not None else None
        details_json = _json_dumps(dict(details or {})) if details else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO drifts "
                "(ts, check_id, severity, prev_hash, new_hash, prev_value, new_value, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_value, cid, sev, prev_hash, new_hash_val, prev_json, new_json, details_json),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_drifts(
        self,
        *,
        limit: int = 50,
        severity_min: Optional[str] = None,
        include_acknowledged: bool = True,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 1000))
        sql = (
            "SELECT id, ts, check_id, severity, prev_hash, new_hash, prev_value, new_value, "
            "details_json, acknowledged, ack_ts, ack_by FROM drifts"
        )
        conds: List[str] = []
        args: List[Any] = []
        if severity_min:
            rank = SEVERITY_RANK.get(self._normalize_severity(severity_min), 0)
            allowed = [k for k, v in SEVERITY_RANK.items() if v >= rank]
            conds.append("severity IN (" + ",".join("?" for _ in allowed) + ")")
            args.extend(allowed)
        if not include_acknowledged:
            conds.append("acknowledged = 0")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [r for r in (_drift_row_to_dict(row) for row in rows) if r is not None]

    def ack_drift(self, drift_id: int, *, by: Optional[str] = None) -> bool:
        did = int(drift_id or 0)
        if did <= 0:
            return False
        ts_value = int(time.time())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE drifts SET acknowledged=1, ack_ts=?, ack_by=? WHERE id=?",
                (ts_value, str(by or "").strip() or None, did),
            )
            conn.commit()
            return cur.rowcount > 0

    def drift_stats(self) -> Dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT severity, COUNT(*) AS n FROM drifts WHERE acknowledged=0 GROUP BY severity"
            ).fetchall()
        stats = {k: 0 for k in SEVERITY_RANK}
        for row in rows:
            sev = str(row["severity"] or "")
            if sev in stats:
                stats[sev] = int(row["n"] or 0)
        return stats

    # --- retention / maintenance ---

    def cleanup_retention(self, *, max_age_days: Optional[int] = None) -> Dict[str, int]:
        days = int(max_age_days if max_age_days is not None else self.retention_days)
        if days <= 0:
            return {"snapshots_deleted": 0, "drifts_deleted": 0}
        cutoff = int(time.time()) - days * 86400
        with self._lock, self._connect() as conn:
            snap_del = conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,)).rowcount
            drift_del = conn.execute(
                "DELETE FROM drifts WHERE acknowledged=1 AND ts < ?",
                (cutoff,),
            ).rowcount
            conn.commit()
            self._set_meta_conn(conn, "last_cleanup_ts", str(int(time.time())))
        return {"snapshots_deleted": int(snap_del or 0), "drifts_deleted": int(drift_del or 0)}

    def vacuum(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("VACUUM")

    def set_meta(self, key: str, value: str) -> None:
        k = self._normalize_id(key, field="meta_key")
        with self._lock, self._connect() as conn:
            self._set_meta_conn(conn, k, str(value or ""))

    def get_meta(self, key: str) -> Optional[str]:
        k = self._normalize_id(key, field="meta_key")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return str(row["value"]) if row and row["value"] is not None else None

    def bump_meta(self, key: str, delta: int = 1) -> int:
        """Atomically increment an integer-valued meta key. Returns the new value."""
        k = self._normalize_id(key, field="meta_key")
        d = int(delta)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = CAST(CAST(meta.value AS INTEGER) + ? AS TEXT)",
                (k, str(d), d),
            )
            row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            conn.commit()
        try:
            return int(row["value"]) if row and row["value"] is not None else d
        except (TypeError, ValueError):
            return d

    # --- helpers ---

    @staticmethod
    def _normalize_id(value: Any, *, field: str) -> str:
        out = str(value or "").strip()
        if not out:
            raise AdminSnapshotStoreError(f"{field} is empty")
        return out

    @staticmethod
    def _normalize_severity(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw not in SEVERITY_RANK:
            raise AdminSnapshotStoreError(f"invalid severity: {value!r}")
        return raw

    @staticmethod
    def _set_meta_conn(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps(repr(value), ensure_ascii=False)


def _json_loads(raw: Any) -> Any:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return loads_safe(raw)
    except Exception:
        return None


def _snapshot_row_to_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": int(row["id"] or 0),
        "ts": int(row["ts"] or 0),
        "check_id": str(row["check_id"] or ""),
        "value_json": str(row["value_json"] or ""),
        "value": _json_loads(row["value_json"]),
        "value_hash": str(row["value_hash"] or ""),
    }


def _drift_row_to_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": int(row["id"] or 0),
        "ts": int(row["ts"] or 0),
        "check_id": str(row["check_id"] or ""),
        "severity": str(row["severity"] or ""),
        "prev_hash": row["prev_hash"],
        "new_hash": str(row["new_hash"] or ""),
        "prev_value": _json_loads(row["prev_value"]),
        "new_value": _json_loads(row["new_value"]),
        "details": _json_loads(row["details_json"]) or {},
        "acknowledged": bool(row["acknowledged"] or 0),
        "ack_ts": int(row["ack_ts"] or 0) if row["ack_ts"] is not None else None,
        "ack_by": str(row["ack_by"] or "") or None,
    }
