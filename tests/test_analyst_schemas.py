from __future__ import annotations

import pytest

from modes.analyst.schemas import (
    AnalystIntentInputSchema,
    AnalystIntentOutputSchema,
    AnalystResearchInputSchema,
    AnalystResearchOutputSchema,
    validate_analyst_payload,
)


def test_analyst_intent_input_schema_requires_user_text() -> None:
    with pytest.raises(ValueError):
        validate_analyst_payload({}, AnalystIntentInputSchema, contract="intent_input")


def test_analyst_intent_output_schema_accepts_contract() -> None:
    payload = {
        "analysis_profile": "codebase",
        "document_kind": "analysis",
        "detail_level": "standard",
        "summary": "Анализ проекта",
    }
    validate_analyst_payload(payload, AnalystIntentOutputSchema, contract="intent_output")
    assert "analysis_profile" in AnalystIntentOutputSchema["properties"]
    assert "detail_level" in AnalystIntentOutputSchema["properties"]
    assert "document_kind" in AnalystIntentOutputSchema["properties"]
    assert "summary" in AnalystIntentOutputSchema["properties"]


@pytest.mark.parametrize("missing_field", ["analysis_profile", "document_kind", "detail_level", "summary"])
def test_analyst_intent_output_schema_requires_fields(missing_field: str) -> None:
    payload = {
        "analysis_profile": "codebase",
        "document_kind": "analysis",
        "detail_level": "standard",
        "summary": "Анализ проекта",
    }
    payload.pop(missing_field)

    with pytest.raises(ValueError, match=missing_field):
        validate_analyst_payload(payload, AnalystIntentOutputSchema, contract="intent_output")


def test_analyst_research_schemas_validate_input_and_output() -> None:
    validate_analyst_payload(
        {"goal": "Провести анализ архитектуры", "template_id": "project_analysis"},
        AnalystResearchInputSchema,
        contract="research_input",
    )
    validate_analyst_payload(
        {
            "summary": "Подготовлен исследовательский отчёт",
            "findings": ["Монолитный слой доступа к данным"],
            "recommendations": ["Выделить сервисный слой"],
            "sources": ["repo://src"],
        },
        AnalystResearchOutputSchema,
        contract="research_output",
    )


def test_analyst_schema_validation_error_context_is_unified() -> None:
    with pytest.raises(ValueError, match=r"analyst\.research_output schema validation failed"):
        validate_analyst_payload({"summary": "x"}, AnalystResearchOutputSchema, contract="research_output")
