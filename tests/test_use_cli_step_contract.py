import asyncio
import json

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.runtime.contracts import PlanStep
from modes.sdk.runtime import planner as planner_mod


def test_orchestrator_execute_step_use_cli_calls_tool_registry(tmp_path, monkeypatch):
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

        called = {"n": 0, "name": None, "args": None, "ctx": None}

        async def _fake_execute(name, args, ctx):
            called["n"] += 1
            called["name"] = name
            called["args"] = dict(args or {})
            called["ctx"] = dict(ctx or {})
            return {"success": True, "output": "cli ok"}

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)

        async def _boom(*args, **kwargs):
            raise AssertionError("executor.run must not be called for step_type=use_cli")

        monkeypatch.setattr(orch._executor, "run", _boom)

        # Keep dispatcher out of the way: return a simple "analyst" profile.
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
            },
        )()
        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        resp = await orch._execute_step(step, session, bot=None, context=None, dest=dest, orchestrator_context="ctx")
        assert resp.status == "ok"
        assert called["n"] == 1
        assert called["name"] == "use_cli"
        assert "do via cli" in str(called["args"]["task_text"] or "")
        assert "Режим analyst: это строго аналитическая, read-only работа." in str(called["args"]["task_text"] or "")
        # analyst_use_cli_timeout_sec (default 3600) is doubled for analyst mode
        assert called["ctx"]["tool_timeouts_ms"]["use_cli"] == 7_200_000

    asyncio.run(_run())


def test_orchestrator_execute_step_use_cli_treats_api_error_output_as_error(tmp_path, monkeypatch):
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

        async def _fake_execute(name, args, ctx):
            assert name == "use_cli"
            return {
                "success": True,
                "output": "[API Error: Qwen API quota exceeded: Your Qwen API quota has been exhausted.]",
            }

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
            },
        )()
        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        resp = await orch._execute_step(step, session, bot=None, context=None, dest=dest, orchestrator_context="ctx")
        assert resp.status == "error"
        assert "quota exceeded" in resp.summary.lower()

    asyncio.run(_run())


def test_orchestrator_execute_step_use_cli_preserves_structured_tool_outputs(tmp_path, monkeypatch):
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

        repo_file = tmp_path / "views" / "header.blade.php"
        repo_file.parent.mkdir(parents=True, exist_ok=True)
        repo_file.write_text("<div>header</div>", encoding="utf-8")

        async def _fake_execute(name, args, ctx):
            assert name == "use_cli"
            del args, ctx
            return {
                "success": True,
                "output": "cli ok",
                "outputs": [
                    {"type": "text", "content": "cli ok"},
                    {"type": "repo_evidence", "path": str(repo_file), "content_preview": "read_file: header.blade.php"},
                ],
            }

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
            },
        )()
        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        resp = await orch._execute_step(step, session, bot=None, context=None, dest=dest, orchestrator_context="ctx")
        assert resp.status == "ok"
        assert any(str(item.get("path") or "") == str(repo_file) for item in resp.outputs)

    asyncio.run(_run())


def test_orchestrator_execute_step_use_cli_preserves_structured_claims(tmp_path, monkeypatch):
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

        repo_file = tmp_path / "views" / "header.blade.php"
        repo_file.parent.mkdir(parents=True, exist_ok=True)
        repo_file.write_text("<div>header</div>", encoding="utf-8")

        async def _fake_execute(name, args, ctx):
            assert name == "use_cli"
            del args, ctx
            return {
                "success": True,
                "output": "- В header есть account dropdown.\n- Меню содержит login CTA.",
                "outputs": [
                    {"type": "text", "content": "- В header есть account dropdown.\n- Меню содержит login CTA."},
                    {"type": "repo_evidence", "path": str(repo_file), "content_preview": "read_file: header.blade.php"},
                ],
                "claims": [
                    {
                        "claim_id": "claim_1",
                        "status": "confirmed",
                        "text": "В header есть account dropdown.",
                        "evidence": [{"type": "repo_evidence", "path": str(repo_file), "preview": "read_file: header.blade.php"}],
                    },
                    {
                        "claim_id": "claim_2",
                        "status": "confirmed",
                        "text": "Меню содержит login CTA.",
                        "evidence": [{"type": "repo_evidence", "path": str(repo_file), "preview": "read_file: header.blade.php"}],
                    },
                ],
            }

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
            },
        )()
        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        resp = await orch._execute_step(step, session, bot=None, context=None, dest=dest, orchestrator_context="ctx")
        assert resp.status == "ok"
        claim_texts = [str(item.get("text") or "") for item in resp.claims]
        assert "В header есть account dropdown." in claim_texts
        assert "Меню содержит login CTA." in claim_texts
        assert any(str(item.get("path") or "") == str(repo_file) for claim in resp.claims for item in (claim.get("evidence") or []))

    asyncio.run(_run())


