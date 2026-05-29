"""Tests for manager decompose logic: JSON parsing, fallback, payload_to_plan."""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from agent.manager import ATOMICITY_MAX_REQS_PER_TASK, ManagerOrchestrator
from modes.manager.services import ManagerUIService
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.planning import ManagerDecomposeNormalizationError
from modes.sdk.runtime.json_normalizer import extract_json_object


# ---------------------------------------------------------------------------
# extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_markdown_fence():
    raw = '```json\n{"a": 1}\n```'
    assert json.loads(extract_json_object(raw)) == {"a": 1}


def test_extract_json_with_surrounding_text():
    raw = 'Here is the plan:\n{"a": 1}\nEnd.'
    assert json.loads(extract_json_object(raw)) == {"a": 1}


def test_extract_json_empty():
    assert extract_json_object("") == ""
    assert extract_json_object(None) == ""


def test_extract_json_fence_with_lang_tag():
    raw = "```json\n{\"tasks\": []}\n```"
    assert json.loads(extract_json_object(raw)) == {"tasks": []}


def test_extract_json_nested_braces():
    raw = 'text {"a": {"b": 1}} end'
    result = json.loads(extract_json_object(raw))
    assert result == {"a": {"b": 1}}


# ---------------------------------------------------------------------------
# _truncate_report
# ---------------------------------------------------------------------------


def test_truncate_short():
    assert ManagerUIService.truncate_report("hello", 1000) == "hello"


def test_truncate_empty():
    assert ManagerUIService.truncate_report("", 100) == ""
    assert ManagerUIService.truncate_report(None, 100) == ""


def test_truncate_long():
    text = "A" * 3000 + "B" * 3000 + "C" * 4000
    result = ManagerUIService.truncate_report(text, 8000)
    assert "обрезано" in result
    assert result.startswith("A")
    assert result.endswith("C" * 100)  # ends with Cs
    assert len(result) < len(text)


# ---------------------------------------------------------------------------
# _payload_to_plan (through ManagerOrchestrator._try_parse_plan)
# ---------------------------------------------------------------------------


class _FakeConfig:
    class defaults:
        manager_max_tasks = 10
        manager_max_attempts = 3
        manager_decompose_timeout_sec = 300
        manager_response_archive = False
        manager_dev_timeout_sec = 600
        manager_review_timeout_sec = 300
        manager_dev_report_max_chars = 8000
        manager_auto_resume = True
        openai_api_key = "test"
        openai_model = "gpt-4"
        openai_base_url = ""
        openai_big_model = ""


def _make_orchestrator():
    """Create a ManagerOrchestrator with minimal config (will fail on real calls but OK for parsing)."""
    # We only need _payload_to_plan which doesn't use executor.
    # Patch __init__ to skip executor creation.
    obj = object.__new__(ManagerOrchestrator)
    obj._config = _FakeConfig()
    return obj


def test_payload_to_plan_valid():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "empty project",
            "already_done": [],
            "remaining_work": ["everything"],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "Setup",
                "description": "Create project structure",
                "acceptance_criteria": ["main.py exists"],
                "depends_on": [],
            }
        ],
    }
    plan = orch._payload_to_plan(payload, "Build app", 10)
    assert plan is not None
    assert plan.project_goal == "Build app"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].id == "task_1"
    assert plan.analysis is not None
    assert plan.analysis.current_state == "empty project"


