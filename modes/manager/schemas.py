from __future__ import annotations

from typing import Any, Dict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def _schema_error_context(contract: str) -> str:
    return f"manager.{contract}"


_ChecklistRowSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["item", "status", "how", "why_not"],
    "properties": {
        "item": {"type": "string"},
        "status": {"type": "string"},
        "how": {"type": "string"},
        "why_not": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}

_TaskSchema: Dict[str, Any] = {
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

ManagerInputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["project_goal", "tasks"],
    "properties": {
        "project_goal": {"type": "string"},
        "project_analysis": {
            "type": "object",
            "properties": {
                "current_state": {"type": "string"},
                "already_done": {"type": "array", "items": {"type": "string"}},
                "remaining_work": {"type": "array", "items": {"type": "string"}},
                "requirements": {"type": "array", "items": {"type": "string"}},
                "checklist_table": {"type": "array", "items": _ChecklistRowSchema},
            },
            "additionalProperties": True,
        },
        "checklist_table": {"type": "array", "items": _ChecklistRowSchema},
        "tasks": {"type": "array", "minItems": 1, "items": _TaskSchema},
    },
    "additionalProperties": True,
}

ManagerOutputSchema: Dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary", "acceptance_criteria_report", "checklist_table", "tests", "lint"],
    "properties": {
        "status": {"type": "string", "enum": ["ok", "blocked", "failed"]},
        "summary": {"type": "string"},
        "acceptance_criteria_report": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["criterion", "status", "evidence"],
                "properties": {
                    "criterion": {"type": "string"},
                    "status": {"type": "string", "enum": ["done", "not_done"]},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "checklist_table": {"type": "array", "items": _ChecklistRowSchema},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["command", "result", "details"],
                "properties": {
                    "command": {"type": "string"},
                    "result": {"type": "string"},
                    "details": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "lint": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["command", "result", "details"],
                "properties": {
                    "command": {"type": "string"},
                    "result": {"type": "string"},
                    "details": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

# Manager runtime/report schemas used by agent.manager flow.
_PLAN_TASK_SCHEMA: Dict[str, Any] = {
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

_CHECKLIST_ROW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "item": {"type": "string"},
        "status": {"type": "string"},
        "how": {"type": "string"},
        "why_not": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}

PLAN_PAYLOAD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "project_goal": {"type": "string"},
        "project_analysis": {
            "type": "object",
            "properties": {
                "current_state": {"type": "string"},
                "already_done": {"type": "array", "items": {"type": "string"}},
                "remaining_work": {"type": "array", "items": {"type": "string"}},
                "requirements": {"type": "array", "items": {"type": "string"}},
                "checklist_table": {"type": "array", "items": _CHECKLIST_ROW_SCHEMA},
            },
            "additionalProperties": True,
        },
        "analysis": {
            "type": "object",
            "properties": {
                "current_state": {"type": "string"},
                "already_done": {"type": "array", "items": {"type": "string"}},
                "remaining_work": {"type": "array", "items": {"type": "string"}},
                "requirements": {"type": "array", "items": {"type": "string"}},
                "checklist_table": {"type": "array", "items": _CHECKLIST_ROW_SCHEMA},
            },
            "additionalProperties": True,
        },
        "checklist_table": {"type": "array", "items": _CHECKLIST_ROW_SCHEMA},
        "tasks": {
            "type": "array",
            "items": _PLAN_TASK_SCHEMA,
            "minItems": 1,
        },
    },
    "additionalProperties": True,
}

PLAN_VALIDATION_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["valid", "issues"],
    "properties": {
        "valid": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

REVIEW_RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["approved", "summary", "comments"],
    "properties": {
        "approved": {"type": "boolean"},
        "summary": {"type": "string"},
        "comments": {"type": "string"},
        "tests_passed": {"type": ["boolean", "null"]},
        "files_reviewed": {"type": "array", "items": {"type": "string"}},
        "not_done_assessment": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "why_not": {"type": "string"},
                    "verdict": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}

WORK_TYPE_CLASSIFIER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["work_type", "confidence", "reason"],
    "properties": {
        "work_type": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "additionalProperties": True,
}

EXECUTOR_RESPONSE_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["summary", "outputs"],
    "properties": {
        "summary": {"type": "string"},
        "outputs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": ["string", "null"]},
                    "content": {},
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}

_FINAL_AUDIT_FIX_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["gap", "changes", "evidence"],
    "properties": {
        "gap": {"type": "string"},
        "changes": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "additionalProperties": True,
}

_FINAL_AUDIT_REQ_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["req_id", "status", "tasks", "evidence"],
    "properties": {
        "req_id": {"type": "string"},
        "status": {"type": "string"},
        "tasks": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "gap": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}

FINAL_SPEC_AUDIT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": [
        "status",
        "summary",
        "gaps_found",
        "fixes_applied",
        "remaining_gaps",
        "tests",
        "lint",
        "requirement_matrix",
    ],
    "properties": {
        "status": {"enum": ["PASS", "GAP_FIXED", "FAIL"]},
        "summary": {"type": "string"},
        "gaps_found": {"type": "array", "items": {"type": "string"}},
        "fixes_applied": {"type": "array", "items": _FINAL_AUDIT_FIX_SCHEMA},
        "remaining_gaps": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array"},
        "lint": {"type": "array"},
        "requirement_matrix": {"type": "array", "items": _FINAL_AUDIT_REQ_SCHEMA},
        "manager_prompt_patch_candidate": {"type": ["object", "null"]},
    },
    "additionalProperties": True,
}


def validate_manager_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, contract: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(f"{_schema_error_context(contract)} schema validation failed{where}: {exc.message}") from exc


def validate_payload(payload: Dict[str, Any], schema: Dict[str, Any], *, context: str) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = "/".join(str(x) for x in exc.path)
        where = f" at {path}" if path else ""
        raise ValueError(f"{context} schema validation failed{where}: {exc.message}") from exc


__all__ = [
    "ManagerInputSchema",
    "ManagerOutputSchema",
    "PLAN_PAYLOAD_SCHEMA",
    "PLAN_VALIDATION_RESPONSE_SCHEMA",
    "REVIEW_RESULT_SCHEMA",
    "WORK_TYPE_CLASSIFIER_SCHEMA",
    "EXECUTOR_RESPONSE_OUTPUT_SCHEMA",
    "FINAL_SPEC_AUDIT_SCHEMA",
    "validate_manager_payload",
    "validate_payload",
]
