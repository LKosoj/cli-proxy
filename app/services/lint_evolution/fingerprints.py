from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Iterator

from .paths import db_path, ensure_directories

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    ts REAL NOT NULL,
    source_path TEXT NOT NULL,
    rule_kind TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    raw_text TEXT NOT NULL,
    schema_v INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_signals_project_kind ON signals(project_id, rule_kind);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup ON signals(project_id, source_path, rule_kind, subject_hash);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    started_ts REAL NOT NULL,
    finished_ts REAL,
    status TEXT NOT NULL,
    candidates_count INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_project_level ON runs(project_id, level);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    ts REAL NOT NULL,
    outcome TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    schema_v INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_outcomes_rule ON outcomes(project_id, rule_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON outcomes(ts);

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_ts REAL NOT NULL,
    fields_added TEXT NOT NULL DEFAULT '',
    fields_removed TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class FingerprintRow:
    rule_kind: str
    weighted_count: float
    distinct_subjects: int
    last_seen_ts: float


@dataclass(frozen=True)
class SignalRecord:
    project_id: str
    ts: float
    source_path: str
    rule_kind: str
    subject_hash: str
    weight: float
    raw_text: str
    schema_v: int = 1


@contextmanager
def _connect(workdir: str) -> Iterator[sqlite3.Connection]:
    ensure_directories(workdir)
    conn = sqlite3.connect(str(db_path(workdir)))
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_signals(workdir: str, signals: Iterable[SignalRecord]) -> int:
    rows = [
        (s.project_id, s.ts, s.source_path, s.rule_kind, s.subject_hash, s.weight, s.raw_text, s.schema_v)
        for s in signals
    ]
    if not rows:
        return 0
    with _connect(workdir) as conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO signals
                (project_id, ts, source_path, rule_kind, subject_hash, weight, raw_text, schema_v)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return int(cur.rowcount or 0)


def signals_count_since(workdir: str, *, project_id: str, since_ts: float) -> int:
    with _connect(workdir) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM signals WHERE project_id = ? AND ts > ?",
            (project_id, float(since_ts)),
        )
        row = cur.fetchone()
        return int(row["c"] if row else 0)


def fingerprints_in_window(
    workdir: str,
    *,
    project_id: str,
    window_seconds: float,
    now: float | None = None,
) -> list[FingerprintRow]:
    end = float(now if now is not None else time.time())
    start = end - max(0.0, window_seconds)
    with _connect(workdir) as conn:
        cur = conn.execute(
            """
            SELECT rule_kind,
                   SUM(weight) AS weighted_count,
                   COUNT(DISTINCT subject_hash) AS distinct_subjects,
                   MAX(ts) AS last_seen_ts
              FROM signals
             WHERE project_id = ? AND ts >= ?
             GROUP BY rule_kind
             ORDER BY weighted_count DESC
            """,
            (project_id, start),
        )
        return [
            FingerprintRow(
                rule_kind=str(r["rule_kind"]),
                weighted_count=float(r["weighted_count"] or 0.0),
                distinct_subjects=int(r["distinct_subjects"] or 0),
                last_seen_ts=float(r["last_seen_ts"] or 0.0),
            )
            for r in cur.fetchall()
        ]


def record_run(
    workdir: str,
    *,
    project_id: str,
    level: int,
    started_ts: float,
    finished_ts: float | None,
    status: str,
    candidates_count: int = 0,
    applied_count: int = 0,
    notes: str = "",
) -> int:
    with _connect(workdir) as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (project_id, level, started_ts, finished_ts, status, candidates_count, applied_count, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, int(level), float(started_ts), finished_ts, status, int(candidates_count), int(applied_count), notes),
        )
        return int(cur.lastrowid or 0)


def finish_run(
    workdir: str,
    run_id: int,
    *,
    finished_ts: float,
    status: str,
    candidates_count: int = 0,
    applied_count: int = 0,
    notes: str = "",
) -> None:
    with _connect(workdir) as conn:
        conn.execute(
            """
            UPDATE runs
               SET finished_ts = ?,
                   status = ?,
                   candidates_count = ?,
                   applied_count = ?,
                   notes = ?
             WHERE id = ?
            """,
            (
                float(finished_ts),
                str(status),
                int(candidates_count),
                int(applied_count),
                str(notes or ""),
                int(run_id),
            ),
        )


def insert_outcome(
    workdir: str,
    *,
    project_id: str,
    rule_id: str,
    outcome: str,
    weight: float = 1.0,
    schema_v: int = 1,
    ts: float | None = None,
) -> None:
    when = float(ts if ts is not None else time.time())
    with _connect(workdir) as conn:
        conn.execute(
            "INSERT INTO outcomes (project_id, rule_id, ts, outcome, weight, schema_v) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, rule_id, when, outcome, float(weight), int(schema_v)),
        )


def outcomes_since(workdir: str, *, project_id: str, since_ts: float) -> list[tuple[str, str, float]]:
    with _connect(workdir) as conn:
        cur = conn.execute(
            "SELECT rule_id, outcome, weight FROM outcomes WHERE project_id = ? AND ts >= ?",
            (project_id, float(since_ts)),
        )
        return [(str(r["rule_id"]), str(r["outcome"]), float(r["weight"])) for r in cur.fetchall()]


def outcomes_for_rule(
    workdir: str,
    *,
    project_id: str,
    rule_id: str,
    limit: int | None = None,
) -> list[tuple[float, str, float]]:
    sql = "SELECT ts, outcome, weight FROM outcomes WHERE project_id = ? AND rule_id = ? ORDER BY ts DESC"
    args: tuple = (project_id, rule_id)
    if limit is not None:
        sql += " LIMIT ?"
        args = args + (int(limit),)
    with _connect(workdir) as conn:
        cur = conn.execute(sql, args)
        return [(float(r["ts"]), str(r["outcome"]), float(r["weight"])) for r in cur.fetchall()]


def fp_rate_window(
    workdir: str,
    *,
    project_id: str,
    window_seconds: float,
    now: float | None = None,
) -> tuple[int, int]:
    """Return (false_positive_count, total_count) over rolling window across all rules."""
    end = float(now if now is not None else time.time())
    start = end - max(0.0, window_seconds)
    with _connect(workdir) as conn:
        cur = conn.execute(
            """
            SELECT
                SUM(CASE WHEN outcome IN ('reverted','ignored') THEN 1 ELSE 0 END) AS fp,
                COUNT(*) AS total
              FROM outcomes
             WHERE project_id = ? AND ts >= ?
            """,
            (project_id, start),
        )
        row = cur.fetchone()
        if row is None:
            return (0, 0)
        return (int(row["fp"] or 0), int(row["total"] or 0))
