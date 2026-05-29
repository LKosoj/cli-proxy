from __future__ import annotations

from app.services.lint_evolution.schema_decision import (
    EmergentField,
    SchemaDecision,
    SchemaDecisionConfig,
    SchemaDecisionContext,
    decide_schema,
    score_emergent,
)


_WEIGHTS = {
    "examples_count_norm": 1.0,
    "distinct_cases_norm": 1.0,
    "proposed_type_bool": 0.5,
}


def _good_field(**overrides) -> EmergentField:
    base = dict(
        proposed_name="environment_specific",
        proposed_type="bool",
        examples_count=10,
        distinct_cases=5,
        sample_notes=("a", "b", "c"),
        rationale_extracted="emerged",
        covered_by_existing_field=None,
        proposed_values=(),
    )
    base.update(overrides)
    return EmergentField(**base)


def _ctx(**overrides) -> SchemaDecisionContext:
    base = dict(days_since_last_bump=60.0, pending_proposals=0, proposal_was_rejected=False)
    base.update(overrides)
    return SchemaDecisionContext(**base)


def _config(**overrides) -> SchemaDecisionConfig:
    cfg = SchemaDecisionConfig(weights=dict(_WEIGHTS))
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_extend_when_strong_field() -> None:
    res = decide_schema(_good_field(), _ctx(), _config())
    assert res.decision is SchemaDecision.EXTEND_SCHEMA


def test_reject_when_examples_below_min() -> None:
    res = decide_schema(_good_field(examples_count=3), _ctx(), _config())
    assert res.decision is SchemaDecision.REJECT


def test_reject_when_covered_by_existing_field() -> None:
    res = decide_schema(_good_field(covered_by_existing_field="rule_kind"), _ctx(), _config())
    assert res.decision is SchemaDecision.REJECT


def test_hold_for_unsupported_type() -> None:
    res = decide_schema(_good_field(proposed_type="string"), _ctx(), _config())
    assert res.decision is SchemaDecision.HOLD


def test_reject_for_invalid_name() -> None:
    res = decide_schema(_good_field(proposed_name="bad-name"), _ctx(), _config())
    assert res.decision is SchemaDecision.REJECT


def test_hold_for_enum_without_values() -> None:
    res = decide_schema(_good_field(proposed_type="enum", proposed_values=()), _ctx(), _config())
    assert res.decision is SchemaDecision.HOLD


def test_defer_when_inside_bump_cooldown() -> None:
    res = decide_schema(_good_field(), _ctx(days_since_last_bump=10.0), _config(min_bump_interval_days=30))
    assert res.decision is SchemaDecision.DEFER


def test_defer_when_pending_at_max() -> None:
    res = decide_schema(_good_field(), _ctx(pending_proposals=1), _config(max_pending=1))
    assert res.decision is SchemaDecision.DEFER


def test_reject_when_proposal_was_rejected_recently() -> None:
    res = decide_schema(_good_field(), _ctx(proposal_was_rejected=True), _config())
    assert res.decision is SchemaDecision.REJECT


def test_propose_when_score_in_propose_band() -> None:
    res = decide_schema(_good_field(examples_count=5, distinct_cases=3, proposed_type="number"), _ctx(), _config())
    # score = 0.5 (examples) + 0.6 (distinct) = 1.1 → propose
    assert res.decision is SchemaDecision.PROPOSE


def test_score_examples_normalization() -> None:
    f = _good_field(examples_count=20, distinct_cases=10)
    assert score_emergent(f, _WEIGHTS) == 1.0 + 1.0 + 0.5  # both capped, plus bool bonus