def test_payload_to_plan_analysis_key():
    """Supports both 'project_analysis' and 'analysis' keys."""
    orch = _make_orchestrator()
    payload = {
        "analysis": {
            "current_state": "state",
            "already_done": ["x"],
            "remaining_work": ["y"],
        },
        "tasks": [{"id": "t1", "title": "T", "description": "D", "acceptance_criteria": ["ok"]}],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    assert plan.analysis is not None
    assert plan.analysis.current_state == "state"
    assert plan.analysis.requirements == []
    assert plan.analysis.checklist_table == []


def test_payload_to_plan_no_tasks():
    orch = _make_orchestrator()
    payload = {"tasks": []}
    assert orch._payload_to_plan(payload, "goal", 10) is None


def test_payload_to_plan_max_tasks():
    orch = _make_orchestrator()
    payload = {
        "tasks": [
            {"id": f"t{i}", "title": f"T{i}", "description": "d", "acceptance_criteria": ["ok"]}
            for i in range(20)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 5)
    assert plan is not None
    assert len(plan.tasks) == 5


def test_payload_to_plan_strips_and_fills_missing_ids_and_fields():
    orch = _make_orchestrator()
    payload = {
        "tasks": [
            {"id": "   ", "title": "  T1  ", "description": "  D1  ", "acceptance_criteria": [" ok ", "  "]},
            {"title": "T2", "description": "D2", "acceptance_criteria": ["ok"], "depends_on": ["  ", "task_1", "task_1"]},
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    assert plan.tasks[0].id == "task_1"
    assert plan.tasks[0].title == "T1"
    assert plan.tasks[0].description == "D1"
    assert plan.tasks[0].acceptance_criteria == ["ok"]
    assert plan.tasks[0].covers_requirements == []
    assert plan.tasks[1].id == "task_2"
    assert plan.tasks[1].depends_on == ["task_1"]


def test_payload_to_plan_parses_requirement_links():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": ["x"],
            "requirements": ["REQ-1: сделать A", "REQ-2: сделать B"],
            "checklist_table": [
                {"item": "REQ-1", "status": "done", "how": "есть", "why_not": ""},
            ],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1"],
            }
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    assert plan.analysis is not None
    assert plan.analysis.requirements == ["REQ-1: сделать A", "REQ-2: сделать B"]
    assert plan.analysis.checklist_table == [
        {"item": "REQ-1", "status": "done", "how": "есть", "why_not": ""},
    ]
    assert plan.tasks[0].covers_requirements == ["REQ-1"]


def test_try_parse_plan_accepts_checklist_why_not_null():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": ["x"],
            "requirements": ["REQ-1: сделать A"],
            "checklist_table": [
                {"item": "REQ-1", "status": "done", "how": "есть", "why_not": None},
            ],
        },
        "checklist_table": [
            {"item": "REQ-1", "status": "done", "how": "есть", "why_not": None},
        ],
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1"],
            }
        ],
    }
    plan = orch._try_parse_plan(json.dumps(payload, ensure_ascii=False), "goal", 10)
    assert plan is not None
    assert plan.analysis is not None
    assert plan.analysis.checklist_table == [
        {"item": "REQ-1", "status": "done", "how": "есть", "why_not": ""},
    ]


def test_validate_plan_structure_requires_req_coverage():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": ["x"],
            "requirements": ["REQ-1: сделать A", "REQ-2: сделать B"],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1"],
            }
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert "Требование 'REQ-2: сделать B' не покрыто ни одной задачей" in issues


def test_validate_plan_structure_accepts_short_req_links_for_detailed_requirements():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [f"rw_{i}" for i in range(1, 7)],
            "requirements": ["REQ-1: сделать A"],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1"] if i == 1 else [],
            }
            for i in range(1, 7)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any("covers_requirements содержит неизвестный 'REQ-1'" in str(x) for x in issues)
    assert "Требование 'REQ-1: сделать A' не покрыто ни одной задачей" not in issues


def test_validate_plan_structure_detects_unknown_req_link():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": ["x"],
            "requirements": ["REQ-1: сделать A"],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-2"],
            }
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert "covers_requirements содержит неизвестный 'REQ-2'" in "\n".join(issues)


def test_validate_plan_structure_reports_task_count_below_min():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": [],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
            }
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in issues)


def test_validate_plan_structure_reports_task_count_below_min_from_dynamic_remaining_work():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [f"rw_{i}" for i in range(1, 9)],
            "requirements": [],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 8)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert "TASK_COUNT_BELOW_MIN: tasks_count=7, min_tasks_dynamic=8" in issues


