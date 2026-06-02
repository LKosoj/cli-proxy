from __future__ import annotations

import asyncio
import os
import types
from typing import Any, Dict

from agent.manager_core import ManagerOrchestrator
from modes.sdd.artifacts import parse_tasks_md, render_tasks_md
from modes.sdd.handoff import (
    handoff_scoped_key,
    make_writeback_observer,
    run_handoff_to_manager,
    seed_plan_from_tasks_md,
)
from modes.sdk.planning import (
    MANAGER_CONTINUE_TOKEN,
    load_plan,
    register_plan_observer,
    save_plan,
    unregister_plan_observer,
)
from modes.sdk.runtime.contracts import DevTask, ProjectPlan


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_plan(*, goal: str = "Test goal", status: str = "active") -> ProjectPlan:
    return ProjectPlan(
        project_goal=goal,
        tasks=[
            DevTask(
                id="T1",
                title="First task",
                description="Do something",
                acceptance_criteria=["AC-1"],
            )
        ],
        status=status,
    )


def _make_session(tmp_path, *, scoped_key: str = "1_s1") -> Any:
    from session import SddState

    return types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        scoped_key=scoped_key,
        modes=types.SimpleNamespace(active_mode=None),
        sdd=SddState(),
    )


# ---------------------------------------------------------------------------
# Test 1: seed → ManagerOrchestrator._load_live_plan returns active plan
# ---------------------------------------------------------------------------


def test_seed_sets_active_plan_loaded_by_manager_orchestrator(tmp_path) -> None:
    """seed_plan_from_tasks_md persists plan as 'active' so ManagerOrchestrator
    skips decompose on the next run."""
    session = _make_session(tmp_path)
    tasks_md_path = str(tmp_path / "tasks.md")

    plan = _make_plan()
    (tmp_path / "tasks.md").write_text(render_tasks_md(plan), encoding="utf-8")

    scoped_key = handoff_scoped_key(session)
    seeded = seed_plan_from_tasks_md(str(tmp_path), tasks_md_path, scoped_key)

    assert seeded.status == "active"
    assert len(seeded.tasks) == 1
    assert seeded.tasks[0].id == "T1"

    # Prove ManagerOrchestrator._load_live_plan sees the same plan
    orch = object.__new__(ManagerOrchestrator)
    loaded = orch._load_live_plan(session)

    assert loaded is not None
    assert loaded.status == "active"
    assert any(t.id == "T1" for t in loaded.tasks)


# ---------------------------------------------------------------------------
# Test 2: make_writeback_observer + save_plan writes tasks.md
# ---------------------------------------------------------------------------


def test_writeback_observer_writes_tasks_md_on_save(tmp_path) -> None:
    """Registering a writeback observer via make_writeback_observer ensures that
    any save_plan call (simulating manager decompose) updates tasks.md on disk."""
    tasks_md_path = str(tmp_path / "tasks.md")
    scoped_key = "1_s1"
    workdir = str(tmp_path)

    plan = _make_plan()
    save_plan(workdir, plan, scoped_key)

    observer = make_writeback_observer(tasks_md_path)
    register_plan_observer(workdir, scoped_key, observer)

    try:
        updated_plan = ProjectPlan(
            project_goal="Test goal",
            tasks=[
                DevTask(id="T1", title="First task", description="Do something",
                        acceptance_criteria=["AC-1"]),
                DevTask(id="T1.1", title="Subtask", description="Sub",
                        acceptance_criteria=["AC-sub"]),
            ],
            status="active",
        )
        save_plan(workdir, updated_plan, scoped_key)

        assert os.path.isfile(tasks_md_path)
        content = open(tasks_md_path, encoding="utf-8").read()
        assert "### T1.1" in content
    finally:
        unregister_plan_observer(workdir, scoped_key)


# ---------------------------------------------------------------------------
# Test 3: failed task with partial_work_note survives round-trip through sink
# ---------------------------------------------------------------------------


