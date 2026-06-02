from __future__ import annotations

import logging
from typing import List

import pytest

import modes.sdk.planning as planning
from modes.sdk.planning import (
    PlanObserver,
    load_plan,
    register_plan_observer,
    save_plan,
    unregister_plan_observer,
)
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


@pytest.fixture(autouse=True)
def _isolate_plan_observers():
    """Глобальный реестр наблюдателей — синглтон модуля; снимок до/после каждого
    теста гарантирует, что протёкший register не повлияет на соседние тесты."""
    snapshot = dict(planning._plan_observers)
    try:
        yield
    finally:
        planning._plan_observers.clear()
        planning._plan_observers.update(snapshot)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# The write-back observer lives at the single planning-layer chokepoint
# `save_plan`. BOTH manager save paths funnel through it:
#   * ManagerOrchestrator._save_live_plan  -> save_plan  (engine)
#   * ManagerMode._save_live_plan          -> save_plan  (transport/resume)
# so testing save_plan() directly exercises the same path both of them use.


def _minimal_plan(goal: str = "test-goal") -> ProjectPlan:
    return ProjectPlan(
        project_goal=goal,
        tasks=[DevTask(id="t1", title="Task 1", description="", acceptance_criteria=["done"])],
        status="active",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_save_plan_no_observer_writes_file(tmp_path) -> None:
    """Default (no observer registered): plan is persisted, no errors."""
    save_plan(str(tmp_path), _minimal_plan())
    loaded = load_plan(str(tmp_path))
    assert loaded is not None
    assert loaded.project_goal == "test-goal"


def test_registered_observer_called_with_plan(tmp_path) -> None:
    """A registered observer fires once with the saved plan."""
    received: List[ProjectPlan] = []
    register_plan_observer(str(tmp_path), None, received.append)
    try:
        plan = _minimal_plan()
        save_plan(str(tmp_path), plan)
        assert len(received) == 1
        assert received[0] is plan
    finally:
        unregister_plan_observer(str(tmp_path), None)


def test_observer_fires_on_every_save(tmp_path) -> None:
    """The observer fires on each subsequent save (live write-back)."""
    received: List[str] = []
    register_plan_observer(str(tmp_path), None, lambda p: received.append(p.project_goal))
    try:
        save_plan(str(tmp_path), _minimal_plan("g1"))
        save_plan(str(tmp_path), _minimal_plan("g2"))
        assert received == ["g1", "g2"]
    finally:
        unregister_plan_observer(str(tmp_path), None)


def test_unregister_stops_callback(tmp_path) -> None:
    received: List[ProjectPlan] = []
    register_plan_observer(str(tmp_path), None, received.append)
    unregister_plan_observer(str(tmp_path), None)
    save_plan(str(tmp_path), _minimal_plan())
    assert received == []


def test_observer_keyed_by_workdir_and_scoped_key(tmp_path) -> None:
    """An observer registered for a different key must not be called."""
    other = tmp_path / "other"
    other.mkdir()
    received: List[ProjectPlan] = []
    register_plan_observer(str(other), None, received.append)
    try:
        save_plan(str(tmp_path), _minimal_plan())
        assert received == []
    finally:
        unregister_plan_observer(str(other), None)


def test_none_and_empty_scoped_key_match(tmp_path) -> None:
    """Registering with None must match a save with '' (and vice versa)."""
    received: List[ProjectPlan] = []
    register_plan_observer(str(tmp_path), "", received.append)
    try:
        save_plan(str(tmp_path), _minimal_plan(), scoped_key=None)
        assert len(received) == 1
    finally:
        unregister_plan_observer(str(tmp_path), "")


def test_failing_observer_is_logged_and_plan_still_persisted(tmp_path, caplog) -> None:
    """A failing observer must NOT raise and the plan must still be on disk."""
    def _bad(_p: ProjectPlan) -> None:
        raise RuntimeError("sink failure")

    register_plan_observer(str(tmp_path), None, _bad)
    try:
        with caplog.at_level(logging.ERROR, logger="modes.sdk.planning"):
            save_plan(str(tmp_path), _minimal_plan())  # must not raise
        assert any(r.levelno >= logging.ERROR for r in caplog.records)
        # Plan is persisted despite the observer failure (observer runs AFTER write).
        loaded = load_plan(str(tmp_path))
        assert loaded is not None and loaded.project_goal == "test-goal"
    finally:
        unregister_plan_observer(str(tmp_path), None)


def test_plan_observer_type_alias_is_exported() -> None:
    assert PlanObserver is not None