def test_validate_plan_structure_reports_task_count_below_min_with_checklist_bonus():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": [],
            "checklist_table": [
                {"item": "a", "status": "not_done", "how": "", "why_not": "x"},
                {"item": "b", "status": "not_done", "how": "", "why_not": "x"},
                {"item": "c", "status": "not_done", "how": "", "why_not": "x"},
            ],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 7)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert "TASK_COUNT_BELOW_MIN: tasks_count=6, min_tasks_dynamic=7" in issues


def test_validate_plan_structure_does_not_report_task_count_below_min_when_equal_to_dynamic_min():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [f"rw_{i}" for i in range(1, 9)],
            "requirements": [],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 9)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in issues)


def test_validate_plan_structure_handles_empty_analysis_for_task_count_min_boundary():
    orch = _make_orchestrator()
    payload = {
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 7)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    assert plan.analysis is None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in issues)


def test_validate_plan_structure_clamps_dynamic_min_to_max_tasks_limit():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [f"rw_{i}" for i in range(1, 21)],
            "requirements": [],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 11)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in issues)


def test_validate_plan_structure_uses_clamped_dynamic_min_when_below_max_tasks_limit():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [f"rw_{i}" for i in range(1, 21)],
            "requirements": [],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 10)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert "TASK_COUNT_BELOW_MIN: tasks_count=9, min_tasks_dynamic=10" in issues


def test_validate_plan_structure_does_not_report_task_count_below_min_when_enough_tasks():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": [],
        },
        "tasks": [
            {
                "id": f"task_{i}",
                "title": f"T{i}",
                "description": f"D{i}",
                "acceptance_criteria": ["ok"],
            }
            for i in range(1, 7)
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in issues)


def test_validate_plan_structure_reports_task_too_broad_req_coverage():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": ["REQ-1", "REQ-2", "REQ-3"],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1", "REQ-2", "REQ-3"],
            },
            {
                "id": "task_2",
                "title": "T2",
                "description": "D2",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_3",
                "title": "T3",
                "description": "D3",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_4",
                "title": "T4",
                "description": "D4",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_5",
                "title": "T5",
                "description": "D5",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_6",
                "title": "T6",
                "description": "D6",
                "acceptance_criteria": ["ok"],
            },
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert any(str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:") for x in issues)


def test_validate_plan_structure_does_not_report_task_too_broad_req_coverage_when_within_limit():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": ["REQ-1", "REQ-2"],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1", "REQ-2"],
            },
            {
                "id": "task_2",
                "title": "T2",
                "description": "D2",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_3",
                "title": "T3",
                "description": "D3",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_4",
                "title": "T4",
                "description": "D4",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_5",
                "title": "T5",
                "description": "D5",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_6",
                "title": "T6",
                "description": "D6",
                "acceptance_criteria": ["ok"],
            },
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:") for x in issues)


def test_validate_plan_structure_does_not_report_task_too_broad_req_coverage_at_exact_limit():
    orch = _make_orchestrator()
    req_ids = [f"REQ-{i}" for i in range(1, int(ATOMICITY_MAX_REQS_PER_TASK) + 1)]
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": req_ids,
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": list(req_ids),
            },
            {
                "id": "task_2",
                "title": "T2",
                "description": "D2",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_3",
                "title": "T3",
                "description": "D3",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_4",
                "title": "T4",
                "description": "D4",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_5",
                "title": "T5",
                "description": "D5",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_6",
                "title": "T6",
                "description": "D6",
                "acceptance_criteria": ["ok"],
            },
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:") for x in issues)


def test_validate_plan_structure_reports_task_too_broad_req_coverage_at_limit_plus_one():
    orch = _make_orchestrator()
    req_ids = [f"REQ-{i}" for i in range(1, int(ATOMICITY_MAX_REQS_PER_TASK) + 2)]
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": req_ids,
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": list(req_ids),
            },
            {
                "id": "task_2",
                "title": "T2",
                "description": "D2",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_3",
                "title": "T3",
                "description": "D3",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_4",
                "title": "T4",
                "description": "D4",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_5",
                "title": "T5",
                "description": "D5",
                "acceptance_criteria": ["ok"],
            },
            {
                "id": "task_6",
                "title": "T6",
                "description": "D6",
                "acceptance_criteria": ["ok"],
            },
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert (
        "TASK_TOO_BROAD_REQ_COVERAGE: "
        f"task_id=task_1, covers_requirements={len(req_ids)}, "
        f"max_allowed={ATOMICITY_MAX_REQS_PER_TASK}"
    ) in issues


