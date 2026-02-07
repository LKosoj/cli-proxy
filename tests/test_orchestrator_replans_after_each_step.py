import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from agent.orchestrator import OrchestratorRunner
from agent.contracts import ExecutorResponse, PlanStep


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
            # Plan expands as we go. Step ids remain stable so the orchestrator can seed completed.
            if calls["n"] == 1:
                return [
                    PlanStep(id="step1", title="s1", instruction="do 1"),
                    PlanStep(id="step2", title="s2", instruction="do 2", depends_on=["step1"]),
                ]
            if calls["n"] == 2:
                return [
                    PlanStep(id="step1", title="s1", instruction="do 1"),
                    PlanStep(id="step2", title="s2", instruction="do 2", depends_on=["step1"]),
                    PlanStep(id="step3", title="s3", instruction="do 3", depends_on=["step2"]),
                ]
            return [
                PlanStep(id="step1", title="s1", instruction="do 1"),
                PlanStep(id="step2", title="s2", instruction="do 2", depends_on=["step1"]),
                PlanStep(id="step3", title="s3", instruction="do 3", depends_on=["step2"]),
            ]

        monkeypatch.setattr("agent.orchestrator.plan_steps", _fake_plan_steps)

        executed = []

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context):
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
        # We expect replans: initial + after step1 + after step2
        assert calls["n"] == 3

    asyncio.run(_run())
