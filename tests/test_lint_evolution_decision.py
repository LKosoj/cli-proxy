from __future__ import annotations

import pytest

from app.services.lint_evolution.lint_decision import (
    Decision,
    DecisionConfig,
    FeatureSet,
    SignalAggregate,
    decide,
    score_features,
)


_DEFAULT_WEIGHTS: dict[str, float] = {
    "category_security": 2.0,
    "category_correctness": 1.5,
    "category_architecture": 1.0,
    "category_style": 0.0,
    "category_performance": 1.0,
    "detector_regex": 1.0,
    "detector_ast": 1.0,
    "detector_shell": 0.5,
    "detector_llm_only": 0.0,
    "fp_risk_high": -2.0,
    "fp_risk_medium": -1.0,
    "fp_risk_low": 0.0,
    "scope_objective": 0.5,
    "scope_context_dependent": -0.5,
    "scope_subjective": -1.5,
    "confidence_multiplier": 0.5,
}


def _good_features(**overrides) -> FeatureSet:
    base = dict(
        rule_kind="tests_failing",
        category="correctness",
        false_positive_risk="low",
        detector_type="regex",
        scope_subjectivity="objective",
        duplicate_of_active=None,
        extends_active=None,
        test_generatable=True,
        confidence=0.9,
        notes="",
    )
    base.update(overrides)
    return FeatureSet(**base)


def _good_signals() -> SignalAggregate:
    return SignalAggregate(weighted_count=15.0, distinct_subjects=5)


def _config(**overrides) -> DecisionConfig:
    cfg = DecisionConfig(weights=dict(_DEFAULT_WEIGHTS))
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_apply_when_all_strong_signals() -> None:
    res = decide(_good_features(), _good_signals(), _config())
    assert res.decision == Decision.APPLY


def test_reject_when_duplicate_of_active() -> None:
    res = decide(_good_features(duplicate_of_active="rule-42"), _good_signals(), _config())
    assert res.decision == Decision.REJECT
    assert "duplicate_of:rule-42" in res.reason


def test_hold_when_detector_llm_only() -> None:
    res = decide(_good_features(detector_type="llm_only"), _good_signals(), _config())
    assert res.decision == Decision.HOLD


def test_reject_when_subjective_scope() -> None:
    res = decide(_good_features(scope_subjectivity="subjective"), _good_signals(), _config())
    assert res.decision == Decision.REJECT


def test_reject_when_style_category() -> None:
    res = decide(_good_features(category="style"), _good_signals(), _config())
    assert res.decision == Decision.REJECT


def test_hold_when_no_test_generatable() -> None:
    res = decide(_good_features(test_generatable=False), _good_signals(), _config())
    assert res.decision == Decision.HOLD


def test_hold_when_distinct_subjects_below_min() -> None:
    res = decide(_good_features(), SignalAggregate(weighted_count=15.0, distinct_subjects=2), _config())
    assert res.decision == Decision.HOLD


def test_hold_when_weighted_count_below_min() -> None:
    res = decide(_good_features(), SignalAggregate(weighted_count=4.0, distinct_subjects=5), _config())
    assert res.decision == Decision.HOLD


def test_revise_when_score_in_middle_band() -> None:
    res = decide(
        _good_features(category="performance", false_positive_risk="medium"),
        _good_signals(),
        _config(),
    )
    assert res.decision == Decision.REVISE


def test_reject_when_score_below_revise() -> None:
    # category=architecture(1.0) + detector_regex(1.0) + fp_risk_high(-2.0) + scope_objective(0.5) + 0.45 = 0.95
    res = decide(
        _good_features(category="architecture", false_positive_risk="high"),
        _good_signals(),
        _config(),
    )
    assert res.decision == Decision.REJECT


def test_merge_when_extends_active_and_score_meets_merge() -> None:
    res = decide(
        _good_features(extends_active="rule-existing"),
        _good_signals(),
        _config(),
    )
    assert res.decision == Decision.MERGE
    assert "extends:rule-existing" in res.reason


def test_score_features_calculation() -> None:
    f = _good_features(category="security", confidence=1.0)
    s = score_features(f, _DEFAULT_WEIGHTS)
    # 2.0 (security) + 1.0 (regex) + 0.0 (low) + 0.5 (objective) + 0.5 * 1.0 = 4.0
    assert s == pytest.approx(4.0)


def test_feature_set_from_dict_safe_defaults() -> None:
    f = FeatureSet.from_dict({})
    assert f.false_positive_risk == "high"
    assert f.scope_subjectivity == "subjective"
    assert f.detector_type == "llm_only"
    assert f.test_generatable is False
