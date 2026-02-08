import asyncio
import types

from agent.contracts import DevTask, ProjectPlan
from session_management import SessionManagement


def test_run_manager_failed_resume_prompt_includes_reason(monkeypatch, tmp_path) -> None:
    plan = ProjectPlan(
        project_goal="Goal",
        tasks=[
            DevTask(
                id="t1",
                title="Task 1",
                description="",
                acceptance_criteria=["ok"],
                status="failed",
                attempt=1,
                max_attempts=3,
                review_comments="Падает проверка smoke",
            )
        ],
        analysis=None,
        status="failed",
    )

    monkeypatch.setattr("agent.manager_store.load_plan", lambda _workdir: plan)

    sent_messages = []

    class _FakeBotApp:
        def __init__(self) -> None:
            self.config = types.SimpleNamespace(
                defaults=types.SimpleNamespace(manager_auto_resume=False),
            )
            self.manager_resume_pending = {}

        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs) -> None:
            sent_messages.append((chat_id, text))

    bot_app = _FakeBotApp()
    sm = SessionManagement(bot_app)
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))

    asyncio.run(
        sm.run_manager(
            session,
            "сделай задачу",
            {"kind": "telegram", "chat_id": 123},
            context=None,
        )
    )

    assert "s1" in bot_app.manager_resume_pending
    assert sent_messages
    text = sent_messages[0][1]
    assert "Причина остановки:" in text
    assert "Падает проверка smoke" in text
