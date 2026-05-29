from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import candidates_store, fingerprints, rules_store, signals_ingestor
from .lint_decision import (
    Decision,
    DecisionConfig,
    DecisionResult,
    FeatureSet,
    SignalAggregate,
    decide,
)
from .paths import db_path, ensure_directories
from .reports import DecisionEntry, RunReport, write_report
from .rule_kinds import PATTERNS, UNKNOWN
from .temporal_xval import classify_stable

logger = logging.getLogger(__name__)

ClassifyFn = Callable[[str, list[str] | None], Awaitable[dict | None]]
_FINGERPRINT_WINDOW_SECONDS = 30 * 24 * 3600.0


@dataclass
class L1Config:
    decision_config: DecisionConfig = field(default_factory=DecisionConfig)
    glob_patterns: tuple[str, ...] = (
        ".cli-proxy/.manager/response/*_manager_review_result_*.md",
        ".cli-proxy/.manager/response/*_agent_review_response_*.md",
    )
    examples_per_classification: int = 5
    fingerprint_window_seconds: float = _FINGERPRINT_WINDOW_SECONDS


def _detector_pattern_for_kind(rule_kind: str) -> str:
    for entry in PATTERNS:
        if entry.kind == rule_kind:
            return entry.pattern.pattern
    return ""


def _collect_examples(workdir: str, *, project_id: str, rule_kind: str, limit: int) -> list[str]:
    sql = (
        "SELECT raw_text FROM signals WHERE project_id = ? AND rule_kind = ? ORDER BY ts DESC LIMIT ?"
    )
    conn = sqlite3.connect(str(db_path(workdir)))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, (project_id, rule_kind, int(limit)))
        return [str(r["raw_text"]) for r in cur.fetchall()]
    finally:
        conn.close()


async def _classify_with_xval(
    classify_fn: ClassifyFn,
    *,
    primary_text: str,
    examples: list[str],
):
    async def runner():
        return await classify_fn(primary_text, examples)

    return await classify_stable(runner)


def _build_rule(
    rule_kind: str,
    classification: dict,
    *,
    run_id: int,
    schema_v: int,
    example: str,
    detector_type: str,
) -> rules_store.Rule:
    rule_id = rules_store.make_rule_id(rule_kind, classification.get("rule_kind", "") or rule_kind)
    pattern = _detector_pattern_for_kind(rule_kind)
    payload = rules_store.DetectorPayload(pattern=pattern, target_glob="**/*.py")
    return rules_store.Rule(
        id=rule_id,
        rule_kind=rule_kind,
        detector_type=detector_type if detector_type in {"regex", "ast", "shell"} else "regex",
        detector_payload=payload,
        metadata=rules_store.RuleMetadata(
            added_ts=time.time(),
            added_run_id=run_id,
            schema_v=schema_v,
            example_signal=example[:500],
            classification=dict(classification),
        ),
        state="active",
    )