def test_orchestrator_execute_step_use_cli_preserves_full_summary_output(tmp_path, monkeypatch):
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

        long_output = "B" * 1200

        async def _fake_execute(name, args, ctx):
            assert name == "use_cli"
            del args, ctx
            return {"success": True, "output": long_output}

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
            },
        )()
        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        resp = await orch._execute_step(step, session, bot=None, context=None, dest=dest, orchestrator_context="ctx")
        assert resp.status == "ok"
        assert resp.summary == long_output

    asyncio.run(_run())


def test_orchestrator_execute_step_use_cli_forwards_response_format(tmp_path, monkeypatch):
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

        captured = {"args": None}

        async def _fake_execute(name, args, ctx):
            assert name == "use_cli"
            del ctx
            captured["args"] = dict(args or {})
            return {"success": True, "output": "cli ok", "claims": [], "outputs": [{"type": "text", "content": "cli ok"}]}

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        setattr(step, "_use_cli_response_format", CLIResponseFormat.CLAIM_BUNDLE_JSON)
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
            },
        )()

        await orch._execute_step(
            step,
            session,
            bot=None,
            context=None,
            dest={"chat_id": 1, "chat_type": "private"},
            orchestrator_context="ctx",
        )
        assert captured["args"]["response_format"] == CLIResponseFormat.CLAIM_BUNDLE_JSON

    asyncio.run(_run())


def test_orchestrator_use_cli_task_text_includes_user_request_and_clarification_answers(tmp_path, monkeypatch):
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

        called = {"args": None}

        async def _fake_execute(name, args, ctx):
            assert name == "use_cli"
            called["args"] = dict(args or {})
            return {"success": True, "output": "cli ok"}

        monkeypatch.setattr(orch._tool_registry, "execute", _fake_execute)
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)

        state_path = tmp_path / "RUN_STATE.json"
        state_path.write_text(
            json.dumps(
                {
                    "mode_context": {
                        "execution_context": {
                            "user_text_preview": "Что необходимо улучшить на сайте?",
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        session = type(
            "S",
            (),
            {
                "id": "s1",
                "executor_profile": "analyst",
                "project_root": str(tmp_path),
                "workdir": str(tmp_path),
                "analyst_run_artifact_handle": type("H", (), {"state_path": str(state_path)})(),
            },
        )()
        step = PlanStep(id="u1", title="cli", instruction="do via cli", step_type="use_cli")
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}

        resp = await orch._execute_step(
            step,
            session,
            bot=None,
            context=None,
            dest=dest,
            orchestrator_context="ctx",
            current_user_text=(
                "Ты работаешь в режиме Аналитик.\n"
                "Ответ пользователя: Главный приоритет — конверсия\n"
                "Ответ пользователя: Мобильные пользователи важнее десктопа"
            ),
        )
        assert resp.status == "ok"
        task_text = str(called["args"]["task_text"] or "")
        assert "do via cli" in task_text
        assert "Режим analyst: это строго аналитическая, read-only работа." in task_text
        assert "Если пользователь просит доработку, дообогащение функционала или дает внешний референс" in task_text
        assert "Исходный запрос пользователя" in task_text
        assert "Что необходимо улучшить на сайте?" in task_text
        assert "Полученные уточнения пользователя" in task_text
        assert "Главный приоритет — конверсия" in task_text
        assert "Мобильные пользователи важнее десктопа" in task_text

    asyncio.run(_run())


def test_required_repo_use_cli_steps_consider_historical_replan_steps(tmp_path):
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
    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
        },
    )()

    historical_steps = {
        "use_cli_repo_grounding": PlanStep(
            id="use_cli_repo_grounding",
            title="Repo grounding",
            instruction=f"Сделай базовый repo-grounded анализ репозитория через CLI в директории:\n{tmp_path}",
            step_type="use_cli",
        )
    }
    current_steps = [
        PlanStep(
            id="step3",
            title="Продолжить анализ",
            instruction="do more",
            step_type="task",
        )
    ]

    missing = orch._missing_required_repo_use_cli_step_ids(
        session,
        {"use_cli_repo_grounding"},
        steps=current_steps,
        historical_steps=historical_steps,
    )

    assert missing == []


def test_required_repo_use_cli_steps_consider_historical_completed_ok_outside_current_plan(tmp_path):
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
    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "audit",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
        },
    )()

    historical_steps = {
        "use_cli_repo_audit": PlanStep(
            id="use_cli_repo_audit",
            title="Repo audit",
            instruction=f"Сделай начальный аудит репозитория через CLI в директории:\n{tmp_path}",
            step_type="use_cli",
        )
    }
    current_steps = [
        PlanStep(
            id="compile_final_analysis",
            title="Собрать финальный анализ",
            instruction="compile",
            step_type="task",
        )
    ]

    missing = orch._missing_required_repo_use_cli_step_ids(
        session,
        set(),
        steps=current_steps,
        historical_steps=historical_steps,
        historical_completed_ok={"use_cli_repo_audit"},
    )

    assert missing == []


