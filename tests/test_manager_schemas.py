from __future__ import annotations

import pytest

from modes.manager.schemas import (
    EXECUTOR_RESPONSE_OUTPUT_SCHEMA,
    FINAL_SPEC_AUDIT_SCHEMA,
    ManagerInputSchema,
    ManagerOutputSchema,
    PLAN_PAYLOAD_SCHEMA,
    WORK_TYPE_CLASSIFIER_SCHEMA,
    validate_payload,
    validate_manager_payload,
)


def test_plan_payload_schema_rejects_missing_tasks() -> None:
    with pytest.raises(ValueError):
        validate_payload({"project_goal": "x"}, PLAN_PAYLOAD_SCHEMA, context="test")


def test_plan_payload_schema_accepts_requirements_and_coverage() -> None:
    payload = {
        "project_analysis": {
            "current_state": "state",
            "already_done": [],
            "remaining_work": ["x"],
            "requirements": ["REQ-1: x"],
            "checklist_table": [
                {"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""},
            ],
        },
        "checklist_table": [
            {"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""},
        ],
        "tasks": [
            {
                "id": "task_1",
                "title": "T",
                "description": "D",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1"],
                "depends_on": [],
            }
        ],
    }
    validate_payload(payload, PLAN_PAYLOAD_SCHEMA, context="test")


def test_plan_payload_schema_accepts_checklist_why_not_null() -> None:
    payload = {
        "project_analysis": {
            "current_state": "state",
            "already_done": [],
            "remaining_work": ["x"],
            "requirements": ["REQ-1: x"],
            "checklist_table": [
                {"item": "REQ-1", "status": "done", "how": "ok", "why_not": None},
            ],
        },
        "checklist_table": [
            {"item": "REQ-1", "status": "done", "how": "ok", "why_not": None},
        ],
        "tasks": [
            {
                "id": "task_1",
                "title": "T",
                "description": "D",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1"],
                "depends_on": [],
            }
        ],
    }
    validate_payload(payload, PLAN_PAYLOAD_SCHEMA, context="test")


def test_final_audit_schema_requires_requirement_matrix() -> None:
    payload = {
        "status": "PASS",
        "summary": "ok",
        "gaps_found": [],
        "fixes_applied": [],
        "remaining_gaps": [],
        "tests": [],
        "lint": [],
    }
    with pytest.raises(ValueError):
        validate_payload(payload, FINAL_SPEC_AUDIT_SCHEMA, context="test")


def test_final_audit_schema_accepts_nullable_gap_and_patch_candidate() -> None:
    payload = {
        "status": "PASS",
        "summary": "ok",
        "gaps_found": [],
        "fixes_applied": [],
        "remaining_gaps": [],
        "tests": [],
        "lint": [],
        "requirement_matrix": [
            {
                "req_id": "REQ-1",
                "status": "PASS",
                "tasks": ["task_1"],
                "evidence": ["pytest -q"],
                "gap": None,
            }
        ],
        "manager_prompt_patch_candidate": None,
    }
    validate_payload(payload, FINAL_SPEC_AUDIT_SCHEMA, context="test")


def test_manager_input_schema_rejects_invalid_tasks() -> None:
    bad_payload = {
        "project_goal": "Goal",
        "tasks": [{"id": "task_1", "title": "T", "description": "D"}],
    }
    with pytest.raises(ValueError):
        validate_manager_payload(bad_payload, ManagerInputSchema, contract="input")


def test_manager_output_schema_accepts_required_fields() -> None:
    payload = {
        "status": "ok",
        "summary": "done",
        "acceptance_criteria_report": [
            {"criterion": "c1", "status": "done", "evidence": "e1"},
        ],
        "checklist_table": [
            {"item": "i1", "status": "done", "how": "h", "why_not": ""},
        ],
        "tests": [{"command": "pytest -q", "result": "passed", "details": "ok"}],
        "lint": [{"command": "flake8", "result": "passed", "details": "ok"}],
        "assumptions": [],
        "blockers": [],
    }
    validate_manager_payload(payload, ManagerOutputSchema, contract="output")


def test_manager_output_schema_accepts_checklist_why_not_null() -> None:
    payload = {
        "status": "ok",
        "summary": "done",
        "acceptance_criteria_report": [
            {"criterion": "c1", "status": "done", "evidence": "e1"},
        ],
        "checklist_table": [
            {"item": "i1", "status": "done", "how": "h", "why_not": None},
        ],
        "tests": [{"command": "pytest -q", "result": "passed", "details": "ok"}],
        "lint": [{"command": "flake8", "result": "passed", "details": "ok"}],
        "assumptions": [],
        "blockers": [],
    }
    validate_manager_payload(payload, ManagerOutputSchema, contract="output")


def test_work_type_classifier_schema_requires_numeric_confidence() -> None:
    with pytest.raises(ValueError):
        validate_payload(
            {"work_type": "development", "confidence": "high", "reason": "x"},
            WORK_TYPE_CLASSIFIER_SCHEMA,
            context="test",
        )


def test_executor_response_output_schema_validates_outputs_array() -> None:
    validate_payload(
        {"summary": "ok", "outputs": [{"type": "text", "content": "answer"}]},
        EXECUTOR_RESPONSE_OUTPUT_SCHEMA,
        context="test",
    )
    with pytest.raises(ValueError):
        validate_payload(
            {"summary": "ok", "outputs": "wrong"},
            EXECUTOR_RESPONSE_OUTPUT_SCHEMA,
            context="test",
        )