def test_validate_plan_structure_keeps_old_depends_on_and_cycle_checks():
    orch = _make_orchestrator()
    payload_missing_dep = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": [],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "depends_on": ["task_missing"],
            },
            {"id": "task_2", "title": "T2", "description": "D2", "acceptance_criteria": ["ok"]},
            {"id": "task_3", "title": "T3", "description": "D3", "acceptance_criteria": ["ok"]},
            {"id": "task_4", "title": "T4", "description": "D4", "acceptance_criteria": ["ok"]},
            {"id": "task_5", "title": "T5", "description": "D5", "acceptance_criteria": ["ok"]},
            {"id": "task_6", "title": "T6", "description": "D6", "acceptance_criteria": ["ok"]},
        ],
    }
    plan_missing_dep = orch._payload_to_plan(payload_missing_dep, "goal", 10)
    assert plan_missing_dep is not None
    issues_missing_dep = ManagerOrchestrator._validate_plan_structure(plan_missing_dep)
    assert any("зависит от несуществующей" in str(x) for x in issues_missing_dep)

    payload_cycle = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": [],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "depends_on": ["task_2"],
            },
            {
                "id": "task_2",
                "title": "T2",
                "description": "D2",
                "acceptance_criteria": ["ok"],
                "depends_on": ["task_1"],
            },
            {"id": "task_3", "title": "T3", "description": "D3", "acceptance_criteria": ["ok"]},
            {"id": "task_4", "title": "T4", "description": "D4", "acceptance_criteria": ["ok"]},
            {"id": "task_5", "title": "T5", "description": "D5", "acceptance_criteria": ["ok"]},
            {"id": "task_6", "title": "T6", "description": "D6", "acceptance_criteria": ["ok"]},
        ],
    }
    plan_cycle = orch._payload_to_plan(payload_cycle, "goal", 10)
    assert plan_cycle is not None
    issues_cycle = ManagerOrchestrator._validate_plan_structure(plan_cycle)
    assert any("циклическая зависимость" in str(x) for x in issues_cycle)


def test_validate_plan_structure_old_and_new_checks_work_in_parallel():
    orch = _make_orchestrator()
    payload = {
        "project_analysis": {
            "current_state": "s",
            "already_done": [],
            "remaining_work": [],
            "requirements": ["REQ-1", "REQ-2"],
        },
        "tasks": [
            {
                "id": "task_1",
                "title": "T1",
                "description": "D1",
                "acceptance_criteria": ["ok"],
                "covers_requirements": ["REQ-1", "REQ-2", "REQ-999"],
            },
            {"id": "task_2", "title": "T2", "description": "D2", "acceptance_criteria": ["ok"]},
            {"id": "task_3", "title": "T3", "description": "D3", "acceptance_criteria": ["ok"]},
            {"id": "task_4", "title": "T4", "description": "D4", "acceptance_criteria": ["ok"]},
            {"id": "task_5", "title": "T5", "description": "D5", "acceptance_criteria": ["ok"]},
            {"id": "task_6", "title": "T6", "description": "D6", "acceptance_criteria": ["ok"]},
        ],
    }
    plan = orch._payload_to_plan(payload, "goal", 10)
    assert plan is not None
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert any(str(x).startswith("TASK_TOO_BROAD_REQ_COVERAGE:") for x in issues)
    assert any("covers_requirements содержит неизвестный 'REQ-999'" in str(x) for x in issues)


