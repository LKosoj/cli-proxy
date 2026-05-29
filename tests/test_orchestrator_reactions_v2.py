import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
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


def test_orchestrator_uses_reaction_engine_for_retry(tmp_path, monkeypatch):
    async def _run():
        orch = OrchestratorRunner(_cfg(tmp_path))
        plan_calls = {"n": 0}
        execute_calls = {"n": 0}
        reaction_calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            plan_calls["n"] += 1
            if plan_calls["n"] == 1:
                return [PlanStep(id="step_fail", title="fail", instruction="do fail")]
            return [PlanStep(id="step_ok", title="ok", instruction="do ok")]

        async def _fake_execute_step(step, *_args, **_kwargs):
            execute_calls["n"] += 1
            if step.id == "step_fail":
                return ExecutorResponse(
                    task_id=step.id,
                    status="error",
                    summary="fail once",
                    outputs=[],
                    tool_calls=[],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[],
                tool_calls=[],
                next_questions=[],
            )

        async def _fake_reactions_execute(event, rules, *, ctx=None):  # noqa: ANN001, ARG001
            reaction_calls["n"] += 1
            assert event.event_type.value == "STEP_FAILED"
            return [{"action": "retry_step", "status": "queued"}]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._reaction_engine, "execute", _fake_reactions_execute)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "do things", bot=None, context=None, dest=dest)

        assert "done" in out
        assert reaction_calls["n"] >= 1
        assert plan_calls["n"] == 2
        assert execute_calls["n"] == 2

    asyncio.run(_run())