def test_failed_task_partial_work_note_roundtrip(tmp_path) -> None:
    """A 'failed' task with a multiline partial_work_note round-trips losslessly
    through render -> parse."""
    note = "Line one\nLine two — findings\n  indented continuation"
    plan = ProjectPlan(
        project_goal="Roundtrip test",
        tasks=[
            DevTask(
                id="T1",
                title="Failing task",
                description="",
                acceptance_criteria=["AC-1"],
                status="failed",
                partial_work_note=note,
            )
        ],
        status="active",
    )
    tasks_md_path = str(tmp_path / "tasks.md")
    observer = make_writeback_observer(tasks_md_path)
    register_plan_observer(str(tmp_path), None, observer)
    try:
        save_plan(str(tmp_path), plan)
        assert os.path.isfile(tasks_md_path)
        text = open(tasks_md_path, encoding="utf-8").read()
        recovered = parse_tasks_md(text)
        assert len(recovered.tasks) == 1
        t = recovered.tasks[0]
        assert t.status == "failed"
        assert t.partial_work_note is not None
        assert "Line one" in t.partial_work_note
        assert "findings" in t.partial_work_note
    finally:
        unregister_plan_observer(str(tmp_path), None)


# ---------------------------------------------------------------------------
# Test 4: end-to-end with fake pipeline
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Fake ModePipelineService: asserts correct args, simulates manager progress."""

    def __init__(self, workdir: str, scoped_key: str) -> None:
        self._workdir = workdir
        self._scoped_key = scoped_key
        self.called_with: Dict[str, Any] = {}

    async def run_mode_pipeline(
        self,
        session: Any,
        prompt: str,
        dest: dict,
        context: Any,
        *,
        mode_id: str,
    ) -> None:
        assert mode_id == "manager", f"Expected mode_id='manager', got {mode_id!r}"
        assert prompt == MANAGER_CONTINUE_TOKEN, f"Expected MANAGER_CONTINUE_TOKEN, got {prompt!r}"
        self.called_with = {"mode_id": mode_id, "prompt": prompt}

        # Simulate decompose: manager adds a subtask and saves the live plan
        plan = load_plan(self._workdir, self._scoped_key)
        assert plan is not None, "Manager expects a seeded plan to be available"
        assert plan.status == "active", f"Expected active plan, got {plan.status!r}"

        plan.tasks.append(
            DevTask(id="T1.1", title="Decomposed subtask", description="",
                    acceptance_criteria=["done"])
        )
        save_plan(self._workdir, plan, self._scoped_key)

        # Simulate completion: manager marks plan complete and saves
        plan.set_status("completed")
        plan.completion_report = "All done"
        save_plan(self._workdir, plan, self._scoped_key)


def test_end_to_end_run_handoff_to_manager(tmp_path) -> None:
    async def _run() -> None:
        session = _make_session(tmp_path)
        scoped_key = handoff_scoped_key(session)
        workdir = str(tmp_path)

        # Write an initial tasks.md
        plan = _make_plan()
        tasks_md_path = str(tmp_path / "tasks.md")
        (tmp_path / "tasks.md").write_text(render_tasks_md(plan), encoding="utf-8")

        fake_pipeline = _FakePipeline(workdir, scoped_key)

        sent_msgs: list = []

        class _FakeMsg:
            async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kw: Any) -> None:
                sent_msgs.append(text)

        mode = types.SimpleNamespace(
            _pipeline=lambda: fake_pipeline,
            _persist_sessions=lambda bot_app: None,
            _messaging=lambda *, bot_app, context: _FakeMsg(),
        )

        session.sdd.phase = "handoff"

        await run_handoff_to_manager(
            mode=mode,
            session=session,
            bot_app=None,
            context=None,
            dest={"kind": "telegram", "chat_id": 1},
            tasks_md_path=tasks_md_path,
        )

        # Pipeline was invoked with correct args
        assert fake_pipeline.called_with.get("mode_id") == "manager"
        assert fake_pipeline.called_with.get("prompt") == MANAGER_CONTINUE_TOKEN

        # SDD-поток закрыт: фаза done, пользователь уведомлён о завершении
        assert session.sdd.phase == "done"
        assert any("Менеджер завершил" in t for t in sent_msgs), \
            f"Expected completion notification, got: {sent_msgs}"

        # tasks.md reflects the final state (completed plan with subtask)
        content = open(tasks_md_path, encoding="utf-8").read()
        assert "### T1.1" in content

        # Observer is unregistered: a subsequent save_plan must NOT update tasks.md
        os.remove(tasks_md_path)
        some_plan = _make_plan(goal="after unregister")
        save_plan(workdir, some_plan, scoped_key)
        assert not os.path.isfile(tasks_md_path), (
            "tasks.md was written after observer should have been unregistered"
        )

    asyncio.run(_run())
