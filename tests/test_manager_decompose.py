"""Tests for manager decompose logic: JSON parsing, fallback, payload_to_plan."""

from __future__ import annotations

import json

from agent.manager import ManagerOrchestrator, _extract_json_object, _truncate_report


# ---------------------------------------------------------------------------
# _extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_markdown_fence():
    raw = '```json\n{"a": 1}\n```'
    assert json.loads(_extract_json_object(raw)) == {"a": 1}


def test_extract_json_with_surrounding_text():
    raw = 'Here is the plan:\n{"a": 1}\nEnd.'
    assert json.loads(_extract_json_object(raw)) == {"a": 1}


def test_extract_json_empty():
    assert _extract_json_object("") == ""
    assert _extract_json_object(None) == ""


def test_extract_json_fence_with_lang_tag():
    raw = "```json\n{\"tasks\": []}\n```"
    assert json.loads(_extract_json_object(raw)) == {"tasks": []}


def test_extract_json_nested_braces():
    raw = 'text {"a": {"b": 1}} end'
    result = json.loads(_extract_json_object(raw))
    assert result == {"a": {"b": 1}}


# ---------------------------------------------------------------------------
# _truncate_report
# ---------------------------------------------------------------------------


def test_truncate_short():
    assert _truncate_report("hello", 1000) == "hello"


def test_truncate_empty():
    assert _truncate_report("", 100) == ""
    assert _truncate_report(None, 100) == ""


def test_truncate_long():
    text = "A" * 3000 + "B" * 3000 + "C" * 4000
    result = _truncate_report(text, 8000)
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
