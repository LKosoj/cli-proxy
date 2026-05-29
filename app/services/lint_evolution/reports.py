from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import ensure_directories, reports_dir


@dataclass
class DecisionEntry:
    rule_kind: str
    decision: str
    reason: str
    score: float = 0.0
    rule_id: str = ""
    classification: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    project_id: str
    level: int
    started_ts: float
    finished_ts: float = 0.0
    status: str = "running"
    signals_ingested: int = 0
    candidates_examined: int = 0
    decisions: list[DecisionEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _filename(level: int, started_ts: float) -> str:
    dt = datetime.fromtimestamp(started_ts, tz=timezone.utc)
    return f"L{level}_{dt.strftime('%Y%m%dT%H%M%SZ')}.json"


def write_report(workdir: str, report: RunReport) -> Path:
    ensure_directories(workdir)
    path = reports_dir(workdir) / _filename(report.level, report.started_ts)
    if not report.finished_ts:
        report.finished_ts = time.time()
    payload = asdict(report)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
