from __future__ import annotations

import types

from agent.manager import _min_tasks_dynamic
from modes.sdk.runtime.contracts import ProjectAnalysis


def test_min_tasks_dynamic_fallback_floor_without_analysis() -> None:
    value = _min_tasks_dynamic(None)
    assert isinstance(value, int)
    assert value == 6


def test_min_tasks_dynamic_fallback_floor_with_empty_dict() -> None:
    value = _min_tasks_dynamic({})
    assert isinstance(value, int)
    assert value == 6


def test_min_tasks_dynamic_fallback_floor_with_missing_key_fields_dict() -> None:
    value = _min_tasks_dynamic({"objective": "", "foo": "bar"})
    assert isinstance(value, int)
    assert value == 6


def test_min_tasks_dynamic_fallback_floor_with_object_missing_fields() -> None:
    value = _min_tasks_dynamic(types.SimpleNamespace(objective=""))
    assert isinstance(value, int)
    assert value == 6


def test_min_tasks_dynamic_is_deterministic_for_same_analysis() -> None:
    analysis = ProjectAnalysis(
        current_state="state",
        already_done=["base wiring"],
        remaining_work=["api", "ui", "tests", "docs"],
        requirements=["REQ-1", "REQ-2", "REQ-3"],
        checklist_table=[
            {"item": "a", "status": "not_done", "how": "", "why_not": "x"},
            {"item": "b", "status": "done", "how": "ok", "why_not": ""},
            {"item": "c", "status": "not_done", "how": "", "why_not": "x"},
            {"item": "d", "status": "not_done", "how": "", "why_not": "x"},
        ],
    )

    first = _min_tasks_dynamic(analysis)
    second = _min_tasks_dynamic(analysis)

    assert isinstance(first, int)
    assert first == second
    # base=max(6, 3*1, 4*1)=6; unresolved bonus=ceil(3/3)=1 => 7
    assert first == 7


def test_min_tasks_dynamic_uses_max_of_requirements_and_remaining_work() -> None:
    analysis = ProjectAnalysis(
        current_state="state",
        already_done=[],
        remaining_work=["api", "ui", "tests", "docs", "infra", "ops", "monitoring", "alerts"],
        requirements=["REQ-1", "REQ-2"],
        checklist_table=[],
    )

    value = _min_tasks_dynamic(analysis)
    assert isinstance(value, int)
    assert value == 8