def test_planner_enforces_flagged_use_cli_repo_audit_and_final_review(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return (
                "{"
                "\"steps\":["
                "{"
                "\"id\":\"step1\","
                "\"title\":\"Собрать данные\","
                "\"instruction\":\"collect\","
                "\"step_type\":\"task\","
                "\"parallel_group\":null,"
                "\"depends_on\":[],"
                "\"parallelizable\":false,"
                "\"parallelizable_reason\":null,"
                "\"ask_question\":null,"
                "\"ask_options\":null"
                "}"
                "]"
                "}"
            )

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "spec",
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}"
        )
        steps = await planner_mod.plan_steps(cfg, "Подготовь ТЗ на доработку", context)
        step_ids = [s.id for s in steps]

        assert "use_cli_repo_audit" in step_ids
        assert "use_cli_repo_final_review" in step_ids
        assert step_ids.count("use_cli_repo_audit") == 1
        assert step_ids.count("use_cli_repo_final_review") == 1
        assert any(s.step_type == "use_cli" and s.id == "use_cli_repo_audit" for s in steps)
        assert any(s.step_type == "use_cli" and s.id == "use_cli_repo_final_review" for s in steps)

    asyncio.run(_run())


def test_planner_enforces_base_use_cli_for_repo_grounded_analysis(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return '{"steps":[]}'

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}"
        )
        steps = await planner_mod.plan_steps(cfg, "Подготовь repo-grounded анализ", context)
        step_ids = [s.id for s in steps]

        assert "use_cli_repo_grounding" in step_ids
        repo_step = next(s for s in steps if s.id == "use_cli_repo_grounding")
        assert repo_step.step_type == "use_cli"
        assert str(tmp_path) in repo_step.instruction

    asyncio.run(_run())


def test_planner_flagged_use_cli_enforcement_is_idempotent_with_prior_steps(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return (
                "{"
                "\"steps\":["
                "{"
                "\"id\":\"step1\","
                "\"title\":\"Собрать данные\","
                "\"instruction\":\"collect\","
                "\"step_type\":\"task\","
                "\"parallel_group\":null,"
                "\"depends_on\":[],"
                "\"parallelizable\":false,"
                "\"parallelizable_reason\":null,"
                "\"ask_question\":null,"
                "\"ask_options\":null"
                "}"
                "]"
                "}"
            )

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "audit",
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prior = json.dumps(
            [
                {"id": "use_cli_repo_audit", "title": "audit", "step_type": "use_cli", "status": "ok"},
                {
                    "id": "use_cli_repo_final_review",
                    "title": "final-review",
                    "step_type": "use_cli",
                    "status": "ok",
                },
            ],
            ensure_ascii=False,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}\n"
            f"prior_steps:\n{prior}"
        )
        steps = await planner_mod.plan_steps(cfg, "Сделай аудит и финальный review", context)
        step_ids = [s.id for s in steps]

        assert "use_cli_repo_audit" not in step_ids
        assert "use_cli_repo_final_review" not in step_ids

    asyncio.run(_run())


