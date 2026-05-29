from __future__ import annotations

import pytest

from modes.agent.schemas import (
    AgentIntentInputSchema,
    AgentIntentOutputSchema,
    AgentResearchInputSchema,
    AgentResearchOutputSchema,
    validate_agent_payload,
)


def test_agent_intent_input_schema_requires_user_text() -> None:
    with pytest.raises(ValueError):
        validate_agent_payload({}, AgentIntentInputSchema, contract="intent_input")


def test_agent_intent_output_schema_accepts_contract() -> None:
    payload = {
        "task_kind_hint": "new_task",
        "goal": "Обновить сайт",
        "actions": ["Проверить текущую структуру"],
        "constraints": [],
        "acceptance_criteria": [],
    }
    validate_agent_payload(payload, AgentIntentOutputSchema, contract="intent_output")


def test_agent_research_schemas_validate_input_and_output() -> None:
    validate_agent_payload({"query": "python asyncio"}, AgentResearchInputSchema, contract="research_input")
    validate_agent_payload(
        {
            "query": "python asyncio",
            "summary": "Найдены материалы",
            "sources": ["https://example.com"],
            "findings": ["Использовать TaskGroup"],
        },
        AgentResearchOutputSchema,
        contract="research_output",
    )


def test_agent_schema_validation_error_context_is_unified() -> None:
    with pytest.raises(ValueError, match=r"agent\.intent_output schema validation failed"):
        validate_agent_payload({"goal": "x"}, AgentIntentOutputSchema, contract="intent_output")
