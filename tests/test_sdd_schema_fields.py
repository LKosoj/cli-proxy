from __future__ import annotations

import pytest

from modes.sdd.schemas import (
    PLAN_OUTPUT_SCHEMA,
    SPEC_OUTPUT_SCHEMA,
    validate_sdd_payload,
)


def _spec(**extra):
    base = {
        "feature_slug": "demo",
        "stories": ["s"],
        "requirements": [{"id": "REQ-1", "text": "t"}],
        "acceptance_criteria": [{"req_id": "REQ-1", "ears": "WHEN x THE SYSTEM SHALL y"}],
    }
    base.update(extra)
    return base


def _plan(**extra):
    base = {"architecture": "a", "stack": ["s"], "constraints": ["c"], "risks": ["r"]}
    base.update(extra)
    return base


def test_spec_out_of_scope_optional() -> None:
    validate_sdd_payload(_spec(), SPEC_OUTPUT_SCHEMA, contract="specify")


def test_spec_out_of_scope_accepted() -> None:
    validate_sdd_payload(_spec(out_of_scope=["nope"]), SPEC_OUTPUT_SCHEMA, contract="specify")


def test_spec_out_of_scope_wrong_type_rejected() -> None:
    with pytest.raises(ValueError):
        validate_sdd_payload(_spec(out_of_scope="nope"), SPEC_OUTPUT_SCHEMA, contract="specify")


def test_plan_affected_modules_optional() -> None:
    validate_sdd_payload(_plan(), PLAN_OUTPUT_SCHEMA, contract="plan")


def test_plan_affected_modules_accepted() -> None:
    validate_sdd_payload(_plan(affected_modules=["modes/sdd"]), PLAN_OUTPUT_SCHEMA, contract="plan")


def test_plan_affected_modules_wrong_type_rejected() -> None:
    with pytest.raises(ValueError):
        validate_sdd_payload(_plan(affected_modules={"a": 1}), PLAN_OUTPUT_SCHEMA, contract="plan")
