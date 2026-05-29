from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SchemaDecision(str, Enum):
    EXTEND_SCHEMA = "extend_schema"
    PROPOSE = "propose"
    DEFER = "defer"
    HOLD = "hold"
    REJECT = "reject"


@dataclass(frozen=True)
class EmergentField:
    proposed_name: str
    proposed_type: str
    examples_count: int
    distinct_cases: int
    sample_notes: tuple[str, ...] = ()
    rationale_extracted: str = ""
    covered_by_existing_field: str | None = None
    proposed_values: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmergentField":
        values = data.get("proposed_values") or []
        notes = data.get("sample_notes") or []
        return cls(
            proposed_name=str(data.get("proposed_name") or ""),
            proposed_type=str(data.get("proposed_type") or ""),
            examples_count=int(data.get("examples_count") or 0),
            distinct_cases=int(data.get("distinct_cases") or 0),
            sample_notes=tuple(str(n) for n in notes),
            rationale_extracted=str(data.get("rationale_extracted") or ""),
            covered_by_existing_field=(data.get("covered_by_existing_field") or None),
            proposed_values=tuple(str(v) for v in values),
        )


@dataclass
class SchemaDecisionContext:
    days_since_last_bump: float = 1e9
    pending_proposals: int = 0
    proposal_was_rejected: bool = False


@dataclass
class SchemaDecisionConfig:
    min_examples: int = 5
    min_distinct_cases: int = 3
    min_bump_interval_days: int = 30
    max_pending: int = 1
    extend_threshold: float = 1.5
    propose_threshold: float = 0.8
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SchemaDecisionResult:
    decision: SchemaDecision
    reason: str
    score: float = 0.0


def _w(weights: dict[str, float], key: str) -> float:
    return float(weights.get(key, 0.0))


def score_emergent(field: EmergentField, weights: dict[str, float]) -> float:
    examples_norm = min(field.examples_count / 10.0, 1.0)
    distinct_norm = min(field.distinct_cases / 5.0, 1.0)
    score = 0.0
    score += _w(weights, "examples_count_norm") * examples_norm
    score += _w(weights, "distinct_cases_norm") * distinct_norm
    if field.proposed_type == "bool":
        score += _w(weights, "proposed_type_bool")
    return score


def decide_schema(
    field: EmergentField,
    ctx: SchemaDecisionContext,
    config: SchemaDecisionConfig,
) -> SchemaDecisionResult:
    if field.examples_count < config.min_examples:
        return SchemaDecisionResult(SchemaDecision.REJECT, f"examples<{config.min_examples}")
    if field.distinct_cases < config.min_distinct_cases:
        return SchemaDecisionResult(SchemaDecision.REJECT, f"distinct_cases<{config.min_distinct_cases}")
    if field.covered_by_existing_field:
        return SchemaDecisionResult(SchemaDecision.REJECT, f"covered_by:{field.covered_by_existing_field}")
    if field.proposed_type not in {"enum", "bool", "number"}:
        return SchemaDecisionResult(SchemaDecision.HOLD, f"unsupported_type:{field.proposed_type}")
    if not field.proposed_name or not field.proposed_name.isidentifier():
        return SchemaDecisionResult(SchemaDecision.REJECT, f"invalid_name:{field.proposed_name!r}")
    if field.proposed_type == "enum" and not field.proposed_values:
        return SchemaDecisionResult(SchemaDecision.HOLD, "enum_without_values")
    if ctx.proposal_was_rejected:
        return SchemaDecisionResult(SchemaDecision.REJECT, "proposal_was_rejected_recently")
    if ctx.days_since_last_bump < float(config.min_bump_interval_days):
        return SchemaDecisionResult(SchemaDecision.DEFER, f"bump_cooldown<{config.min_bump_interval_days}d")
    if ctx.pending_proposals >= config.max_pending:
        return SchemaDecisionResult(SchemaDecision.DEFER, f"pending_proposals>={config.max_pending}")

    score = score_emergent(field, config.weights)
    if score >= config.extend_threshold:
        return SchemaDecisionResult(SchemaDecision.EXTEND_SCHEMA, "score_meets_extend", score)
    if score >= config.propose_threshold:
        return SchemaDecisionResult(SchemaDecision.PROPOSE, "score_meets_propose", score)
    return SchemaDecisionResult(SchemaDecision.REJECT, "score_below_propose", score)
