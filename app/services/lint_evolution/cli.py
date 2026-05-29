"""Standalone CLI for lint_evolution: status / ingest / run-l3 / autopause / schema-history.

No bot or LLM dependencies — operates directly on the workdir's .cli-proxy/lint_evolution/
artifact tree. L1/L2 require a classifier callable and are not exposed here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import (
    autopause,
    fingerprints,
    rules_store,
    schema_store,
    signals_ingestor,
    state as state_store,
    weights_regressor,
    weights_store,
)
from .canary_metric import CanaryConfig, evaluate as evaluate_canary
from .paths import lint_root, project_id_for
from .weights_regressor import L3Config


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_status(workdir: str) -> int:
    pid = project_id_for(workdir)
    state = state_store.load_state(workdir)
    project = state.projects.get(pid)
    payload: dict = {
        "workdir": str(workdir),
        "project_id": pid,
        "lint_root": str(lint_root(workdir)),
        "autopause": {k: {"paused": v.paused, "reason": v.reason, "ts": v.ts} for k, v in autopause.status(workdir).items()},
    }
    if project:
        payload["levels"] = {
            "level1": _level_dict(project.level1),
            "level2": _level_dict(project.level2),
            "level3": _level_dict(project.level3),
        }
    payload["rules_active"] = sum(1 for r in rules_store.load_rules(workdir) if r.state == "active")
    payload["schema_version"] = schema_store.load_state(workdir).active_version
    payload["weights_history"] = weights_store.history_count(workdir)
    _print_json(payload)
    return 0


def _level_dict(lvl) -> dict:
    return {
        "last_run_ts": lvl.last_run_ts,
        "last_success_ts": lvl.last_success_ts,
        "last_error_ts": lvl.last_error_ts,
        "consecutive_failures": lvl.consecutive_failures,
        "lock_owner": lvl.lock_owner,
    }


_DEFAULT_GLOBS = (
    ".cli-proxy/.manager/response/*_manager_review_result_*.md",
    ".cli-proxy/.manager/response/*_agent_review_response_*.md",
)


def cmd_ingest(workdir: str, project_root: str) -> int:
    pid = project_id_for(workdir)
    signals, stats = signals_ingestor.collect_signals(
        project_id=pid,
        project_root=Path(project_root),
        glob_patterns=_DEFAULT_GLOBS,
    )
    inserted = fingerprints.insert_signals(workdir, signals)
    _print_json({
        "files_seen": stats.files_seen,
        "files_skipped": stats.files_skipped,
        "signals_emitted": stats.signals_emitted,
        "inserted": inserted,
    })
    return 0


def cmd_run_l3(workdir: str) -> int:
    pid = project_id_for(workdir)
    res = weights_regressor.run_level3(workdir=workdir, project_id=pid, config=L3Config())
    _print_json({
        "status": res.status.value,
        "outcomes_total": res.outcomes_total,
        "fp_rate_global": res.fp_rate_global,
        "notes": res.notes,
    })
    return 0


def cmd_autopause_resume(workdir: str, level: int) -> int:
    ok = autopause.resume(workdir, level)
    _print_json({"level": level, "resumed": ok})
    return 0 if ok else 1


def cmd_autopause_status(workdir: str) -> int:
    _print_json({k: {"paused": v.paused, "reason": v.reason, "ts": v.ts} for k, v in autopause.status(workdir).items()})
    return 0


def cmd_canary(workdir: str) -> int:
    pid = project_id_for(workdir)
    rep = evaluate_canary(workdir, project_id=pid, config=CanaryConfig())
    _print_json({
        "fp_rolling": rep.fp_rolling,
        "fp_baseline": rep.fp_baseline,
        "fp_growth_pct": rep.fp_growth_pct,
        "schema_growth_180d": rep.schema_growth_180d,
        "triggered": list(rep.triggered),
    })
    return 0


def cmd_schema_history(workdir: str) -> int:
    state = schema_store.load_state(workdir)
    proposals = schema_store.load_proposals(workdir)
    deprecated = schema_store.load_deprecated(workdir)
    _print_json({
        "active_version": state.active_version,
        "last_bump_ts": state.last_bump_ts,
        "fields_active": schema_store.existing_field_names(workdir),
        "proposals_pending": len(proposals),
        "deprecated_count": len(deprecated),
    })
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lint_evolution")
    parser.add_argument("--workdir", default=".", help="project workdir (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="print state and counts")
    p_ingest = sub.add_parser("ingest", help="collect signals from manager review files")
    p_ingest.add_argument("--project-root", default=".", help="project_root for review files (default: workdir)")
    sub.add_parser("run-l3", help="run level3 weights regression once")
    p_resume = sub.add_parser("autopause-resume", help="resume a paused level")
    p_resume.add_argument("--level", type=int, required=True, choices=[1, 2, 3])
    sub.add_parser("autopause-status", help="show autopause flags")
    sub.add_parser("canary", help="evaluate canary metric (may set autopause)")
    sub.add_parser("schema-history", help="schema versions and proposals/deprecated counts")

    args = parser.parse_args(argv)
    workdir = str(args.workdir)
    if args.cmd == "status":
        return cmd_status(workdir)
    if args.cmd == "ingest":
        return cmd_ingest(workdir, str(args.project_root))
    if args.cmd == "run-l3":
        return cmd_run_l3(workdir)
    if args.cmd == "autopause-resume":
        return cmd_autopause_resume(workdir, int(args.level))
    if args.cmd == "autopause-status":
        return cmd_autopause_status(workdir)
    if args.cmd == "canary":
        return cmd_canary(workdir)
    if args.cmd == "schema-history":
        return cmd_schema_history(workdir)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