def test_decompose_raises_on_full_normalization_failure(monkeypatch, tmp_path):
    orch = _make_orchestrator()
    session = types.SimpleNamespace(workdir=str(tmp_path))

    async def _fake_cli(*_args, **_kwargs):
        return "planning", "невалидный вывод без json"

    async def _fake_normalize(self, *_args, **_kwargs):
        return None

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr(ManagerOrchestrator, "_normalize_plan", _fake_normalize)

    async def _run():
        await orch._decompose(session, "Сделать X", bot=None, context=None, dest={"chat_id": None})

    with pytest.raises(ManagerDecomposeNormalizationError) as exc_info:
        asyncio.run(_run())

    assert "Не удалось построить план" in str(exc_info.value)


def test_decompose_passes_min_tasks_dynamic_to_prompt(monkeypatch, tmp_path):
    orch = _make_orchestrator()
    session = types.SimpleNamespace(workdir=str(tmp_path))
    captured = {"analysis_args": []}

    async def _fake_cli(_session, _config, _work_type, prompt, **_kwargs):
        captured["prompt"] = str(prompt)
        captured["response_format"] = str(_kwargs.get("response_format") or "")
        payload = {
            "project_analysis": {
                "current_state": "state",
                "already_done": [],
                "remaining_work": ["x"],
            },
            "tasks": [
                {
                    "id": "task_1",
                    "title": "T1",
                    "description": "D1",
                    "acceptance_criteria": ["ok"],
                }
            ],
        }
        return "planning", json.dumps(payload, ensure_ascii=False)

    def _fake_min_tasks(analysis):
        captured["analysis_args"].append(analysis)
        return 17

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr("agent.manager_core._min_tasks_dynamic", _fake_min_tasks)
    monkeypatch.setattr(
        ManagerOrchestrator,
        "_manager_prompt",
        lambda _self, _workdir, key: (
            "UG={user_goal};MAX={max_tasks};MIN={min_tasks_dynamic}"
            if key == "decompose_instruction"
            else ""
        ),
    )
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)
    monkeypatch.setattr(ManagerOrchestrator, "_apply_manager_prompt_learning", lambda _self, _workdir, text: text)
    monkeypatch.setattr(ManagerOrchestrator, "_git_is_usable", lambda _self, _workdir: True)
    monkeypatch.setattr(ManagerOrchestrator, "_validate_plan", lambda *_args, **_kwargs: asyncio.sleep(0, result=[]))

    async def _run():
        return await orch._decompose(session, "Сделать X", bot=None, context=None, dest={"chat_id": None})

    plan = asyncio.run(_run())
    assert plan is not None
    assert "UG=Сделать X" in captured.get("prompt", "")
    assert "MAX=10" in captured.get("prompt", "")
    assert "MIN=17" in captured.get("prompt", "")
    assert captured.get("response_format") == CLIResponseFormat.JSON_OBJECT
    assert any(arg is None for arg in captured.get("analysis_args", []))


def test_decompose_logs_validation_diagnostics_for_replan(monkeypatch, tmp_path):
    orch = _make_orchestrator()
    session = types.SimpleNamespace(workdir=str(tmp_path))
    logged_info: list[str] = []

    async def _fake_cli(_session, _config, _work_type, _prompt, **_kwargs):
        payload = {
            "project_analysis": {
                "current_state": "state",
                "already_done": [],
                "remaining_work": ["x"],
            },
            "tasks": [
                {
                    "id": "task_1",
                    "title": "T1",
                    "description": "D1",
                    "acceptance_criteria": ["ok"],
                }
            ],
        }
        return "planning", json.dumps(payload, ensure_ascii=False)

    async def _fake_validate_plan(self, plan, workdir):
        _ = self, plan, workdir
        return ["TASK_COUNT_BELOW_MIN: tasks_count=1, min_tasks_dynamic=6"]

    async def _fake_fix_plan(*_args, **_kwargs):
        return None

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr(ManagerOrchestrator, "_validate_plan", _fake_validate_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_fix_plan_via_cli", _fake_fix_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_git_is_usable", lambda _self, _workdir: True)
    # Capture manager diagnostic logs directly to avoid global logging-state flakes.

    def _capture_info(message, *args, **kwargs):
        _ = kwargs
        text = str(message)
        if args:
            try:
                text = text % args
            except Exception:
                text = " ".join([text, *[str(x) for x in args]])
        logged_info.append(text)

    monkeypatch.setattr("agent.manager_core._log.info", _capture_info)

    async def _run():
        return await orch._decompose(session, "Сделать X", bot=None, context=None, dest={"chat_id": None})

    plan = asyncio.run(_run())
    assert plan is not None
    text = "\n".join(logged_info)
    assert "decompose: validation diagnostics" in text
    assert "min_tasks_dynamic=6" in text
    assert "max_tasks=10" in text
    assert "actual_tasks=1" in text
    assert "replan_reason=TASK_COUNT_BELOW_MIN" in text


