import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep


def test_orchestrator_replans_after_each_step(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            return [
                PlanStep(id="step1", title="s1", instruction="do 1"),
                PlanStep(id="step2", title="s2", instruction="do 2", depends_on=["step1"]),
                PlanStep(id="step3", title="s3", instruction="do 3", depends_on=["step2"]),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        executed = []

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            executed.append(step.id)
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"out {step.id}"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=None, context=None, dest=dest)
        assert "done step1" in out
        assert "done step2" in out
        assert "done step3" in out

        # step1 must not be executed twice after replanning
        assert executed == ["step1", "step2", "step3"]
        # Periodic replan every 2 non-ask steps: with 3 steps total, we expect 2 plan builds.
        assert calls["n"] == 2

    asyncio.run(_run())


def test_orchestrator_stabilizes_step_ids_across_replans(tmp_path, monkeypatch):
    """
    When replanning is triggered (e.g. by an error), planner may change step ids.
    Orchestrator should map "same meaning" steps to prior ids to avoid re-executing work.
    """
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    PlanStep(id="step1", title="Снять метрики", instruction="do 1"),
                    PlanStep(id="step2", title="Починить баг", instruction="do 2", depends_on=["step1"]),
                ]
            # Replan: step1 id changed, but title/meaning same.
            return [
                PlanStep(id="alpha", title="Снять метрики", instruction="do 1"),
                PlanStep(id="step2", title="Починить баг", instruction="do 2", depends_on=["alpha"]),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        executed = []

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            executed.append(step.id)
            if step.id == "step2" and len(executed) == 2:
                return ExecutorResponse(
                    task_id=step.id,
                    status="error",
                    summary="fail once",
                    outputs=[{"type": "text", "content": "boom"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"out {step.id}"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        out = await orch.run(session, "do things", bot=None, context=None, dest=dest)
        assert "done step1" in out
        # step1 must NOT be executed twice after replanning, even if planner renamed it to "alpha".
        assert executed.count("step1") == 1
        assert "alpha" not in executed
        # Planner should be called twice: initial + after error.
        assert calls["n"] == 2

    asyncio.run(_run())


def test_orchestrator_allows_more_than_three_clarifications(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg, max_clarifications=35)
        calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            if calls["n"] <= 4:
                return [
                    PlanStep(
                        id=f"ask{calls['n']}",
                        title=f"clarify {calls['n']}",
                        instruction="ask",
                        step_type="ask_user",
                        ask_question="Q?",
                        ask_options=["A", "B"],
                    )
                ]
            return [PlanStep(id="final", title="done", instruction="do final")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            content = "final" if step.id == "final" else f"ans-{step.id}"
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": content}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "need analysis", bot=None, context=None, dest=dest)

        assert "⚠️ Слишком много уточнений" not in out
        assert "done final" in out
        assert calls["n"] == 5

    asyncio.run(_run())


def test_orchestrator_default_clarification_limit_is_three(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            return [
                PlanStep(
                    id=f"ask{calls['n']}",
                    title=f"clarify {calls['n']}",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Q?",
                    ask_options=["A", "B"],
                )
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"ans-{step.id}"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "need analysis", bot=None, context=None, dest=dest)

        assert out == "⚠️ Слишком много уточнений. Остановлено."
        # Initial plan + 3 successful уточнения + 4th triggers stop.
        assert calls["n"] == 4

    asyncio.run(_run())


def test_orchestrator_can_continue_without_more_questions_after_limit(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg, max_clarifications=2, continue_without_clarifications=True)
        calls = {"n": 0}
        executed = []

        async def _fake_plan_steps(_cfg, user_text, _ctx):
            calls["n"] += 1
            if "лимит уточнений исчерпан" not in user_text:
                return [
                    PlanStep(
                        id=f"ask{calls['n']}",
                        title=f"clarify {calls['n']}",
                        instruction="ask",
                        step_type="ask_user",
                        ask_question="Q?",
                        ask_options=["A", "B"],
                    )
                ]
            return [PlanStep(id="final", title="done", instruction="do final")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            executed.append(step.id)
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"ans-{step.id}"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "need analysis", bot=None, context=None, dest=dest)

        assert out != "⚠️ Слишком много уточнений. Остановлено."
        assert "done final" in out
        assert "final" in executed

    asyncio.run(_run())


def test_orchestrator_replans_when_user_chose_clarify_now_option(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    PlanStep(
                        id="ask1",
                        title="clarify",
                        instruction="ask",
                        step_type="ask_user",
                        ask_question="Q?",
                        ask_options=["Да, продолжай", "Нет, уточню сейчас"],
                    )
                ]
            return [PlanStep(id="final", title="done", instruction="do final")]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            if step.id == "ask1":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary=f"done {step.id}",
                    outputs=[{"type": "text", "content": "User selected: Нет, уточню сейчас"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "final"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "need analysis", bot=None, context=None, dest=dest)

        assert "done final" in out
        # Must replan after ask_user answer, since it affects the plan.
        assert calls["n"] == 2

    asyncio.run(_run())


def test_orchestrator_does_not_repeat_same_ask_user_step_after_replan(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)
        calls = {"n": 0}
        executed = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            # Important: return the SAME ask_user step id across replans.
            return [
                PlanStep(
                    id="ask1",
                    title="clarify",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Q?",
                    ask_options=["A", "B"],
                ),
                PlanStep(id="final", title="done", instruction="do final"),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            executed.append(step.id)
            if step.id == "ask1":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary=f"done {step.id}",
                    outputs=[{"type": "text", "content": "User selected: A"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": "final"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "need analysis", bot=None, context=None, dest=dest)

        assert "done final" in out
        # Must replan once after ask_user.
        assert calls["n"] == 2
        # But must NOT execute ask1 again after replan.
        assert executed.count("ask1") == 1
        assert executed.count("final") == 1

    asyncio.run(_run())
