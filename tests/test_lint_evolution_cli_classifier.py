from __future__ import annotations

import json

import pytest

from app.services.lint_evolution.cli_classifier import CliClassifier, build_prompt, extract_json
from app.services.lint_evolution.schemas import classification_schema_v1


_VALID_PAYLOAD = {
    "rule_kind": "unused_imports",
    "category": "correctness",
    "false_positive_risk": "low",
    "detector_type": "regex",
    "scope_subjectivity": "objective",
    "duplicate_of_active": None,
    "extends_active": None,
    "test_generatable": True,
    "confidence": 0.9,
    "notes": "",
}


def test_extract_json_from_fenced_block() -> None:
    text = '```json\n{"a": 1}\n```'
    assert extract_json(text) == {"a": 1}


def test_extract_json_from_raw_object() -> None:
    text = 'preamble {"x": 2} suffix'
    assert extract_json(text) == {"x": 2}


def test_extract_json_returns_none_on_garbage() -> None:
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_build_prompt_has_conservative_defaults_block() -> None:
    p = build_prompt(signal_text="some signal", schema={"x": 1})
    assert "CONSERVATIVE DEFAULTS" in p
    assert "lint-rule CLASSIFIER" in p
    assert "DOES NOT recommend" not in p  # we say DO NOT, not DOES NOT — check exact text
    assert "DO NOT recommend" in p


def test_build_prompt_includes_schema_and_signal() -> None:
    p = build_prompt(signal_text="payload-xyz", schema={"k": "v"})
    assert "payload-xyz" in p
    assert '"k": "v"' in p


@pytest.mark.asyncio
async def test_classify_returns_payload_on_valid_response() -> None:
    schema = classification_schema_v1()

    async def invoke(prompt: str) -> str:
        return json.dumps(_VALID_PAYLOAD)

    cls = CliClassifier(invoke=invoke, schema=schema)
    result = await cls.classify("Убраны неиспользуемые импорты")
    assert result == _VALID_PAYLOAD


@pytest.mark.asyncio
async def test_classify_returns_none_on_schema_violation() -> None:
    schema = classification_schema_v1()
    bad = dict(_VALID_PAYLOAD, false_positive_risk="EXTREME")

    async def invoke(prompt: str) -> str:
        return json.dumps(bad)

    cls = CliClassifier(invoke=invoke, schema=schema)
    assert await cls.classify("text") is None


@pytest.mark.asyncio
async def test_classify_returns_none_on_invoke_exception() -> None:
    schema = classification_schema_v1()

    async def invoke(prompt: str) -> str:
        raise RuntimeError("CLI exploded")

    cls = CliClassifier(invoke=invoke, schema=schema)
    assert await cls.classify("text") is None


@pytest.mark.asyncio
async def test_classify_returns_none_when_no_json() -> None:
    schema = classification_schema_v1()

    async def invoke(prompt: str) -> str:
        return "I am not JSON"

    cls = CliClassifier(invoke=invoke, schema=schema)
    assert await cls.classify("text") is None