def test_decompose_freezes_min_tasks_dynamic_across_fix_attempts(monkeypatch, tmp_path):
    orch = _make_orchestrator()
    orch._config.defaults.manager_max_tasks = 50
    session = types.SimpleNamespace(workdir=str(tmp_path))
    fix_calls = {"count": 0}

    def _mk_payload(tasks_count: int, remaining_work_count: int) -> dict:
        return {
            "project_analysis": {
                "current_state": "state",
                "already_done": [],
                "remaining_work": [f"rw_{i}" for i in range(1, remaining_work_count + 1)],
                "requirements": [],
            },
            "tasks": [
                {
                    "id": f"task_{i}",
                    "title": f"T{i}",
                    "description": f"D{i}",
                    "acceptance_criteria": ["ok"],
                    "depends_on": [f"task_{i - 1}"] if i > 1 else [],
                }
                for i in range(1, tasks_count + 1)
            ],
        }

    async def _fake_cli(_session, _config, _work_type, _prompt, **_kwargs):
        return "planning", json.dumps(_mk_payload(12, 13), ensure_ascii=False)

    async def _fake_validate_plan(self, plan, workdir):
        _ = self, workdir
        return ManagerOrchestrator._validate_plan_structure(plan)

    async def _fake_fix_plan(self, _session, plan, issues, user_goal, _timeout, _workdir, **_kwargs):
        _ = self
        fix_calls["count"] += 1
        assert any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in (issues or []))
        next_tasks = len(list(plan.tasks or [])) + 1
        next_remaining = len(list(plan.analysis.remaining_work or [])) + 1 if plan.analysis else next_tasks + 1
        fixed = orch._payload_to_plan(
            _mk_payload(next_tasks, next_remaining),
            user_goal,
            int(orch._config.defaults.manager_max_tasks),
        )
        assert fixed is not None
        return fixed

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr(ManagerOrchestrator, "_validate_plan", _fake_validate_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_fix_plan_via_cli", _fake_fix_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_git_is_usable", lambda _self, _workdir: True)
    monkeypatch.setattr(
        ManagerOrchestrator,
        "_manager_prompt",
        lambda _self, _workdir, key: (
            "UG={user_goal};MAX={max_tasks};MIN={min_tasks_dynamic}"
            if key == "decompose_instruction"
            else ""
        ),
    )
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)
    monkeypatch.setattr(ManagerOrchestrator, "_apply_manager_prompt_learning", lambda _self, _workdir, text: text)

    async def _run():
        return await orch._decompose(session, "Сделать X", bot=None, context=None, dest={"chat_id": None})

    plan = asyncio.run(_run())
    assert plan is not None
    assert fix_calls["count"] == 1
    assert len(list(plan.tasks or [])) == 13
    assert int(getattr(plan, "_manager_min_tasks_dynamic", 0) or 0) == 13
    issues = ManagerOrchestrator._validate_plan_structure(plan)
    assert not any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in issues)


