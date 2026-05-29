from __future__ import annotations

from agent.manager import (
    MIN_TASKS_FLOOR,
    MIN_TASKS_PER_REMAINING,
    MIN_TASKS_PER_REQ,
    _min_tasks_dynamic,
)
from modes.sdk.runtime.contracts import ProjectAnalysis


def _expected_dynamic(req_count: int, remaining_count: int, not_done_count: int) -> int:
    base = max(
        int(MIN_TASKS_FLOOR),
        int(req_count) * int(MIN_TASKS_PER_REQ),
        int(remaining_count) * int(MIN_TASKS_PER_REMAINING),
    )
    return int(base + ((int(not_done_count) + 2) // 3))


def test_min_tasks_dynamic_complex_scenario() -> None:
    analysis = ProjectAnalysis(
        current_state="baseline",
        already_done=["bootstrap"],
        remaining_work=[f"remaining_{idx}" for idx in range(1, 20)],
        requirements=[f"REQ-{idx}" for idx in range(1, 13)],
        checklist_table=[
            {"item": f"item_{idx}", "status": "not_done", "how": "", "why_not": "todo"}
            for idx in range(1, 8)
        ] + [
            {"item": "already_done", "status": "done", "how": "ok", "why_not": ""}
        ],
    )

    value = _min_tasks_dynamic(analysis)
    expected = _expected_dynamic(req_count=12, remaining_count=19, not_done_count=7)
    assert value == expected


def test_min_tasks_dynamic_medium_scenario() -> None:
    analysis = ProjectAnalysis(
        current_state="partial",
        already_done=[],
        remaining_work=[f"remaining_{idx}" for idx in range(1, 9)],
        requirements=[f"REQ-{idx}" for idx in range(1, 7)],
        checklist_table=[
            {"item": "item_1", "status": "not_done", "how": "", "why_not": "pending"},
            {"item": "item_2", "status": "not_done", "how": "", "why_not": "pending"},
        ],
    )

    first = _min_tasks_dynamic(analysis)
    second = _min_tasks_dynamic(analysis)
    expected = _expected_dynamic(req_count=6, remaining_count=8, not_done_count=2)
    assert first == expected
    assert second == expected


def test_min_tasks_dynamic_fallback_floor() -> None:
    assert _min_tasks_dynamic(None) == int(MIN_TASKS_FLOOR)
    assert _min_tasks_dynamic({}) == int(MIN_TASKS_FLOOR)
