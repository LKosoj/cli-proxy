from __future__ import annotations

from typing import Any, Dict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def _schema_error_context(contract: str) -> str:
    return f"agent.{contract}"


AgentIntentInputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["user_text"],
    "properties": {
        "user_text": {"type": "string"},
        "previous_goal": {"type": "string"},
        "previous_actions": {"type": "array", "items": {"type": "string"}},
        "selected_template": {"type": "string"},
        "project_root": {"type": "string"},
        "workdir": {"type": "string"},
    },
    "additionalProperties": True,
}

AgentIntentOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["task_kind_hint", "goal", "actions"],
    "properties": {
        "task_kind_hint": {"type": "string", "enum": ["new_task", "continue_task"]},
        "goal": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "previous_goal": {"type": "string"},
        "previous_actions": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

AgentResearchInputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string"},
        "max_results_per_lang": {"type": "integer", "minimum": 1, "maximum": 20},
        "analyze_content": {"type": "boolean"},
    },
    "additionalProperties": True,
}

AgentResearchOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["query", "summary", "sources"],
    "properties": {
        "query": {"type": "string"},
        "summary": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}


def validate_agent_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, contract: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(f"{_schema_error_context(contract)} schema validation failed{where}: {exc.message}") from exc
