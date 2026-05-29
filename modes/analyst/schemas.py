from __future__ import annotations

from typing import Any, Dict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def _schema_error_context(contract: str) -> str:
    return f"analyst.{contract}"


AnalystIntentInputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["user_text"],
    "properties": {
        "user_text": {"type": "string"},
        "clarification_answers": {"type": "array", "items": {"type": "string"}},
        "selected_template": {"type": "string"},
        "project_root": {"type": "string"},
        "workdir": {"type": "string"},
        "has_codebase_map": {"type": "boolean"},
    },
    "additionalProperties": True,
}

AnalystIntentOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["analysis_profile", "document_kind", "detail_level", "summary"],
    "properties": {
        "analysis_profile": {
            "type": "string",
            "enum": [
                "greenfield",
                "codebase",
                "codebase_plus_research",
                "research_only",
                "audit",
            ],
        },
        "document_kind": {
            "type": "string",
            "enum": ["spec", "analysis", "audit"],
        },
        "detail_level": {
            "type": "string",
            "enum": ["brief", "standard", "full"],
        },
        "summary": {
            "type": "string",
            "description": "Краткое понимание задачи в 1-2 предложениях",
        },
        "clarification_questions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
        "template_hint": {
            "type": "string",
        },
    },
    "additionalProperties": False,
}


AnalystResearchInputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["goal"],
    "properties": {
        "goal": {"type": "string"},
        "template_id": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "project_root": {"type": "string"},
        "workdir": {"type": "string"},
    },
    "additionalProperties": True,
}

AnalystResearchOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["summary", "findings", "recommendations"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}


def validate_analyst_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, contract: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(f"{_schema_error_context(contract)} schema validation failed{where}: {exc.message}") from exc
