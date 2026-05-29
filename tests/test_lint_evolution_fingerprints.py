from __future__ import annotations

import time
from pathlib import Path
import sqlite3

from app.services.lint_evolution.fingerprints import (
    SignalRecord,
    finish_run,
    fingerprints_in_window,
    fp_rate_window,
    insert_outcome,
    insert_signals,
    record_run,
    signals_count_since,
)
from app.services.lint_evolution.paths import db_path


def _signal(project_id: str, kind: str, subject: str, *, ts: float, weight: float = 3.0, source: str = "src") -> SignalRecord:
    return SignalRecord(
        project_id=project_id,
        ts=ts,
        source_path=f"{source}:{kind}:{subject}",
        rule_kind=kind,
        subject_hash=subject,
        weight=weight,
        raw_text=f"{kind} - {subject}",
    )


def test_insert_signals_dedup(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    s = _signal("p1", "tests_failing", "abc", ts=100.0)
    inserted = insert_signals(workdir, [s, s, s])
    assert inserted == 1


def test_signals_count_since(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    insert_signals(workdir, [_signal("p1", "tests_failing", f"s{i}", ts=10.0 + i) for i in range(5)])
    assert signals_count_since(workdir, project_id="p1", since_ts=0.0) == 5
    assert signals_count_since(workdir, project_id="p1", since_ts=12.0) == 2
    assert signals_count_since(workdir, project_id="other", since_ts=0.0) == 0


def test_fingerprints_aggregation(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = time.time()
    insert_signals(
        workdir,
        [
            _signal("p1", "unused_imports", "a", ts=now, weight=3.0),
            _signal("p1", "unused_imports", "b", ts=now, weight=3.0),
            _signal("p1", "unused_imports", "c", ts=now, weight=3.0),
            _signal("p1", "syntax_error", "x", ts=now, weight=3.0),
        ],
    )
    rows = fingerprints_in_window(workdir, project_id="p1", window_seconds=3600, now=now)
    by_kind = {r.rule_kind: r for r in rows}
    assert by_kind["unused_imports"].weighted_count == 9.0
    assert by_kind["unused_imports"].distinct_subjects == 3
    assert by_kind["syntax_error"].weighted_count == 3.0


def test_fingerprints_window_filters_old(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = time.time()
    insert_signals(
        workdir,
        [
            _signal("p1", "logic_error", "old", ts=now - 10_000, weight=3.0),
            _signal("p1", "logic_error", "new", ts=now, weight=3.0),
        ],
    )
    rows = fingerprints_in_window(workdir, project_id="p1", window_seconds=3600, now=now)
    assert len(rows) == 1
    assert rows[0].distinct_subjects == 1


def test_fp_rate_window(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    now = time.time()
    insert_outcome(workdir, project_id="p1", rule_id="r1", outcome="committed", ts=now)
    insert_outcome(workdir, project_id="p1", rule_id="r1", outcome="committed", ts=now)
    insert_outcome(workdir, project_id="p1", rule_id="r1", outcome="reverted", ts=now)
    insert_outcome(workdir, project_id="p1", rule_id="r2", outcome="ignored", ts=now)
    fp, total = fp_rate_window(workdir, project_id="p1", window_seconds=3600, now=now)
    assert (fp, total) == (2, 4)


def test_record_run(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rid = record_run(
        workdir,
        project_id="p1",
        level=1,
        started_ts=10.0,
        finished_ts=11.0,
        status="ok",
        candidates_count=2,
        applied_count=1,
    )
    assert rid > 0


def test_finish_run_updates_existing_row(tmp_path: Path) -> None:
    workdir = str(tmp_path)
    rid = record_run(
        workdir,
        project_id="p1",
        level=1,
        started_ts=10.0,
        finished_ts=None,
        status="running",
    )

    finish_run(
        workdir,
        rid,
        finished_ts=12.0,
        status="ok",
        candidates_count=3,
        applied_count=2,
        notes="done",
    )

    with sqlite3.connect(str(db_path(workdir))) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 1
    assert row[4] == 12.0
    assert row[5] == "ok"
    assert row[6] == 3
    assert row[7] == 2
    assert row[8] == "done"
