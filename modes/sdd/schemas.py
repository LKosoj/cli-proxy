from __future__ import annotations

from typing import Any, Dict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def _schema_error_context(contract: str) -> str:
    return f"sdd.{contract}"


_RequirementSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["id", "text"],
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
    },
    "additionalProperties": True,
}

_AcceptanceCriterionSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["req_id", "ears"],
    "properties": {
        "req_id": {"type": "string"},
        "ears": {"type": "string"},
    },
    "additionalProperties": True,
}

SPEC_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["feature_slug", "stories", "requirements", "acceptance_criteria"],
    "properties": {
        "feature_slug": {"type": "string"},
        "stories": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": _RequirementSchema},
        "acceptance_criteria": {"type": "array", "items": _AcceptanceCriterionSchema},
    },
    "additionalProperties": True,
}

PLAN_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["architecture", "stack", "constraints", "risks"],
    "properties": {
        "architecture": {"type": "string"},
        "stack": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

_SddTaskSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["id", "title", "description", "acceptance_criteria"],
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "covers_requirements": {"type": "array", "items": {"type": "string"}},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

TASKS_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["project_goal", "tasks"],
    "properties": {
        "project_goal": {"type": "string"},
        "tasks": {"type": "array", "minItems": 1, "items": _SddTaskSchema},
    },
    "additionalProperties": True,
}


def validate_sdd_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, contract: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(
            f"{_schema_error_context(contract)} schema validation failed{where}: {exc.message}"
        ) from exc


__all__ = [
    "SPEC_OUTPUT_SCHEMA",
    "PLAN_OUTPUT_SCHEMA",
    "TASKS_OUTPUT_SCHEMA",
    "validate_sdd_payload",
]
