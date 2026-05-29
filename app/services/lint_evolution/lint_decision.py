from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    APPLY = "apply"
    MERGE = "merge"
    REVISE = "revise"
    HOLD = "hold"
    REJECT = "reject"


@dataclass(frozen=True)
class FeatureSet:
    rule_kind: str
    category: str
    false_positive_risk: str
    detector_type: str
    scope_subjectivity: str
    duplicate_of_active: str | None = None
    extends_active: str | None = None
    test_generatable: bool = False
    confidence: float = 0.0
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeatureSet":
        return cls(
            rule_kind=str(data.get("rule_kind") or "__unknown__"),
            category=str(data.get("category") or "correctness"),
            false_positive_risk=str(data.get("false_positive_risk") or "high"),
            detector_type=str(data.get("detector_type") or "llm_only"),
            scope_subjectivity=str(data.get("scope_subjectivity") or "subjective"),
            duplicate_of_active=(data.get("duplicate_of_active") or None),
            extends_active=(data.get("extends_active") or None),
            test_generatable=bool(data.get("test_generatable") or False),
            confidence=float(data.get("confidence") or 0.0),
            notes=str(data.get("notes") or ""),
        )


@dataclass(frozen=True)
class SignalAggregate:
    weighted_count: float
    distinct_subjects: int


@dataclass
class DecisionConfig:
    min_distinct_subjects: int = 3
    min_weighted_count: float = 5.0
    apply_threshold: float = 2.5
    revise_threshold: float = 1.0
    merge_threshold: float = 1.5
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    reason: str
    score: float


def _w(weights: dict[str, float], key: str) -> float:
    return float(weights.get(key, 0.0))


def score_features(features: FeatureSet, weights: dict[str, float]) -> float:
    score = 0.0
    score += _w(weights, f"category_{features.category}")
    score += _w(weights, f"detector_{features.detector_type}")
    score += _w(weights, f"fp_risk_{features.false_positive_risk}")
    score += _w(weights, f"scope_{features.scope_subjectivity}")
    score += _w(weights, "confidence_multiplier") * features.confidence
    return score


def decide(features: FeatureSet, signals: SignalAggregate, config: DecisionConfig) -> DecisionResult:
    if features.duplicate_of_active:
        return DecisionResult(Decision.REJECT, f"duplicate_of:{features.duplicate_of_active}", 0.0)
    if features.detector_type == "llm_only":
        return DecisionResult(Decision.HOLD, "detector_type:llm_only", 0.0)
    if features.scope_subjectivity == "subjective":
        return DecisionResult(Decision.REJECT, "subjective_scope", 0.0)
    if features.category == "style":
        return DecisionResult(Decision.REJECT, "style_category_handled_by_ruff", 0.0)
    if not features.test_generatable:
        return DecisionResult(Decision.HOLD, "no_tests_generatable", 0.0)
    if signals.distinct_subjects < config.min_distinct_subjects:
        return DecisionResult(Decision.HOLD, f"distinct_subjects<{config.min_distinct_subjects}", 0.0)
    if signals.weighted_count < config.min_weighted_count:
        return DecisionResult(Decision.HOLD, f"weighted_count<{config.min_weighted_count}", 0.0)

    score = score_features(features, config.weights)

    if features.extends_active and score >= config.merge_threshold:
        return DecisionResult(Decision.MERGE, f"extends:{features.extends_active}", score)
    if score >= config.apply_threshold:
        return DecisionResult(Decision.APPLY, "score_meets_apply", score)
    if score >= config.revise_threshold:
        return DecisionResult(Decision.REVISE, "score_meets_revise", score)
    return DecisionResult(Decision.REJECT, "score_below_revise", score)
