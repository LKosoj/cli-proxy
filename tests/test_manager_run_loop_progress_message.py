import asyncio
import types

from modes.sdk.runtime.contracts import DevTask, ProjectPlan, ReviewResult
from agent.manager import ManagerOrchestrator
from agent.plugins.run_command import RunCommandTool
from agent.tooling import helpers


class _FakeBot:
    def __init__(self) -> None:
        self.messages = []

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
        self.messages.append((chat_id, text))


def _make_orchestrator() -> ManagerOrchestrator:
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(
        defaults=types.SimpleNamespace(
            manager_max_tasks=10,
            manager_max_attempts=3,
            manager_auto_commit=False,
        )
    )
    return obj


def test_run_loop_sends_development_message_with_plan_progress(tmp_path) -> None:
    orch = _make_orchestrator()

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        return True, "ok"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        return ReviewResult(approved=True, summary="ok", comments="")

    async def _make_decision(_task, _review, workdir=""):
        return "approved", []

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(id="t1", title="Task 1", description="", acceptance_criteria=["ok"]),
            DevTask(id="t2", title="Task 2", description="", acceptance_criteria=["ok"]),
        ],
        status="active",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path))
    bot = _FakeBot()

    asyncio.run(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))

    dev_messages = [text for _chat_id, text in bot.messages if text.startswith("🔧 Разработка")]
    assert dev_messages
    assert dev_messages[0] == "🔧 Разработка (1/2): Task 1 (попытка 1/3)"


def test_run_loop_resumes_from_review_without_redevelopment(tmp_path) -> None:
    orch = _make_orchestrator()
    calls = {"develop": 0, "review": 0}

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        calls["develop"] += 1
        return True, "ok"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        calls["review"] += 1
        return ReviewResult(approved=True, summary="ok", comments="")

    async def _make_decision(_task, _review, workdir=""):
        return "approved", []

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Task 1",
                description="",
                acceptance_criteria=["ok"],
                status="in_review",
                attempt=1,
            ),
        ],
        status="active",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path))
    bot = _FakeBot()

    asyncio.run(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))

    assert calls["develop"] == 0
    assert calls["review"] == 1
    assert plan.tasks[0].attempt == 1
    assert any("🔍 Продолжаю ревью: Task 1 (попытка 1/3)" == text for _chat_id, text in bot.messages)


def test_run_loop_resumes_from_development_without_attempt_increment(tmp_path) -> None:
    orch = _make_orchestrator()
    calls = {"develop": 0, "review": 0}

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        calls["develop"] += 1
        return True, "ok"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        calls["review"] += 1
        return ReviewResult(approved=True, summary="ok", comments="")

    async def _make_decision(_task, _review, workdir=""):
        return "approved", []

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Task 1",
                description="",
                acceptance_criteria=["ok"],
                status="in_progress",
                attempt=1,
            ),
        ],
        status="active",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path))
    bot = _FakeBot()

    asyncio.run(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))

    assert calls["develop"] == 1
    assert calls["review"] == 1
    assert plan.tasks[0].attempt == 1


def test_run_loop_quiet_mode_suppresses_non_important_progress_messages(tmp_path) -> None:
    orch = _make_orchestrator()

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        return True, "ok"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        return ReviewResult(approved=True, summary="ok", comments="")

    async def _make_decision(_task, _review, workdir=""):
        return "approved", []

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[DevTask(id="t1", title="Task 1", description="", acceptance_criteria=["ok"])],
        status="active",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path), manager_quiet_mode=True)
    bot = _FakeBot()

    asyncio.run(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))

    assert bot.messages == []


def test_run_loop_quiet_mode_keeps_important_failure_messages(tmp_path) -> None:
    orch = _make_orchestrator()

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        return False, "hard fail"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        return ReviewResult(approved=False, summary="fail", comments="fail")

    async def _make_decision(_task, _review, workdir=""):
        return "rejected", ["fail"]

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Task 1",
                description="",
                acceptance_criteria=["ok"],
                max_attempts=1,
            )
        ],
        status="active",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path), manager_quiet_mode=True)
    bot = _FakeBot()

    asyncio.run(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))

    texts = [text for _chat_id, text in bot.messages]
    assert any(text.startswith("❌ Провал: Task 1") for text in texts)
    assert any(text == "⛔ План остановлен: критическая задача провалена." for text in texts)


