import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep


def test_orchestrator_replans_on_success_when_checker_says_yes(tmp_path, monkeypatch):
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

        plan_calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            plan_calls["n"] += 1
            # Second planning call renames step1 -> alpha to ensure id stabilization works.
            if plan_calls["n"] == 1:
                return [
                    PlanStep(id="step1", title="Найти информацию", instruction="do 1"),
                    PlanStep(id="step2", title="Сделать работу", instruction="do 2", depends_on=["step1"]),
                ]
            return [
                PlanStep(id="alpha", title="Найти информацию", instruction="do 1"),
                PlanStep(id="step2", title="Сделать работу", instruction="do 2", depends_on=["alpha"]),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        checker_calls = {"n": 0}

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            # First check: demand replan. Second check: no.
            checker_calls["n"] += 1
            if checker_calls["n"] == 1:
                return '{"needs_replan": true, "reason": "Найдены новые факты, меняющие план"}'
            return '{"needs_replan": false, "reason": ""}'

        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

        executed = []

        async def _fake_execute_step(step, *_args, **_kwargs):
            executed.append(step.id)
            if step.id == "step1":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Выяснил: несовместимо, нужен другой подход",
                    outputs=[{"type": "text", "content": "Оказалось, что текущая схема несовместима."}],
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

        # Plan should be built twice because checker requested replan after step1 success.
        assert plan_calls["n"] == 2
        # step1 executed once, step2 executed once; alpha must not execute.
        assert executed == ["step1", "step2"]
        assert "done step2" in out

    asyncio.run(_run())
