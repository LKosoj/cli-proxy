from __future__ import annotations

import pytest

from app.services.lint_evolution.temporal_xval import classify_stable


@pytest.mark.asyncio
async def test_stable_when_critical_fields_match() -> None:
    payloads = [
        {"rule_kind": "x", "category": "correctness", "false_positive_risk": "low", "scope_subjectivity": "objective", "extra": "a"},
        {"rule_kind": "x", "category": "correctness", "false_positive_risk": "low", "scope_subjectivity": "objective", "extra": "b"},
    ]
    it = iter(payloads)

    async def fn():
        return next(it)

    res = await classify_stable(fn)
    assert res.stable is True
    assert res.result == payloads[0]
    assert res.diverged_fields == ()


@pytest.mark.asyncio
async def test_unstable_when_critical_field_diverges() -> None:
    payloads = [
        {"rule_kind": "x", "category": "correctness", "false_positive_risk": "low", "scope_subjectivity": "objective"},
        {"rule_kind": "x", "category": "correctness", "false_positive_risk": "high", "scope_subjectivity": "objective"},
    ]
    it = iter(payloads)

    async def fn():
        return next(it)

    res = await classify_stable(fn)
    assert res.stable is False
    assert res.result is None
    assert "false_positive_risk" in res.diverged_fields


@pytest.mark.asyncio
async def test_unstable_when_no_response() -> None:
    async def fn():
        return None

    res = await classify_stable(fn)
    assert res.stable is False
    assert res.diverged_fields == ("__no_response__",)