def test_decompose_accepts_goal_alignment_semantic_warnings_after_reaching_minimum(monkeypatch, tmp_path):
    orch = _make_orchestrator()
    orch._config.defaults.manager_max_tasks = 30
    session = types.SimpleNamespace(workdir=str(tmp_path))
    validate_calls = {"count": 0}
    fix_calls = {"count": 0}

    def _mk_payload(tasks_count: int, remaining_work_count: int) -> dict:
        return {
            "project_analysis": {
                "current_state": "state",
                "already_done": [],
                "remaining_work": [f"rw_{i}" for i in range(1, remaining_work_count + 1)],
                "requirements": [],
            },
            "tasks": [
                {
                    "id": f"task_{i}",
                    "title": f"T{i}",
                    "description": f"D{i}",
                    "acceptance_criteria": ["ok"],
                    "depends_on": [f"task_{i - 1}"] if i > 1 else [],
                }
                for i in range(1, tasks_count + 1)
            ],
        }

    async def _fake_cli(_session, _config, _work_type, _prompt, **_kwargs):
        return "planning", json.dumps(_mk_payload(8, 9), ensure_ascii=False)

    async def _fake_validate_plan(_self, _plan, _workdir):
        validate_calls["count"] += 1
        if validate_calls["count"] == 1:
            return ["TASK_COUNT_BELOW_MIN: tasks_count=8, min_tasks_dynamic=9"]
        return ["Несоответствие между планом и projectgoal: задача 'S9.8' отсутствует в projectgoal."]

    async def _fake_fix_plan(_self, _session, _plan, _issues, _user_goal, _timeout, _workdir, **_kwargs):
        fix_calls["count"] += 1
        fixed = orch._payload_to_plan(
            _mk_payload(9, 9),
            "goal",
            int(orch._config.defaults.manager_max_tasks),
        )
        assert fixed is not None
        return fixed

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr(ManagerOrchestrator, "_validate_plan", _fake_validate_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_fix_plan_via_cli", _fake_fix_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_git_is_usable", lambda _self, _workdir: True)
    monkeypatch.setattr(
        ManagerOrchestrator,
        "_manager_prompt",
        lambda _self, _workdir, key: (
            "UG={user_goal};MAX={max_tasks};MIN={min_tasks_dynamic}"
            if key == "decompose_instruction"
            else ""
        ),
    )
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)
    monkeypatch.setattr(ManagerOrchestrator, "_apply_manager_prompt_learning", lambda _self, _workdir, text: text)

    async def _run():
        return await orch._decompose(session, "Сделать X", bot=None, context=None, dest={"chat_id": None})

    plan = asyncio.run(_run())
    assert plan is not None
    assert len(list(plan.tasks or [])) == 9
    assert validate_calls["count"] == 2
    assert fix_calls["count"] == 1


def test_should_stabilize_task_count_when_floor_and_goal_alignment_alternate() -> None:
    history = [
        {"TASK_COUNT_BELOW_MIN"},
        {"GOAL_ALIGNMENT_MISMATCH"},
    ]
    assert ManagerOrchestrator._should_stabilize_task_count(history) is True


def test_issue_tags_detect_goal_alignment_mismatch_patterns() -> None:
    issues = ["Лишняя задача: S9.8 не упомянута в projectgoal."]
    tags = ManagerOrchestrator._issue_tags(issues)
    assert "GOAL_ALIGNMENT_MISMATCH" in tags
    assert ManagerOrchestrator._issues_are_goal_alignment_only(issues) is True


