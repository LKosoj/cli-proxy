import asyncio
import types

from agent.contracts import DevTask, ProjectPlan, ReviewResult
from agent.manager import ManagerOrchestrator


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
        )
    )
    return obj


def test_run_loop_sends_development_message_with_plan_progress(tmp_path) -> None:
    orch = _make_orchestrator()

    async def _delegate_develop(_session, _plan, _task):
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

    async def _delegate_develop(_session, _plan, _task):
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

    async def _delegate_develop(_session, _plan, _task):
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

    async def _delegate_develop(_session, _plan, _task):
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

    async def _delegate_develop(_session, _plan, _task):
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