def test_planner_repo_grounding_enforcement_is_idempotent_with_prior_steps(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return '{"steps":[]}'

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prior = json.dumps(
            [
                {"id": "use_cli_repo_grounding", "title": "grounding", "step_type": "use_cli", "status": "ok"},
            ],
            ensure_ascii=False,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}\n"
            f"prior_steps:\n{prior}"
        )
        steps = await planner_mod.plan_steps(cfg, "Перепланируй repo-grounded анализ", context)
        step_ids = [s.id for s in steps]

        assert "use_cli_repo_grounding" not in step_ids

    asyncio.run(_run())


def test_planner_rewrites_invalid_reserved_repo_step_ids_into_repo_grounded_use_cli_steps(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return json.dumps(
                {
                    "steps": [
                        {
                            "id": "use_cli_repo_audit",
                            "title": "audit",
                            "instruction": "невалидный шаг без repo root",
                            "step_type": "task",
                            "parallel_group": None,
                            "depends_on": [],
                            "parallelizable": False,
                            "parallelizable_reason": None,
                            "ask_question": None,
                            "ask_options": None,
                        },
                        {
                            "id": "use_cli_repo_final_review",
                            "title": "review",
                            "instruction": "ещё один невалидный шаг",
                            "step_type": "task",
                            "parallel_group": None,
                            "depends_on": [],
                            "parallelizable": False,
                            "parallelizable_reason": None,
                            "ask_question": None,
                            "ask_options": None,
                        },
                    ]
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "spec",
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}"
        )
        steps = await planner_mod.plan_steps(cfg, "Подготовь ТЗ на доработку", context)
        audit_step = next(step for step in steps if step.id == "use_cli_repo_audit")
        final_step = next(step for step in steps if step.id == "use_cli_repo_final_review")

        assert audit_step.step_type == "use_cli"
        assert str(tmp_path) in audit_step.instruction
        assert final_step.step_type == "use_cli"
        assert str(tmp_path) in final_step.instruction

    asyncio.run(_run())


def test_planner_does_not_trust_prior_repo_step_ids_with_wrong_step_type(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return '{"steps":[]}'

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "audit",
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prior = json.dumps(
            [
                {"id": "use_cli_repo_audit", "title": "audit", "step_type": "task", "status": "ok"},
                {"id": "use_cli_repo_final_review", "title": "review", "step_type": "task", "status": "ok"},
            ],
            ensure_ascii=False,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}\n"
            f"prior_steps:\n{prior}"
        )
        steps = await planner_mod.plan_steps(cfg, "Сделай аудит и финальный review", context)
        step_ids = [step.id for step in steps]

        assert "use_cli_repo_audit" in step_ids
        assert "use_cli_repo_final_review" in step_ids
        assert all(
            step.step_type == "use_cli"
            for step in steps
            if step.id in {"use_cli_repo_audit", "use_cli_repo_final_review"}
        )

    asyncio.run(_run())


def test_planner_reinjects_repo_grounding_when_prior_step_status_is_not_ok(tmp_path, monkeypatch):
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

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            return '{"steps":[]}'

        monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)

        flags = json.dumps(
            {
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prior = json.dumps(
            [
                {"id": "use_cli_repo_grounding", "title": "grounding", "step_type": "use_cli", "status": "partial"},
            ],
            ensure_ascii=False,
        )
        context = (
            f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
            f"analyst_intent_flags:\n{flags}\n"
            f"prior_steps:\n{prior}"
        )
        steps = await planner_mod.plan_steps(cfg, "Перепланируй repo-grounded анализ", context)
        step_ids = [s.id for s in steps]

        assert "use_cli_repo_grounding" in step_ids

    asyncio.run(_run())