def test_final_acceptance_three_scenarios_task_counts_and_replan_reasons(monkeypatch, tmp_path):
    orch = _make_orchestrator()
    orch._config.defaults.manager_max_tasks = 30
    session = types.SimpleNamespace(workdir=str(tmp_path))

    scenarios = {
        "simple": {"remaining_work": 3, "initial_tasks": 6, "fixed_tasks": None},
        "medium": {"remaining_work": 10, "initial_tasks": 8, "fixed_tasks": 10},
        "complex": {"remaining_work": 18, "initial_tasks": 11, "fixed_tasks": 19},
    }
    current = {"name": "simple"}
    logs: list[tuple[str, str, str]] = []

    def _mk_payload(tasks_count: int, remaining_work_count: int) -> dict:
        return {
            "project_analysis": {
                "current_state": "state",
                "already_done": [],
                "remaining_work": [f"rw_{i}" for i in range(1, remaining_work_count + 1)],
                "requirements": [],
            },
            "tasks": [
                {
                    "id": f"task_{i}",
                    "title": f"T{i}",
                    "description": f"D{i}",
                    "acceptance_criteria": ["ok"],
                    "depends_on": [f"task_{i - 1}"] if i > 1 else [],
                }
                for i in range(1, tasks_count + 1)
            ],
        }

    async def _fake_cli(_session, _config, _work_type, _prompt, **_kwargs):
        sc = scenarios[current["name"]]
        return "planning", json.dumps(
            _mk_payload(int(sc["initial_tasks"]), int(sc["remaining_work"])),
            ensure_ascii=False,
        )

    async def _fake_validate_plan(self, plan, workdir):
        _ = self, workdir
        return ManagerOrchestrator._validate_plan_structure(plan)

    async def _fake_fix_plan(_self, _session, _plan, issues, _user_goal, _timeout, _workdir, **_kwargs):
        sc = scenarios[current["name"]]
        if sc["fixed_tasks"] is None:
            return None
        assert any(str(x).startswith("TASK_COUNT_BELOW_MIN:") for x in (issues or []))
        fixed_payload = _mk_payload(int(sc["fixed_tasks"]), int(sc["remaining_work"]))
        return orch._payload_to_plan(
            fixed_payload,
            "goal",
            int(orch._config.defaults.manager_max_tasks),
        )

    def _capture(level: str):
        def _inner(message, *args, **kwargs):
            _ = kwargs
            text = str(message)
            if args:
                try:
                    text = text % args
                except Exception:
                    text = " ".join([text, *[str(x) for x in args]])
            logs.append((current["name"], level, text))
        return _inner

    monkeypatch.setattr("agent.manager_core.run_prompt_routed_meta", _fake_cli)
    monkeypatch.setattr(ManagerOrchestrator, "_validate_plan", _fake_validate_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_fix_plan_via_cli", _fake_fix_plan)
    monkeypatch.setattr(ManagerOrchestrator, "_git_is_usable", lambda _self, _workdir: True)
    monkeypatch.setattr(
        ManagerOrchestrator,
        "_manager_prompt",
        lambda _self, _workdir, key: (
            "UG={user_goal};MAX={max_tasks};MIN={min_tasks_dynamic}"
            if key == "decompose_instruction"
            else ""
        ),
    )
    monkeypatch.setattr(ManagerOrchestrator, "_with_invariant_policy", lambda _self, _workdir, text: text)
    monkeypatch.setattr(ManagerOrchestrator, "_apply_manager_prompt_learning", lambda _self, _workdir, text: text)
    monkeypatch.setattr("agent.manager_core._log.info", _capture("info"))
    monkeypatch.setattr("agent.manager_core._log.warning", _capture("warning"))

    final_counts: dict[str, int] = {}

    async def _run(goal: str):
        return await orch._decompose(session, goal, bot=None, context=None, dest={"chat_id": None})

    for name in ("simple", "medium", "complex"):
        current["name"] = name
        plan = asyncio.run(_run(f"{name} scenario"))
        assert plan is not None
        final_counts[name] = len(list(plan.tasks or []))

    assert final_counts["simple"] == 6
    assert final_counts["medium"] == 10
    assert final_counts["complex"] == 19
    assert final_counts["complex"] > 11

    for name, expected_actual in (("medium", 8), ("complex", 11)):
        diag_logs = [
            msg
            for sc_name, level, msg in logs
            if sc_name == name and level == "info" and "decompose: validation diagnostics" in msg
        ]
        assert diag_logs
        text = "\n".join(diag_logs)
        assert "replan_reason=TASK_COUNT_BELOW_MIN" in text
        assert f"actual_tasks={expected_actual}" in text

    simple_diag_logs = [
        msg
        for sc_name, level, msg in logs
        if sc_name == "simple" and level == "info" and "decompose: validation diagnostics" in msg
    ]
    assert not simple_diag_logs
