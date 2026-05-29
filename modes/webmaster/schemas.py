from __future__ import annotations

from typing import Any, Dict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def _schema_error_context(contract: str) -> str:
    return f"webmaster.{contract}"


_ChecklistRowSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["item", "status", "how", "why_not"],
    "properties": {
        "item": {"type": "string"},
        "status": {"type": "string"},
        "how": {"type": "string"},
        "why_not": {"type": "string"},
    },
    "additionalProperties": True,
}

_DefectSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["severity", "title", "location", "why", "fix_hint"],
    "properties": {
        "severity": {"type": "string"},
        "title": {"type": "string"},
        "location": {"type": "string"},
        "why": {"type": "string"},
        "fix_hint": {"type": "string"},
    },
    "additionalProperties": True,
}

_ValidationChecklistResultSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["item", "status"],
    "properties": {
        "item": {"type": "string"},
        "status": {"type": "string", "enum": ["PASS", "PARTIAL", "FAIL"]},
        "evidence": {"type": "string", "default": ""},
        "fixed": {"type": "string", "default": ""},
        "why_not_done": {"type": "string", "default": ""},
    },
    "additionalProperties": True,
}

WebmasterInputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["goal", "actions", "constraints", "acceptance_criteria"],
    "properties": {
        "goal": {"type": "string"},
        "task_kind": {"type": "string", "enum": ["new_task", "continue_task"]},
        "actions": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

WebmasterIntentOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["goal", "actions", "constraints", "acceptance_criteria"],
    "properties": {
        "goal": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}, "default": []},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "default": []},
        "ambiguities": {"type": "array", "items": {"type": "string"}, "default": []},
        "assumptions": {"type": "array", "items": {"type": "string"}, "default": []},
    },
    "additionalProperties": True,
}

WebmasterOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary", "blocking_issues", "checklist_rows", "defects"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "needs_revision", "blocked", "failed"]},
        "summary": {"type": "string"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
        "checklist_rows": {"type": "array", "items": _ChecklistRowSchema},
        "defects": {"type": "array", "items": _DefectSchema},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

WebmasterValidationReportSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary", "blocking_issues", "checklist_results", "defects"],
    "properties": {
        "status": {"type": "string", "enum": ["PASS", "PARTIAL", "FAIL"]},
        "summary": {"type": "string"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}, "default": []},
        "checklist_results": {"type": "array", "items": _ValidationChecklistResultSchema, "default": []},
        "defects": {"type": "array", "items": _DefectSchema, "default": []},
    },
    "additionalProperties": True,
}


def validate_webmaster_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, contract: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(f"{_schema_error_context(contract)} schema validation failed{where}: {exc.message}") from exc