async def run_level1(
    *,
    workdir: str,
    project_id: str,
    project_root: Path,
    classify_fn: ClassifyFn,
    config: L1Config,
    schema_version: int = 1,
    since_ts: float = 0.0,
) -> RunReport:
    ensure_directories(workdir)
    started = time.time()
    report = RunReport(project_id=project_id, level=1, started_ts=started, status="running")
    run_id: int | None = None

    try:
        signals, stats = signals_ingestor.collect_signals(
            project_id=project_id,
            project_root=project_root,
            glob_patterns=config.glob_patterns,
            since_ts=since_ts,
        )
        report.signals_ingested = fingerprints.insert_signals(workdir, signals)
        logger.info(
            "lint_evolution.level1: ingested %d new signals (files_seen=%d)",
            report.signals_ingested,
            stats.files_seen,
        )

        rows = fingerprints.fingerprints_in_window(
            workdir,
            project_id=project_id,
            window_seconds=config.fingerprint_window_seconds,
            now=started,
        )
        active_rules = rules_store.load_rules(workdir)
        active_kinds = {r.rule_kind for r in active_rules if r.state == "active"}

        run_id = fingerprints.record_run(
            workdir,
            project_id=project_id,
            level=1,
            started_ts=started,
            finished_ts=None,
            status="running",
        )

        for row in rows:
            if row.rule_kind == UNKNOWN:
                continue
            if row.rule_kind in active_kinds:
                continue
            if candidates_store.has_pending_kind(workdir, row.rule_kind):
                continue
            if row.weighted_count < config.decision_config.min_weighted_count:
                continue
            if row.distinct_subjects < config.decision_config.min_distinct_subjects:
                continue

            examples = _collect_examples(
                workdir,
                project_id=project_id,
                rule_kind=row.rule_kind,
                limit=config.examples_per_classification,
            )
            if not examples:
                continue
            primary_text, *recent = examples
            report.candidates_examined += 1

            xval = await _classify_with_xval(classify_fn, primary_text=primary_text, examples=recent)
            if not xval.stable or xval.result is None:
                report.decisions.append(
                    DecisionEntry(
                        rule_kind=row.rule_kind,
                        decision=Decision.HOLD.value,
                        reason=f"unstable_classifier:{','.join(xval.diverged_fields) or 'unknown'}",
                    )
                )
                continue

            features = FeatureSet.from_dict(xval.result)
            outcome: DecisionResult = decide(
                features,
                SignalAggregate(weighted_count=row.weighted_count, distinct_subjects=row.distinct_subjects),
                config.decision_config,
            )
            entry = DecisionEntry(
                rule_kind=row.rule_kind,
                decision=outcome.decision.value,
                reason=outcome.reason,
                score=outcome.score,
                classification=dict(xval.result),
            )
            if outcome.decision in (Decision.APPLY, Decision.MERGE):
                rule = _build_rule(
                    row.rule_kind,
                    xval.result,
                    run_id=run_id,
                    schema_v=schema_version,
                    example=primary_text,
                    detector_type=features.detector_type,
                )
                try:
                    rules_store.add_rule(workdir, rule)
                    entry.rule_id = rule.id
                except ValueError as exc:
                    entry.reason = f"{outcome.reason};add_rule_failed:{exc}"
                    entry.decision = Decision.HOLD.value
            elif outcome.decision in (Decision.REVISE, Decision.HOLD):
                candidates_store.add_pending(
                    workdir,
                    candidates_store.Candidate(
                        rule_kind=row.rule_kind,
                        decision=outcome.decision.value,
                        reason=outcome.reason,
                        score=outcome.score,
                        classification=dict(xval.result),
                        examples=examples,
                    ),
                )
            else:
                candidates_store.add_rejected(
                    workdir,
                    candidates_store.Candidate(
                        rule_kind=row.rule_kind,
                        decision=outcome.decision.value,
                        reason=outcome.reason,
                        score=outcome.score,
                        classification=dict(xval.result),
                        examples=examples,
                    ),
                )

            report.decisions.append(entry)

        report.status = "ok"
    except Exception as exc:
        logger.exception("lint_evolution.level1: run failed: %s", exc)
        report.status = "error"
        report.errors.append(str(exc))
    finally:
        report.finished_ts = time.time()
        write_report(workdir, report)
        try:
            applied_count = sum(
                1
                for d in report.decisions
                if d.decision in (Decision.APPLY.value, Decision.MERGE.value)
            )
            if run_id is None:
                fingerprints.record_run(
                    workdir,
                    project_id=project_id,
                    level=1,
                    started_ts=started,
                    finished_ts=report.finished_ts,
                    status=report.status,
                    candidates_count=report.candidates_examined,
                    applied_count=applied_count,
                )
            else:
                fingerprints.finish_run(
                    workdir,
                    run_id,
                    finished_ts=report.finished_ts,
                    status=report.status,
                    candidates_count=report.candidates_examined,
                    applied_count=applied_count,
                )
        except Exception as exc:
            logger.warning("lint_evolution.level1: cannot record run row: %s", exc)
    return report
