from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonicalizer import canonicalize
from .fingerprints import SignalRecord

logger = logging.getLogger(__name__)

_TS_PATTERN = re.compile(r"^(\d{8}_\d{6})_")
_HEADER_TS_PATTERN = re.compile(r"\*\*Timestamp:\*\*\s+([0-9\- :T+]+)")
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

_REVIEW_WEIGHT = 3.0
_MIN_TEXT_LEN = 8


@dataclass(frozen=True)
class IngestStats:
    files_seen: int
    signals_emitted: int
    files_skipped: int


def _extract_ts(path: Path, body: str) -> float:
    # Filesystem mtime отражает реальный момент появления review-файла
    # (rotation/copy сбросит mtime — это желаемое поведение для окна сигналов).
    # Парсинг даты из имени/заголовка — только fallback, иначе fixture-сиды и
    # архивные дампы со старыми датами в имени выпадают из 30-дневного окна.
    try:
        return path.stat().st_mtime
    except OSError:
        pass
    match = _TS_PATTERN.match(path.name)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    header = _HEADER_TS_PATTERN.search(body)
    if header:
        raw = header.group(1).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
    return 0.0


def _extract_json_block(body: str) -> dict | None:
    text = body.split("---", 1)[-1] if "---" in body else body
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        match = _JSON_OBJECT_PATTERN.search(text)
        candidate = match.group(0) if match else None
    if not candidate:
        return None
    try:
        parsed = loads_safe(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _negative_texts(payload: dict) -> Iterator[str]:
    if payload.get("approved") is True and not payload.get("comments") and not payload.get("not_done_assessment"):
        return
    comments = str(payload.get("comments") or "").strip()
    if comments:
        yield comments
    for item in payload.get("not_done_assessment") or []:
        if not isinstance(item, dict):
            continue
        comment = str(item.get("comment") or "").strip()
        if comment:
            yield comment
        why = str(item.get("why_not") or "").strip()
        if why and why != comment:
            yield why
    summary = str(payload.get("summary") or "").strip()
    if payload.get("approved") is False and summary:
        yield summary


def parse_review_file(path: Path) -> Iterator[tuple[str, float]]:
    """Yield (text, timestamp) tuples extracted from a single review_result markdown file."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("lint_evolution: cannot read %s: %s", path, exc)
        return
    payload = _extract_json_block(body)
    if not payload:
        return
    ts = _extract_ts(path, body)
    for text in _negative_texts(payload):
        if len(text) < _MIN_TEXT_LEN:
            continue
        yield (text, ts)


def collect_signals(
    *,
    project_id: str,
    project_root: Path,
    glob_patterns: Iterable[str],
    since_ts: float = 0.0,
) -> tuple[list[SignalRecord], IngestStats]:
    """Walk *glob_patterns* relative to *project_root*, parse review files, build SignalRecords."""
    signals: list[SignalRecord] = []
    files_seen = 0
    files_skipped = 0
    seen_paths: set[str] = set()
    for pattern in glob_patterns or ():
        for path in sorted(project_root.glob(pattern)):
            spath = str(path)
            if spath in seen_paths:
                continue
            seen_paths.add(spath)
            files_seen += 1
            emitted_before = len(signals)
            for text, ts in parse_review_file(path):
                if ts <= since_ts:
                    continue
                canon = canonicalize(text)
                signals.append(
                    SignalRecord(
                        project_id=project_id,
                        ts=ts,
                        source_path=spath,
                        rule_kind=canon.rule_kind,
                        subject_hash=canon.subject_hash,
                        weight=_REVIEW_WEIGHT,
                        raw_text=text[:1000],
                    )
                )
            if len(signals) == emitted_before:
                files_skipped += 1
    return signals, IngestStats(files_seen=files_seen, signals_emitted=len(signals), files_skipped=files_skipped)
