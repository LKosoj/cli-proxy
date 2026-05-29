from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ensure_directories, rules_dir

logger = logging.getLogger(__name__)


_ALLOWED_DETECTOR_TYPES: frozenset[str] = frozenset({"regex", "ast", "shell"})
_ALLOWED_STATES: frozenset[str] = frozenset({"active", "demoted", "retired"})


@dataclass
class DetectorPayload:
    pattern: str = ""
    target_glob: str = "**/*.py"
    ast_check: str = ""
    shell: str = ""


@dataclass
class RuleMetadata:
    added_ts: float = 0.0
    added_run_id: int = 0
    schema_v: int = 1
    example_signal: str = ""
    classification: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleMetrics:
    hits: int = 0
    fp_count: int = 0
    tp_count: int = 0


@dataclass
class Rule:
    id: str
    rule_kind: str
    detector_type: str
    detector_payload: DetectorPayload = field(default_factory=DetectorPayload)
    metadata: RuleMetadata = field(default_factory=RuleMetadata)
    metrics: RuleMetrics = field(default_factory=RuleMetrics)
    state: str = "active"


def make_rule_id(rule_kind: str, subject_hash: str) -> str:
    return f"{rule_kind}-{(subject_hash or '')[:8] or 'na'}"


def _rules_file(workdir: str) -> Path:
    ensure_directories(workdir)
    return rules_dir(workdir) / "self.yaml"


def _to_rule(payload: dict[str, Any]) -> Rule:
    detector = (payload or {}).get("detector_payload") or {}
    metadata = (payload or {}).get("metadata") or {}
    metrics = (payload or {}).get("metrics") or {}
    return Rule(
        id=str(payload.get("id") or ""),
        rule_kind=str(payload.get("rule_kind") or "__unknown__"),
        detector_type=str(payload.get("detector_type") or "regex"),
        detector_payload=DetectorPayload(
            pattern=str(detector.get("pattern") or ""),
            target_glob=str(detector.get("target_glob") or "**/*.py"),
            ast_check=str(detector.get("ast_check") or ""),
            shell=str(detector.get("shell") or ""),
        ),
        metadata=RuleMetadata(
            added_ts=float(metadata.get("added_ts") or 0.0),
            added_run_id=int(metadata.get("added_run_id") or 0),
            schema_v=int(metadata.get("schema_v") or 1),
            example_signal=str(metadata.get("example_signal") or ""),
            classification=dict(metadata.get("classification") or {}),
        ),
        metrics=RuleMetrics(
            hits=int(metrics.get("hits") or 0),
            fp_count=int(metrics.get("fp_count") or 0),
            tp_count=int(metrics.get("tp_count") or 0),
        ),
        state=str(payload.get("state") or "active"),
    )


def load_rules(workdir: str) -> list[Rule]:
    path = _rules_file(workdir)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.exception("lint_evolution.rules_store: cannot parse %s: %s", path, exc)
        return []
    raw_rules = (data or {}).get("rules") or []
    return [_to_rule(r) for r in raw_rules if isinstance(r, dict)]


def save_rules(workdir: str, rules: list[Rule]) -> None:
    path = _rules_file(workdir)
    payload = {"rules": [asdict(r) for r in rules]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def find_active_by_kind(rules: list[Rule], rule_kind: str) -> Rule | None:
    for r in rules:
        if r.state == "active" and r.rule_kind == rule_kind:
            return r
    return None


def add_rule(workdir: str, rule: Rule) -> None:
    if rule.detector_type not in _ALLOWED_DETECTOR_TYPES:
        raise ValueError(f"detector_type {rule.detector_type!r} not allowed")
    if rule.state not in _ALLOWED_STATES:
        raise ValueError(f"state {rule.state!r} not allowed")
    if not rule.id:
        raise ValueError("rule.id is required")
    rules = load_rules(workdir)
    if any(r.id == rule.id for r in rules):
        raise ValueError(f"rule {rule.id!r} already exists")
    if not rule.metadata.added_ts:
        rule.metadata.added_ts = time.time()
    rules.append(rule)
    save_rules(workdir, rules)


def update_rule_state(workdir: str, rule_id: str, *, state: str) -> bool:
    if state not in _ALLOWED_STATES:
        raise ValueError(f"state {state!r} not allowed")
    rules = load_rules(workdir)
    changed = False
    for r in rules:
        if r.id == rule_id:
            r.state = state
            changed = True
            break
    if changed:
        save_rules(workdir, rules)
    return changed


def increment_metric(workdir: str, rule_id: str, *, hits: int = 0, fp: int = 0, tp: int = 0) -> bool:
    rules = load_rules(workdir)
    changed = False
    for r in rules:
        if r.id == rule_id:
            r.metrics.hits += int(hits)
            r.metrics.fp_count += int(fp)
            r.metrics.tp_count += int(tp)
            changed = True
            break
    if changed:
        save_rules(workdir, rules)
    return changed
