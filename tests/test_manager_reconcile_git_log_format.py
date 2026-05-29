import os
import asyncio

from config import load_config
from agent.manager import ManagerOrchestrator
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def test_reconcile_uses_git_log_stat_with_hash(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.manager_response_archive = False

    orch = ManagerOrchestrator(cfg)

    called = {}

    async def fake_run_git(workdir: str, args: list[str]):
        called["workdir"] = workdir
        called["args"] = list(args)
        return 0, ""

    async def fake_chat_completion(*_a, **_kw):
        return None

    monkeypatch.setattr(ManagerOrchestrator, "_run_git", staticmethod(fake_run_git))
    monkeypatch.setattr("agent.manager_core.chat_completion", fake_chat_completion)

    plan = ProjectPlan(
        project_goal="x",
        analysis=ProjectAnalysis(current_state="Empty project", already_done=[], remaining_work=[]),
        tasks=[
            DevTask(id="task_1", title="T", description="D", acceptance_criteria=["A"], depends_on=[]),
        ],
    )

    session = type("S", (), {"workdir": str(tmp_path)})()
    task = plan.tasks[0]

    asyncio.run(orch._reconcile_plan_after_commit(session, task, plan, bot=None, context=None, dest={"kind": "telegram"}))

    assert called.get("workdir") == str(tmp_path)
    args = called.get("args")
    assert args is not None
    assert args[:2] == ["log", "-1"]
    assert "--stat" in args
    assert "--format=%s (%h)" in args