def test_run_loop_calls_baseline_commit_once_before_first_development(tmp_path) -> None:
    orch = _make_orchestrator()
    order: list[str] = []

    async def _baseline(_session, _plan, _bot, _context, _dest):
        order.append("baseline")
        return True

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        order.append(f"develop:{_task.id}")
        return True, "ok"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        return ReviewResult(approved=True, summary="ok", comments="")

    async def _make_decision(_task, _review, workdir=""):
        return "approved", []

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    orch._auto_commit_baseline_before_first_step = _baseline
    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(id="t1", title="Task 1", description="", acceptance_criteria=["ok"]),
            DevTask(id="t2", title="Task 2", description="", acceptance_criteria=["ok"]),
        ],
        status="active",
    )

    session = types.SimpleNamespace(workdir=str(tmp_path))
    bot = _FakeBot()

    asyncio.run(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))

    assert order == ["baseline", "develop:t1", "develop:t2"]


def test_run_loop_waits_for_command_approval_before_review_completes(tmp_path, monkeypatch) -> None:
    orch = _make_orchestrator()
    approval_requested = asyncio.Event()
    allow_approval = asyncio.Event()
    issued_cmd_ids: list[str] = []

    async def _delegate_develop(_session, _plan, _task, **_kwargs):
        return True, "ok"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        tool = RunCommandTool()
        tool.initialize(config=None, services={})
        result = await tool.execute(
            {"command": "dangerous-command --do-it"},
            {"cwd": str(tmp_path), "session_id": "s1", "chat_id": 123, "chat_type": "private"},
        )
        assert result.get("success") is True
        return ReviewResult(approved=True, summary="ok", comments="")

    async def _make_decision(_task, _review, workdir=""):
        return "approved", []

    async def _auto_commit(_session, _task, _plan, _bot, _context, _dest):
        return False

    async def _reconcile_plan_after_commit(_session, _task, _plan, _bot, _context, _dest):
        return None

    async def _fake_exec(command: str, cwd: str):
        return {"success": True, "output": f"executed: {command} @ {cwd}"}

    async def _approve_later(cmd_id: str) -> None:
        approval_requested.set()
        await allow_approval.wait()
        helpers.approve_pending_command(cmd_id)

    def _approval_callback(_chat_id: int, cmd_id: str, _cmd: str, _reason: str) -> None:
        issued_cmd_ids.append(str(cmd_id))
        asyncio.get_running_loop().create_task(_approve_later(str(cmd_id)))

    monkeypatch.setattr(helpers, "check_command", lambda *_args, **_kwargs: (True, False, "Dangerous"))
    monkeypatch.setattr(helpers, "execute_shell_command", _fake_exec)
    monkeypatch.setattr(helpers, "_APPROVAL_CALLBACK", _approval_callback, raising=False)
    helpers._PENDING_COMMANDS.clear()
    helpers._PENDING_COMMAND_WAITERS.clear()
    helpers._PENDING_COMMAND_DECISIONS.clear()

    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_plan_after_commit

    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[DevTask(id="t1", title="Task 1", description="", acceptance_criteria=["ok"])],
        status="active",
    )

    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
    bot = _FakeBot()

    async def _scenario() -> None:
        run_task = asyncio.create_task(orch._run_loop(session, plan, bot, context=None, dest={"chat_id": 123}))
        await asyncio.wait_for(approval_requested.wait(), timeout=1.0)
        assert not run_task.done()
        assert plan.tasks[0].status == "in_review"
        allow_approval.set()
        await asyncio.wait_for(run_task, timeout=2.0)

    try:
        asyncio.run(_scenario())
    finally:
        helpers._PENDING_COMMANDS.clear()
        helpers._PENDING_COMMAND_WAITERS.clear()
        helpers._PENDING_COMMAND_DECISIONS.clear()

    assert issued_cmd_ids
    assert plan.tasks[0].status == "approved"
    assert plan.status == "completed"
