from __future__ import annotations

import pytest

from modes.webmaster.schemas import (
    WebmasterInputSchema,
    WebmasterIntentOutputSchema,
    WebmasterOutputSchema,
    validate_webmaster_payload,
)


def test_webmaster_input_schema_rejects_missing_required_fields() -> None:
    with pytest.raises(ValueError):
        validate_webmaster_payload({"goal": "x"}, WebmasterInputSchema, contract="input")


def test_webmaster_output_schema_accepts_required_fields() -> None:
    payload = {
        "status": "ok",
        "summary": "validated",
        "blocking_issues": [],
        "checklist_rows": [
            {"item": "i1", "status": "done", "how": "h", "why_not": ""},
        ],
        "defects": [
            {"severity": "low", "title": "t", "location": "l", "why": "w", "fix_hint": "f"},
        ],
        "assumptions": [],
        "blockers": [],
    }
    validate_webmaster_payload(payload, WebmasterOutputSchema, contract="output")


def test_webmaster_intent_output_schema_accepts_required_fields() -> None:
    payload = {
        "goal": "Обновить лендинг",
        "actions": ["Обновить hero", "Исправить CTA"],
        "constraints": ["Не менять бекенд"],
        "acceptance_criteria": ["CTA кликабелен"],
        "ambiguities": [],
        "assumptions": [],
    }
    validate_webmaster_payload(payload, WebmasterIntentOutputSchema, contract="intent_output")


def test_webmaster_intent_output_schema_rejects_wrong_types() -> None:
    bad_payload = {
        "goal": 123,
        "actions": "single action",
        "constraints": [],
        "acceptance_criteria": [],
    }
    with pytest.raises(ValueError, match=r"webmaster\.intent_output schema validation failed"):
        validate_webmaster_payload(bad_payload, WebmasterIntentOutputSchema, contract="intent_output")


def test_webmaster_schema_validation_error_context_is_unified() -> None:
    with pytest.raises(ValueError, match=r"webmaster\.output schema validation failed"):
        validate_webmaster_payload({"status": "ok"}, WebmasterOutputSchema, contract="output")
