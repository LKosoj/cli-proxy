from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import candidates_store, fingerprints, schema_store
from .reports import DecisionEntry, RunReport, write_report
from .schema_decision import (
    EmergentField,
    SchemaDecision,
    SchemaDecisionConfig,
    SchemaDecisionContext,
    decide_schema,
)

logger = logging.getLogger(__name__)

MetaClassifyFn = Callable[[list[str], list[str]], Awaitable[list[dict[str, Any]] | None]]


@dataclass
class L2Config:
    decision_config: SchemaDecisionConfig = field(default_factory=SchemaDecisionConfig)
    notes_per_run: int = 30


def _gather_notes(workdir: str, *, max_notes: int) -> list[str]:
    notes: list[str] = []
    for c in candidates_store.load_rejected(workdir):
        n = (c.classification or {}).get("notes")
        if n:
            notes.append(str(n).strip())
        if len(notes) >= max_notes:
            return notes
    for c in candidates_store.load_pending(workdir):
        n = (c.classification or {}).get("notes")
        if n:
            notes.append(str(n).strip())
        if len(notes) >= max_notes:
            return notes
    return notes


def _proposal_was_rejected(workdir: str, name: str) -> bool:
    for prev in schema_store.load_proposals(workdir):
        if prev.get("decision") == SchemaDecision.REJECT.value and prev.get("proposed_name") == name:
            return True
    return False


def _count_pending_proposals(workdir: str) -> int:
    return sum(1 for p in schema_store.load_proposals(workdir) if p.get("decision") == SchemaDecision.PROPOSE.value)


async def run_level2(
    *,
    workdir: str,
    project_id: str,
    project_root: Path,  # noqa: ARG001 (unused but kept for symmetry)
    meta_classify_fn: MetaClassifyFn,
    config: L2Config,
) -> RunReport:
    schema_store.bootstrap_schema(workdir)
    started = time.time()
    report = RunReport(project_id=project_id, level=2, started_ts=started, status="running")
    run_id: int | None = None

    try:
        run_id = fingerprints.record_run(
            workdir,
            project_id=project_id,
            level=2,
            started_ts=started,
            finished_ts=None,
            status="running",
        )
        notes = _gather_notes(workdir, max_notes=config.notes_per_run)
        if not notes:
            report.status = "ok"
            report.errors.append("no_notes_to_meta_classify")
            return report

        existing = schema_store.existing_field_names(workdir)
        emergent = await meta_classify_fn(notes, existing)
        if emergent is None:
            report.status = "error"
            report.errors.append("meta_classify_failed_or_unstable")
            return report

        report.candidates_examined = len(emergent)
        days = schema_store.days_since_last_bump(workdir, now=started)
        pending = _count_pending_proposals(workdir)
        existing_set = set(existing)

        for raw in emergent:
            field = EmergentField.from_dict(raw)
            if field.proposed_name in existing_set:
                report.decisions.append(
                    DecisionEntry(
                        rule_kind="schema",
                        decision=SchemaDecision.REJECT.value,
                        reason="name_exists_in_active_schema",
                    )
                )
                continue
            ctx = SchemaDecisionContext(
                days_since_last_bump=days,
                pending_proposals=pending,
                proposal_was_rejected=_proposal_was_rejected(workdir, field.proposed_name),
            )
            outcome = decide_schema(field, ctx, config.decision_config)
            entry = DecisionEntry(
                rule_kind="schema",
                decision=outcome.decision.value,
                reason=outcome.reason,
                score=outcome.score,
                classification={"proposed_name": field.proposed_name, "proposed_type": field.proposed_type},
            )
            if outcome.decision is SchemaDecision.EXTEND_SCHEMA:
                spec = schema_store.FieldSpec(
                    name=field.proposed_name,
                    type=field.proposed_type,
                    values=list(field.proposed_values),
                    rationale=field.rationale_extracted,
                )
                try:
                    new_version = schema_store.extend_schema(workdir, spec, reason=field.rationale_extracted)
                    spec.added_in_version = new_version
                    entry.classification["new_version"] = new_version
                    days = schema_store.days_since_last_bump(workdir, now=time.time())
                    pending = _count_pending_proposals(workdir)
                    existing_set.add(field.proposed_name)
                except ValueError as exc:
                    entry.decision = SchemaDecision.HOLD.value
                    entry.reason = f"{outcome.reason};extend_failed:{exc}"

            schema_store.append_proposal(
                workdir,
                {
                    "proposed_name": field.proposed_name,
                    "proposed_type": field.proposed_type,
                    "decision": entry.decision,
                    "score": outcome.score,
                    "reason": outcome.reason,
                    "ts": time.time(),
                },
            )
            if entry.decision == SchemaDecision.PROPOSE.value:
                pending += 1

            report.decisions.append(entry)

        report.status = "ok"
    except Exception as exc:
        logger.exception("lint_evolution.level2: run failed: %s", exc)
        report.status = "error"
        report.errors.append(str(exc))
    finally:
        report.finished_ts = time.time()
        write_report(workdir, report)
        try:
            applied_count = sum(1 for d in report.decisions if d.decision == SchemaDecision.EXTEND_SCHEMA.value)
            if run_id is None:
                fingerprints.record_run(
                    workdir,
                    project_id=project_id,
                    level=2,
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
            logger.warning("lint_evolution.level2: cannot record run row: %s", exc)
    return report
