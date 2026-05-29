from __future__ import annotations

import agent.manager as manager_mod


def test_manager_decomposition_constants_exist_and_are_internal_defaults() -> None:
    assert isinstance(manager_mod.MIN_TASKS_FLOOR, int)
    assert isinstance(manager_mod.MIN_TASKS_PER_REQ, int)
    assert isinstance(manager_mod.MIN_TASKS_PER_REMAINING, int)
    assert isinstance(manager_mod.ATOMICITY_MAX_REQS_PER_TASK, int)

    assert manager_mod.MIN_TASKS_FLOOR > 0
    assert manager_mod.MIN_TASKS_PER_REQ > 0
    assert manager_mod.MIN_TASKS_PER_REMAINING > 0
    assert manager_mod.ATOMICITY_MAX_REQS_PER_TASK > 0
