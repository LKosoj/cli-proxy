import asyncio
import json
import logging
import re
from pathlib import Path

import modes.sdk.orchestrator_runner as orchestrator_runner_module
from app.services.run_artifact_store import RunArtifactStore
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.orchestrator_runner import (
    OrchestratorRunner,
    _select_final_repo_review_draft_seed,
)
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep


def _make_orchestrator(tmp_path):
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
    return OrchestratorRunner(cfg)


def test_select_final_repo_review_draft_seed_prefers_document_artifact_over_review_notes(tmp_path):
    synth_draft = tmp_path / "synthesize-final-tz_use-cli_output_4.md"
    synth_draft.write_text(
        "Черновик переноса сессий.\n\n"
        "Нужно добавить codex reader и writer.\n",
        encoding="utf-8",
    )
    validate_notes = tmp_path / "validate-notes.md"
    validate_notes.write_text(
        "ТЗ в целом хорошее, но нужно внести несколько корректировок перед передачей.",
        encoding="utf-8",
    )

    text, path = _select_final_repo_review_draft_seed(
        [
            {
                "task_id": "synthesize_final_tz",
                "outputs": [{"path": str(synth_draft)}],
            },
            {
                "task_id": "validate_tz_completeness",
                "outputs": [{"path": str(validate_notes)}],
            },
        ]
    )

    assert path == str(synth_draft)
    assert "codex reader" in text


def test_orchestrator_reuses_existing_draft_for_final_repo_review_prep(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        compose_calls = {"n": 0}
        captured = {"final_review_instruction": ""}
        draft_seed_path = tmp_path / "synthesize-final-tz_use-cli_output_4.md"
        draft_seed_path.write_text(
            "Черновик реализации переноса сессий.\n\n"
            "Нужно реализовать codex session import/export.\n",
            encoding="utf-8",
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(id="synthesize_final_tz", title="Draft", instruction="draft", step_type="use_cli"),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="Final review",
                    instruction="review",
                    step_type="use_cli",
                    depends_on=["synthesize_final_tz"],
                ),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            if step.id == "synthesize_final_tz":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="draft ready",
                    outputs=[{"type": "file", "path": str(draft_seed_path), "name": draft_seed_path.name}],
                    tool_calls=[{"tool": "use_cli"}],
                    next_questions=[],
                )
            captured["final_review_instruction"] = str(step.instruction or "")
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="review ok",
                outputs=[{"type": "repo_review_verdict", "content": "review ok", "content_preview": "review ok"}],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if "Материалы (JSON):" in str(_user or ""):
                compose_calls["n"] += 1
                return (
                    "## Результат\n\n"
                    "Готово.\n\n"
                    "## Детали\n\n"
                    "- Документ собран.\n\n"
                    "## Как проверить\n\n"
                    "- Проверить шаги.\n\n"
                    "## Что не удалось\n\n"
                    "- Нет.\n\n"
                    "## Нужно от вас\n\n"
                    "- Нет.\n\n"
                    "## Допущения\n\n"
                    "- Нет.\n"
                )
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "FINAL"

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1", "workdir": str(tmp_path), "project_root": str(tmp_path)})()
        out = await orch.run(session, "Собери ТЗ", bot=None, context=None, dest={})

        assert "draft ready" in out
        assert compose_calls["n"] == 0
        assert "_repo_final_review_draft.md" in captured["final_review_instruction"]
        match = re.search(r"Файл черновика ТЗ:\n(.+?)\n\n", captured["final_review_instruction"], re.S)
        assert match is not None
        repo_review_draft = Path(match.group(1).strip()).read_text(encoding="utf-8")
        assert "codex session import/export" in repo_review_draft

    asyncio.run(_run())


def test_orchestrator_prepares_final_repo_review_draft_from_bounded_fallback_without_compose(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        compose_calls = {"n": 0}
        captured = {"final_review_instruction": ""}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(id="analysis", title="Analysis", instruction="analyze"),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="Final review",
                    instruction="review",
                    step_type="use_cli",
                    depends_on=["analysis"],
                ),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            if step.id == "analysis":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Собран bounded draft для финальной сверки.",
                    outputs=[],
                    tool_calls=[{"tool": "use_cli"}],
                    next_questions=[],
                )
            captured["final_review_instruction"] = str(step.instruction or "")
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="review ok",
                outputs=[{"type": "repo_review_verdict", "content": "review ok", "content_preview": "review ok"}],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if "Материалы (JSON):" in str(_user or ""):
                compose_calls["n"] += 1
                return "UNEXPECTED COMPOSE"
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "FINAL"

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "s1", "workdir": str(tmp_path), "project_root": str(tmp_path)})()
        out = await orch.run(session, "Собери ТЗ", bot=None, context=None, dest={})

        assert "bounded draft" in out
        assert compose_calls["n"] == 0
        assert "_repo_final_review_draft.md" in captured["final_review_instruction"]
        match = re.search(r"Файл черновика ТЗ:\n(.+?)\n\n", captured["final_review_instruction"], re.S)
        assert match is not None
        repo_review_draft = Path(match.group(1).strip()).read_text(encoding="utf-8")
        assert "Собран bounded draft" in repo_review_draft

    asyncio.run(_run())


def test_execute_step_forces_analyst_task_profile_through_use_cli(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        step = PlanStep(id="repo_grounding", title="Grounding", instruction="collect facts")
        session = type("S", (), {"id": "sess-analyst", "active_mode": "analyst"})()
        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})()
        calls = {}

        async def _fake_execute_use_cli_step(
            step_arg,
            session_arg,
            bot,
            context,
            dest,
            orchestrator_context,
            *,
            current_user_text,
            profile,
            corr_id,
            constraints=None,
        ):
            del session_arg, bot, context, dest, orchestrator_context, constraints
            calls["step_type"] = step_arg.step_type
            calls["profile"] = profile.name
            calls["corr_id"] = corr_id
            calls["current_user_text"] = current_user_text
            return ExecutorResponse(
                task_id=step_arg.id,
                status="ok",
                summary="done",
                outputs=[],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda _step, _session: profile)
        monkeypatch.setattr(orch, "_execute_use_cli_step", _fake_execute_use_cli_step)

        resp = await orch._execute_step(
            step,
            session,
            bot=None,
            context=None,
            dest={},
            orchestrator_context="ctx",
            current_user_text="source prompt",
        )

        assert resp.status == "ok"
        assert step.step_type == "use_cli"
        assert calls == {
            "step_type": "use_cli",
            "profile": "analyst",
            "corr_id": "sess-analyst:repo_grounding",
            "current_user_text": "source prompt",
        }

    asyncio.run(_run())


def test_replan_retry_semantics(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)

        plan_calls = {"n": 0}
        planner_contexts = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            plan_calls["n"] += 1
            planner_contexts.append(_ctx)
            if plan_calls["n"] == 1:
                return [
                    PlanStep(id="prep", title="Подготовить контекст", instruction="prep"),
                    PlanStep(
                        id="step_a",
                        title="Починить проблему",
                        instruction="fix",
                        depends_on=["prep"],
                    ),
                    PlanStep(
                        id="step_b",
                        title="Проверить результат",
                        instruction="verify",
                        depends_on=["step_a"],
                    ),
                ]
            return [
                PlanStep(id="prep2", title="Подготовить контекст", instruction="prep"),
                PlanStep(
                    id="alpha",
                    title="Починить проблему",
                    instruction="fix",
                    depends_on=["prep2"],
                ),
                PlanStep(
                    id="step_b",
                    title="Проверить результат",
                    instruction="verify",
                    depends_on=["alpha"],
                ),
            ]

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

        retry_calls = {"n": 0}

        async def _fake_retry(**_kwargs):
            retry_calls["n"] += 1
            return retry_calls["n"] == 1

        monkeypatch.setattr(orch, "_should_retry_via_reactions", _fake_retry)

        executed = []
        attempts = {"step_a": 0}

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, constraints
            executed.append(step.id)
            if step.id == "prep":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="prep done",
                    outputs=[{"type": "text", "content": "prep done"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            if step.id == "step_a":
                attempts["step_a"] += 1
                if attempts["step_a"] == 1:
                    return ExecutorResponse(
                        task_id=step.id,
                        status="error",
                        summary="step_a failed once",
                        outputs=[{"type": "text", "content": "temporary failure"}],
                        tool_calls=[{"tool": "fake"}],
                        next_questions=[],
                    )
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="step_a recovered",
                    outputs=[{"type": "text", "content": "recovered"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            if step.id == "step_b":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="step_b done",
                    outputs=[{"type": "text", "content": "verified"}],
                    tool_calls=[{"tool": "fake"}],
                    next_questions=[],
                )
            raise AssertionError(f"unexpected step id: {step.id}")

        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "sess-1"})
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        out = await orch.run(session, "fix issue", bot=None, context=None, dest=dest)

        assert plan_calls["n"] == 2
        assert executed == ["prep", "step_a", "step_a", "step_b"]
        assert executed.count("prep") == 1
        assert "prep2" not in executed
        assert "alpha" not in executed
        assert "step_b done" in out

        assert len(planner_contexts) == 2
        assert "step_results_so_far" in planner_contexts[1]
        assert "step_a failed once" in planner_contexts[1]

        session_path = Path(tmp_path) / "_sandbox" / "chats" / "chat_101" / "SESSION.json"
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        run_entry = payload["orchestrator_by_task"]["sess-1"][-1]
        step_results = run_entry["step_results"]
        step_a_history = [item for item in step_results if item.get("task_id") == "step_a"]

        assert [item["status"] for item in step_a_history] == ["error", "ok"]
        assert any(item.get("summary") == "step_a failed once" for item in step_results)
        assert any(item.get("summary") == "step_a recovered" for item in step_results)
        assert not any(
            item.get("task_id") == "step_b" and item.get("status") == "blocked"
            for item in step_results
        )

    asyncio.run(_run())


def test_orchestrator_skips_post_success_replan_check_for_stable_deterministic_analyst_plan(
    tmp_path,
    monkeypatch,
):
    async def _run():
        class _Bot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        def _template_provider(_session):
            return {
                "name": "Spec template",
                "required_sections": ["Контекст", "Требования"],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(
                    id="use_cli_repo_audit",
                    title="Начальный аудит репозитория через CLI",
                    instruction="repo audit",
                    step_type="use_cli",
                ),
                PlanStep(
                    id="analyze_external_reference",
                    title="Отдельный анализ внешнего референса",
                    instruction="analyze external reference",
                    depends_on=["use_cli_repo_audit"],
                ),
                PlanStep(
                    id="synthesize_final_tz",
                    title="Синтез финального ТЗ",
                    instruction="synthesize final tz",
                    step_type="use_cli",
                    depends_on=["analyze_external_reference"],
                ),
                PlanStep(
                    id="validate_tz_completeness",
                    title="Валидация полноты и трассируемости ТЗ",
                    instruction="validate tz",
                    step_type="use_cli",
                    depends_on=["synthesize_final_tz"],
                ),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="Финальный second-opinion review репозитория через CLI",
                    instruction="repo final review",
                    step_type="use_cli",
                    depends_on=["validate_tz_completeness"],
                ),
            ]

        executed: list[str] = []
        replan_prompts: list[str] = []

        async def _fake_execute_step(
            step,
            session,
            bot,
            context,
            dest,
            orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            executed.append(step.id)
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"{step.id} done",
                outputs=[{"type": "text", "content": f"{step.id} evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, user, response_format=None, **_kwargs):
            if "Последний результат" in str(user or ""):
                replan_prompts.append(str(user))
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Зафиксировать изменения."

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-deterministic-analyst-plan",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "active_mode": "analyst",
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": True,
                    "requires_final_repo_review": True,
                    "requires_repo_audit": True,
                },
            },
        )()

        out = await orch.run(
            session,
            "Подготовь implementation-ready spec по deterministic analyst plan",
            bot=_FakeBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert executed == [
            "use_cli_repo_audit",
            "analyze_external_reference",
            "synthesize_final_tz",
            "validate_tz_completeness",
            "use_cli_repo_final_review",
        ]
        assert replan_prompts == []
        assert "# Техническое задание" in out

    asyncio.run(_run())


def test_orchestrator_skips_post_success_replan_after_validate_step_for_repo_grounded_analyst_flow(
    tmp_path,
    monkeypatch,
):
    async def _run():
        class _Bot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        def _template_provider(_session):
            return {
                "name": "Spec template",
                "required_sections": ["Контекст", "Требования"],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "repo_grounded_required": True,
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(id="step1", title="repo collect", instruction="collect", step_type="use_cli"),
                PlanStep(
                    id="validate_tz_completeness",
                    title="validate tz",
                    instruction="validate",
                    step_type="use_cli",
                    depends_on=["step1"],
                ),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="repo final review",
                    instruction="review",
                    step_type="use_cli",
                    depends_on=["validate_tz_completeness"],
                ),
            ]

        executed: list[str] = []
        replan_prompts: list[str] = []

        async def _fake_execute_step(
            step,
            session,
            bot,
            context,
            dest,
            orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            executed.append(step.id)
            summary = f"{step.id} done"
            if step.id == "validate_tz_completeness":
                summary = "Валидация обнаружила новые требования и нужно поменять формулировки в документе."
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=summary,
                outputs=[{"type": "text", "content": f"{step.id} evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, user, response_format=None, **_kwargs):
            if "План (включая уже выполненные):" in str(user or ""):
                replan_prompts.append(str(user))
                return '{"needs_replan": true, "reason": "should not be called for validate step"}'
            if response_format is not None:
                return '{"final_text":"## Контекст\\nПодтвержденный контекст.\\n\\n## Требования\\n- Зафиксировать изменения."}'
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Зафиксировать изменения."

        _fake_chat_completion._supports_strict_json_contract = True
        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-validate-no-replan",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "active_mode": "analyst",
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": True,
                },
            },
        )()

        out = await orch.run(
            session,
            "Подготовь implementation-ready spec по analyst flow",
            bot=_Bot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert executed == ["step1", "validate_tz_completeness", "use_cli_repo_final_review"]
        assert replan_prompts == []
        assert "# Техническое задание" in out

    asyncio.run(_run())


def test_next_batch_blocks_when_dependency_not_restored_in_current_plan(tmp_path):
    orch = _make_orchestrator(tmp_path)
    steps = [
        PlanStep(
            id="step_b",
            title="Проверить результат",
            instruction="verify",
            depends_on=["step_a"],
        )
    ]

    batch, skipped = orch._next_batch(
        steps=steps,
        completed_ok=set(),
        completed_fail=set(),
        session_id="sess-1",
        historical_non_success={"step_a"},
    )

    assert batch == []
    assert len(skipped) == 1
    blocked = skipped[0]
    assert blocked.task_id == "step_b"
    assert blocked.status == "blocked"
    assert "step_a" in blocked.summary
    assert blocked.tool_calls
    assert blocked.tool_calls[0]["error"] == "dependency_missing_in_plan"
    assert blocked.tool_calls[0]["historical_dependencies"] == ["step_a"]


def test_next_batch_allows_dependency_satisfied_by_historical_success(tmp_path):
    orch = _make_orchestrator(tmp_path)
    steps = [
        PlanStep(
            id="step_b",
            title="Проверить результат",
            instruction="verify",
            depends_on=["step_a"],
        )
    ]

    batch, skipped = orch._next_batch(
        steps=steps,
        completed_ok=set(),
        completed_fail=set(),
        session_id="sess-1",
        historical_success={"step_a"},
        historical_non_success=set(),
    )

    assert skipped == []
    assert len(batch) == 1
    assert batch[0].id == "step_b"


def test_new_top_level_run_does_not_feed_persisted_orchestrator_history_into_planner(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        session = type("S", (), {"id": "sess-1"})
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        session_path = Path(tmp_path) / "_sandbox" / "chats" / "chat_101" / "SESSION.json"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            json.dumps(
                {
                    "orchestrator_by_task": {
                        "sess-1": [
                            {
                                "date": "2026-03-31",
                                "user": "old run",
                                "context": "old ctx",
                                "steps": [
                                    {
                                        "id": "step1",
                                        "title": "Старый шаг 1",
                                        "step_type": "task",
                                        "depends_on": [],
                                    },
                                    {
                                        "id": "step2",
                                        "title": "Старый шаг 2",
                                        "step_type": "task",
                                        "depends_on": ["step1"],
                                    },
                                ],
                                "results": ["old summary"],
                                "step_results": [
                                    {
                                        "task_id": "step1",
                                        "title": "Старый шаг 1",
                                        "step_type": "task",
                                        "status": "ok",
                                        "summary": "done",
                                    }
                                ],
                                "final": "old final",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        planner_contexts = []

        async def _fake_plan_steps(_cfg, _user_text, ctx):
            planner_contexts.append(ctx)
            return [PlanStep(id="step1", title="Новый шаг", instruction="do work")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "done"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        out = await orch.run(session, "fresh run", bot=None, context=None, dest=dest)

        assert "done" in out
        assert len(planner_contexts) == 1
        assert "orchestrator_history:" not in planner_contexts[0]
        assert "prior_steps:" not in planner_contexts[0]

    asyncio.run(_run())


def test_orchestrator_pauses_on_needs_input_without_final_blocked(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        sent_outputs = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнение",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                ),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="Final review",
                    instruction="review",
                    step_type="use_cli",
                    depends_on=["ask1"],
                ),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            if step.id == "ask1":
                return ExecutorResponse(
                    task_id="ask1",
                    status="needs_input",
                    summary="Нужен ответ пользователя",
                    outputs=[],
                    tool_calls=[{"tool": "ask_user"}],
                    next_questions=["Какой вариант нужен?"],
                )
            raise AssertionError("unexpected step execution after needs_input pause")

        class _FakeBot:
            async def send_output(self, *_args, **_kwargs):
                sent_outputs.append(_kwargs)
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-1",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": True,
                    "document_kind": "spec",
                    "requires_codebase_grounding": True,
                    "requires_final_repo_review": True,
                    "requires_repo_audit": False,
                },
            },
        )()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        out = await orch.run(session, "fix issue", bot=_FakeBot(), context=None, dest=dest)

        assert out == "Какой вариант нужен?"
        assert sent_outputs == []

    asyncio.run(_run())


def test_orchestrator_calls_analyst_step_results_sync_hook_after_each_step(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        synced_payloads = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Сбор фактов", instruction="collect")]

        async def _fake_execute_step(
            step,
            session,
            bot,
            context,
            dest,
            orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "repo-grounded evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "sess-hook"})()
        session.analyst_step_results_sync_hook = (
            lambda step_results: synced_payloads.append([dict(item) for item in step_results])
        )
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        out = await orch.run(session, "collect evidence", bot=None, context=None, dest=dest)

        assert "done" in out
        assert len(synced_payloads) == 1
        assert len(synced_payloads[0]) == 1
        assert synced_payloads[0][0]["task_id"] == "step1"
        assert synced_payloads[0][0]["status"] == "ok"

    asyncio.run(_run())


def test_orchestrator_runner_emits_awaiting_input_without_finalization(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        trace_events = []
        orch_events = []

        original_build_trace_event = orchestrator_runner_module.build_trace_event

        def _spy_build_trace_event(event_type, *, mode_id="", session_id="", **kwargs):
            event = original_build_trace_event(
                event_type,
                mode_id=mode_id,
                session_id=session_id,
                **kwargs,
            )
            trace_events.append(dict(event))
            return event

        def _fake_emit_runtime_progress(session, payload):
            payload_dict = dict(payload or {})
            payload_dict["session_id"] = str(getattr(session, "id", "") or "")
            orch_events.append(payload_dict)

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнение",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                )
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            assert step.id == "ask1"
            return ExecutorResponse(
                task_id="ask1",
                status="needs_input",
                summary="Нужен ответ пользователя",
                outputs=[],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=["Какой вариант нужен?"],
            )

        monkeypatch.setattr(orchestrator_runner_module, "build_trace_event", _spy_build_trace_event)
        monkeypatch.setattr(orchestrator_runner_module, "emit_runtime_progress", _fake_emit_runtime_progress)
        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "sess-1"})()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        out = await orch.run(session, "fix issue", bot=None, context=None, dest=dest)

        assert out == "Какой вариант нужен?"
        assert any(
            item.get("event_type") == "awaiting_input" and item.get("status") == "needs_input"
            for item in trace_events
        )
        assert not any(item.get("event_type") == "run_finished" for item in trace_events)
        assert any(
            item.get("phase") == "awaiting_input"
            and item.get("status") == "needs_input"
            and item.get("message") == "Какой вариант нужен?"
            for item in orch_events
        )

    asyncio.run(_run())


def test_orchestrator_persists_ask_question_and_options_in_step_artifact(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)

        async def _fake_plan_steps(_cfg, user_text, _ctx):
            if "Ответ пользователя:" in user_text:
                return []
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнение",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                )
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            assert step.id == "ask1"
            return ExecutorResponse(
                task_id="ask1",
                status="ok",
                summary="Ответ получен",
                outputs=[{"type": "text", "content": "Ответ пользователя: A"}],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "FINAL"

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "sess-1"})()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(session, "fix issue", bot=_FakeBot(), context=None, dest=dest)

        assert out == "FINAL"
        artifact_path = Path(tmp_path) / "_sandbox" / "chats" / "chat_101" / "_orchestrator" / "ask1.md"
        artifact_text = artifact_path.read_text(encoding="utf-8")
        assert "- question: Какой вариант нужен?" in artifact_text
        assert "A" in artifact_text
        assert "B" in artifact_text

    asyncio.run(_run())


def test_orchestrator_treats_analyst_ask_user_as_blocking_even_when_flag_is_false(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        executed = []

        async def _fake_plan_steps(_cfg, user_text, _ctx):
            if "Ответ пользователя:" in user_text:
                raise AssertionError("blocking ask_user should pause instead of injecting clarification answers")
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнение",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                ),
                PlanStep(
                    id="final",
                    title="Продолжить работу",
                    instruction="do final",
                    depends_on=["ask1"],
                ),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            executed.append(step.id)
            if step.id != "ask1":
                raise AssertionError("final step must stay blocked behind ask_user")
            return ExecutorResponse(
                task_id=step.id,
                status="needs_input",
                summary="Нужен ответ пользователя",
                outputs=[],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=["Какой вариант нужен?"],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-analyst-ask-blocking",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                },
            },
        )()

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(
            session,
            "Сделай ТЗ и обязательно дождись ответа на уточняющий вопрос",
            bot=_FakeBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert out == "Какой вариант нужен?"
        assert executed == ["ask1"]
        artifact_path = Path(tmp_path) / "_sandbox" / "chats" / "chat_101" / "_orchestrator" / "ask1.md"
        artifact_text = artifact_path.read_text(encoding="utf-8")
        assert "status: needs_input" in artifact_text
        assert "- question: Какой вариант нужен?" in artifact_text

    asyncio.run(_run())


def test_orchestrator_final_qc_required_input_gaps_do_not_trigger_late_ask_user(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Spec with required inputs",
                "required_sections": ["Контекст", "Требования"],
                "required_inputs": ["Какие сценарии нельзя сломать"],
                "qa_prompt": "QA-REQUIRED-INPUTS",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=True,
            final_rework_passes=1,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return []

        seed_question_calls = {"count": 0}

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            if response_format is not None:
                if "Seed question:" in _user:
                    seed_question_calls["count"] += 1
                    return (
                        '{"ask_question": "Какие сценарии нужно сохранить без изменений?", '
                        '"ask_options": ["Только текущий основной flow", "Все текущие сценарии", "Нужно уточнить отдельно"]}'
                    )
                return (
                    '{"needs_rework": false, "issues": [], "missing_sections": [], '
                    '"required_input_gaps": ["Какие сценарии нельзя сломать"]}'
                )
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Реализовать поведение."

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-required-input-blocking",
                "active_mode": "analyst",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                    "required_inputs": ["Какие сценарии нельзя сломать"],
                },
            },
        )()

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(
            session,
            "Подготовь implementation-ready spec без уточнения сценариев",
            bot=_FakeBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert "Какие сценарии нужно сохранить без изменений?" not in out
        assert "## Контекст" in out
        assert getattr(session, "analyst_blocking_clarification_runtime", False) is False
        assert seed_question_calls["count"] == 0
        assert "Допущения и незакрытые входы" not in out

    asyncio.run(_run())


def test_orchestrator_final_qc_required_input_gaps_do_not_trigger_late_ask_user_without_protected_shell(
    tmp_path,
    monkeypatch,
):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Spec without protected shell",
                "required_sections": ["Контекст", "Требования"],
                "required_inputs": ["Какие сценарии нельзя сломать"],
                "qa_prompt": "QA-REQUIRED-INPUTS",
                "output_kind": "spec",
                "compose_mode": "template_first",
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=True,
            final_rework_passes=1,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return []

        seed_question_calls = {"count": 0}

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            if response_format is not None:
                if "Seed question:" in _user:
                    seed_question_calls["count"] += 1
                    return (
                        '{"ask_question": "Какие сценарии нужно сохранить без изменений?", '
                        '"ask_options": ["Только текущий основной flow", "Все текущие сценарии", "Нужно уточнить отдельно"]}'
                    )
                return (
                    '{"needs_rework": false, "issues": [], "missing_sections": [], '
                    '"required_input_gaps": ["Какие сценарии нельзя сломать"]}'
                )
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Реализовать поведение."

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-required-input-no-shell",
                "active_mode": "analyst",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                    "required_inputs": ["Какие сценарии нельзя сломать"],
                },
            },
        )()

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(
            session,
            "Подготовь implementation-ready spec без уточнения сценариев",
            bot=_FakeBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert "Какие сценарии нужно сохранить без изменений?" not in out
        assert "## Контекст" in out
        assert getattr(session, "analyst_blocking_clarification_runtime", False) is False
        assert seed_question_calls["count"] == 0
        assert "Допущения и незакрытые входы" not in out

    asyncio.run(_run())


def test_orchestrator_does_not_persist_required_input_pause_state_after_final_qc_gaps(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Spec with required inputs",
                "required_sections": ["Контекст", "Требования"],
                "required_inputs": ["Какие сценарии нельзя сломать"],
                "qa_prompt": "QA-REQUIRED-INPUTS",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=True,
            final_rework_passes=1,
            template_provider=_template_provider,
        )
        assessment_calls = {"count": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return []

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            if response_format is not None:
                if "Seed question:" in _user:
                    return (
                        '{"ask_question": "Какие сценарии нужно сохранить без изменений?", '
                        '"ask_options": ["Только текущий основной flow", "Все текущие сценарии", "Нужно уточнить отдельно"]}'
                    )
                assessment_calls["count"] += 1
                if assessment_calls["count"] == 1:
                    return (
                        '{"needs_rework": false, "issues": [], "missing_sections": [], '
                        '"required_input_gaps": ["Какие сценарии нельзя сломать"]}'
                    )
                return '{"needs_rework": false, "issues": [], "missing_sections": [], "required_input_gaps": []}'
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Реализовать поведение."

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-required-input-reset",
                "active_mode": "analyst",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                    "required_inputs": ["Какие сценарии нельзя сломать"],
                },
            },
        )()

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        out_first = await orch.run(
            session,
            "Подготовь implementation-ready spec без уточнения сценариев",
            bot=_FakeBot(),
            context=None,
            dest=dest,
        )
        assert "Какие сценарии нужно сохранить без изменений?" not in out_first
        assert "## Контекст" in out_first
        assert getattr(session, "analyst_blocking_clarification_runtime", False) is False

        out_second = await orch.run(
            session,
            "Подготовь implementation-ready spec после уточнения сценариев",
            bot=_FakeBot(),
            context=None,
            dest=dest,
        )

        assert out_second != out_first
        assert "# Техническое задание" in out_second
        assert getattr(session, "analyst_blocking_clarification_runtime", False) is False

    asyncio.run(_run())


def test_orchestrator_analyst_never_auto_continues_after_clarification_limit(tmp_path, monkeypatch):
    async def _run():
        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            max_clarifications=1,
            continue_without_clarifications=True,
        )
        plan_calls = {"count": 0}
        executed: list[str] = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            plan_calls["count"] += 1
            if plan_calls["count"] == 1:
                ask_id = "ask1"
                ask_title = "Уточнить платформу"
                ask_instruction = "ask platform"
                ask_question = "Какая платформа нужна?"
                ask_options = ["web", "mobile"]
            elif plan_calls["count"] == 2:
                ask_id = "ask2"
                ask_title = "Уточнить ограничения"
                ask_instruction = "ask constraints"
                ask_question = "Какие ограничения критичны?"
                ask_options = ["Сроки", "Совместимость"]
            else:
                return [PlanStep(id="final", title="Финал", instruction="do final")]
            return [
                PlanStep(
                    id=ask_id,
                    title=ask_title,
                    instruction=ask_instruction,
                    step_type="ask_user",
                    ask_question=ask_question,
                    ask_options=ask_options,
                ),
                PlanStep(
                    id="final",
                    title="Продолжить работу",
                    instruction="do final",
                    depends_on=[ask_id],
                ),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            executed.append(step.id)
            if step.step_type == "ask_user":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary=f"Получен ответ для {step.id}",
                    outputs=[{"type": "text", "content": "User selected: A"}],
                    tool_calls=[{"tool": "ask_user"}],
                    next_questions=[],
                )
            raise AssertionError("analyst must not auto-continue to final work after clarification limit")

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-clarification-limit-analyst",
                "active_mode": "analyst",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "needs_clarification": True,
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                },
            },
        )()

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(
            session,
            "Сделай ТЗ и дождись всех уточнений пользователя",
            bot=_FakeBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert executed == ["ask1", "ask2"]
        assert "допущ" not in out.lower()
        assert plan_calls["count"] == 2

    asyncio.run(_run())


def test_orchestrator_analyst_pauses_on_opaque_ask_user_success(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        plan_calls = {"count": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            plan_calls["count"] += 1
            if plan_calls["count"] > 1:
                raise AssertionError("opaque ask_user success must not trigger replan")
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнение",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                ),
                PlanStep(
                    id="final",
                    title="Продолжить работу",
                    instruction="do final",
                    depends_on=["ask1"],
                ),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            if step.id != "ask1":
                raise AssertionError("final step must stay blocked behind ask_user")
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="Ответ получен",
                outputs=[{"type": "text", "content": "A"}],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-opaque-ask-user",
                "active_mode": "analyst",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": True,
                    "clarification_question": "Какой вариант нужен?",
                    "needs_clarification": True,
                },
            },
        )()

        out = await orch.run(
            session,
            "fix issue",
            bot=None,
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert out == "Какой вариант нужен?"
        assert plan_calls["count"] == 1

    asyncio.run(_run())


def test_orchestrator_analyst_no_transport_returns_composed_spec_document_not_raw_summary(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Spec template",
                "required_sections": ["Контекст", "Требования"],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Собрать факты", instruction="collect")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="RAW SUMMARY ONLY",
                outputs=[{"type": "text", "content": "repo-grounded evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Зафиксировать изменения."

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-no-transport-spec",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                },
            },
        )()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        out = await orch.run(session, "Подготовь implementation-ready spec", bot=None, context=None, dest=dest)

        assert out.strip() != "RAW SUMMARY ONLY"
        assert "# Техническое задание" in out
        assert "## Контекст" in out
        assert "## Требования" in out
        assert "## Исходная задача" in out

    asyncio.run(_run())


def test_orchestrator_analyst_empty_final_text_uses_template_aware_fallback_not_raw_summary(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Spec template",
                "required_sections": ["Контекст", "Требования"],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Собрать факты", instruction="collect")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="RAW SUMMARY ONLY",
                outputs=[{"type": "text", "content": "repo-grounded evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return ""

        sent_outputs = []

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, _session, _dest, text, _context, **_kwargs):
                sent_outputs.append(str(text or ""))
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-empty-final-text-spec",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                },
            },
        )()

        out = await orch.run(
            session,
            "Подготовь implementation-ready spec",
            bot=_FakeBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert out.strip() != "RAW SUMMARY ONLY"
        assert "# Техническое задание" in out
        assert "## Контекст" in out
        assert "## Требования" in out
        assert "deterministic fallback" in out
        assert sent_outputs == [out]

    asyncio.run(_run())


def test_orchestrator_analyst_send_failure_returns_composed_document_not_raw_summary(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Spec template",
                "required_sections": ["Контекст", "Требования"],
                "qa_prompt": "QA-SPEC",
                "output_kind": "spec",
                "compose_mode": "template_first",
                "protected_spec_shell": {
                    "title": "Техническое задание",
                    "source_task_section": "Исходная задача",
                    "core_sections": ["Контекст", "Требования"],
                    "open_questions_section": "Открытые вопросы и валидационные шаги",
                },
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Собрать факты", instruction="collect")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="RAW SUMMARY ONLY",
                outputs=[{"type": "text", "content": "repo-grounded evidence"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Зафиксировать изменения."

        class _FailingBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                raise RuntimeError("send failed")

            async def _send_document(self, *_args, **_kwargs):
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type(
            "S",
            (),
            {
                "id": "sess-send-failed-spec",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_intent_flags": {
                    "clarification_is_blocking": False,
                    "document_kind": "spec",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                },
            },
        )()

        out = await orch.run(
            session,
            "Подготовь implementation-ready spec",
            bot=_FailingBot(),
            context=None,
            dest={"kind": "telegram", "chat_id": 101, "chat_type": "private"},
        )

        assert out.strip() != "RAW SUMMARY ONLY"
        assert "# Техническое задание" in out
        assert "Подтвержденный контекст." in out
        assert "- Зафиксировать изменения." in out

    asyncio.run(_run())


def test_orchestrator_uses_run_scoped_artifacts_for_analyst_runtime(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        store = RunArtifactStore(orch._config)

        session = type("S", (), {"id": "sess-analyst", "workdir": str(tmp_path)})()
        run_handle = store.start_run(
            session=session,
            mode_id="analyst",
            run_id="run_20260410T120000Z_artifacts",
        )
        session.analyst_run_artifact_handle = run_handle
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        async def _fake_plan_steps(_cfg, user_text, _ctx):
            if "Ответ пользователя:" in user_text:
                return []
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнение",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                )
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            assert step.id == "ask1"
            return ExecutorResponse(
                task_id="ask1",
                status="ok",
                summary="Ответ получен",
                outputs=[{"type": "text", "content": "Ответ пользователя: A"}],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "FINAL"

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(session, "fix issue", bot=_FakeBot(), context=None, dest=dest)

        assert out == "FINAL"
        artifact_path = Path(run_handle.artifacts_dir) / "ask1.md"
        legacy_path = Path(tmp_path) / "_sandbox" / "chats" / "chat_101" / "_orchestrator" / "ask1.md"
        artifact_text = artifact_path.read_text(encoding="utf-8")
        assert artifact_path.exists()
        assert legacy_path.exists() is False
        assert "- question: Какой вариант нужен?" in artifact_text
        assert "A" in artifact_text
        assert "B" in artifact_text

    asyncio.run(_run())


def test_orchestrator_rework_accepts_only_structured_final_text(tmp_path, monkeypatch):
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

        orch = OrchestratorRunner(
            cfg,
            final_rework_enabled=True,
            final_rework_passes=1,
            template_provider=lambda _session: {
                "name": "Default",
                "required_sections": ["S1"],
                "qa_prompt": "QA",
            },
        )

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Сделать работу", instruction="do")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            assert step.id == "step1"
            return ExecutorResponse(
                task_id="step1",
                status="ok",
                summary="step done",
                outputs=[{"type": "text", "content": "step done"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        calls = {"compose": 0, "qc": 0, "rework": 0}

        async def _fake_chat_completion(_cfg, system, _user, response_format=None):
            if response_format is not None:
                calls["qc"] += 1
                if calls["qc"] == 1:
                    return '{"needs_rework": true, "issues": ["fix"], "missing_sections": []}'
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            if "Доработай ТЗ" in system and "final_text" in system:
                calls["rework"] += 1
                if calls["rework"] == 1:
                    return "Проанализирую текущее состояние"
                return '{"final_text":"REVISED"}'
            calls["compose"] += 1
            return "DRAFT"

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = type("S", (), {"id": "sess-1"})()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await orch.run(session, "fix issue", bot=_FakeBot(), context=None, dest=dest)

        assert out == "REVISED"
        assert calls["compose"] == 1
        assert calls["rework"] == 2

    asyncio.run(_run())


def test_orchestrator_start_log_distinguishes_source_user_text_from_runtime_prompt(tmp_path, monkeypatch, caplog):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        session = type("S", (), {"id": "sess-1"})()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}

        state_path = Path(tmp_path) / "run-state.json"
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
        session.analyst_run_artifact_handle = type("H", (), {"state_path": str(state_path)})()

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Новый шаг", instruction="do work")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": "done"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        with caplog.at_level(logging.INFO, logger="modes.sdk.orchestrator_runner"):
            out = await orch.run(
                session,
                "Ты работаешь в режиме Аналитик.\n\nЗадача: ...",
                bot=None,
                context=None,
                dest=dest,
            )

        assert "done" in out
        start_logs = [record.getMessage() for record in caplog.records if "orchestrator run START" in record.getMessage()]
        assert start_logs
        assert "source_user_text='Что необходимо улучшить на сайте?'" in start_logs[-1]
        assert "input_prompt='Ты работаешь в режиме Аналитик." in start_logs[-1]
        assert " user_text=" not in start_logs[-1]

    asyncio.run(_run())


def test_orchestrator_persists_clarification_answers_into_recovery_bundle(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}
        state_path = Path(tmp_path) / "run-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "mode_context": {
                        "source_user_text": "Сделай ТЗ",
                        "input_bundle": {
                            "original_user_text": "Сделай ТЗ",
                            "clarification_answers": [],
                            "recovery_prompt_text": "Сделай ТЗ",
                        },
                        "execution_context": {
                            "source_user_text": "Сделай ТЗ",
                            "user_text_preview": "Сделай ТЗ",
                        },
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
                "id": "sess-clarify",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_run_artifact_handle": type("H", (), {"state_path": str(state_path)})(),
                "analyst_intent_flags": {
                    "clarification_is_blocking": True,
                    "document_kind": "spec",
                    "needs_clarification": True,
                    "clarification_topic": "Нужно уточнить платформу",
                    "requires_codebase_grounding": False,
                    "requires_final_repo_review": False,
                    "requires_repo_audit": False,
                },
            },
        )()

        calls = {"n": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    PlanStep(
                        id="ask1",
                        title="Уточнение",
                        instruction="ask",
                        step_type="ask_user",
                        ask_question="Какая платформа в приоритете?",
                        ask_options=["web", "mobile"],
                    )
                ]
            return []

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="answered",
                outputs=[{"type": "text", "content": "User selected: mobile"}],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        await orch.run(session, "Сделай ТЗ", bot=None, context=None, dest=dest)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        input_bundle = state["mode_context"]["input_bundle"]
        assert input_bundle["clarification_answers"] == ["mobile"]
        assert input_bundle["recovery_prompt_text"] == "Сделай ТЗ\nОтвет пользователя: mobile"
        assert state["mode_context"]["execution_context"]["clarification_answers"] == ["mobile"]

    asyncio.run(_run())


def test_orchestrator_persist_recovery_bundle_ignores_control_answers(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state_path = Path(tmp_path) / "run-state.json"
    state_path.write_text(
        json.dumps(
            {
                "mode_context": {
                    "source_user_text": "Сделай ТЗ",
                    "input_bundle": {
                        "original_user_text": "Сделай ТЗ",
                        "clarification_answers": [],
                        "recovery_prompt_text": "Сделай ТЗ",
                    },
                    "execution_context": {
                        "source_user_text": "Сделай ТЗ",
                        "user_text_preview": "Сделай ТЗ",
                    },
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
            "id": "sess-clarify",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_run_artifact_handle": type("H", (), {"state_path": str(state_path)})(),
        },
    )()

    orch._persist_recovery_input_bundle(
        session,
        clarification_answers=[
            "Продолжить с предположениями",
            "mobile",
            "Остановиться и уточнить",
        ],
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    input_bundle = state["mode_context"]["input_bundle"]
    assert input_bundle["clarification_answers"] == ["mobile"]
    assert input_bundle["recovery_prompt_text"] == "Сделай ТЗ\nОтвет пользователя: mobile"
    assert state["mode_context"]["execution_context"]["clarification_answers"] == ["mobile"]


def test_orchestrator_persists_analyst_quality_metrics(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Repo analysis",
                "required_sections": ["S1"],
                "qa_prompt": "QA-REPO",
                "repo_grounded_required": True,
                "output_kind": "analysis",
                "compose_mode": "template_first",
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=True,
            template_provider=_template_provider,
        )
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}
        state_path = Path(tmp_path) / "run-state.json"
        metrics_path = Path(tmp_path) / "METRICS.json"
        state_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
        state_path.write_text(
            json.dumps(
                {
                    "mode_context": {
                        "input_bundle": {
                            "template_resolution": {
                                "selected_template_id": "default",
                                "intent_template_id": "default",
                                "effective_template_id": "change_spec",
                                "document_kind": "spec",
                                "change_scope": "broad_change",
                            }
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        metrics_path.write_text(json.dumps({"version": 1, "run_id": "r1"}, ensure_ascii=False), encoding="utf-8")

        session = type(
            "S",
            (),
            {
                "id": "sess-metrics",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_run_artifact_handle": type(
                    "H",
                    (),
                    {"state_path": str(state_path), "metrics_path": str(metrics_path)},
                )(),
            },
        )()

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="use_cli_repo_grounding", title="Grounding", instruction="ground", step_type="use_cli")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="grounding ok",
                outputs=[{"type": "text", "content": "repo evidence"}],
                claims=[
                    {
                        "claim_id": "claim1",
                        "status": "confirmed",
                        "text": "Repo-grounded fact.",
                        "evidence": [{"type": "repo_evidence", "path": str(tmp_path / "views" / "header.blade.php")}],
                    }
                ],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "## Статус готовности\n\n**Готово к реализации.**\n\nDRAFT"

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        out = await orch.run(session, "Сделай repo-grounded анализ", bot=_FakeBot(), context=None, dest=dest)

        assert out == "DRAFT"
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        quality = payload.get("analyst_quality") or {}
        assert quality.get("runtime_verdict") == "Готово к реализации"
        assert quality.get("optimistic_status_removed") is True
        assert quality.get("false_ready_rate") == 0.0
        assert quality.get("invented_claim_rate") == 0.0
        assert quality.get("routing_correctness") == 1.0
        assert quality.get("routing", {}).get("expected_template_id") == "change_spec"
        assert quality.get("structured_bundle_stages", {}).get("gap_closure", {}).get("calls") == 0
        assert quality.get("invented_claims_by_source") == {}

    asyncio.run(_run())


def test_orchestrator_quality_metrics_track_structured_use_cli_step_stage(tmp_path, monkeypatch):
    async def _run():
        def _template_provider(_session):
            return {
                "name": "Repo analysis",
                "required_sections": ["S1"],
                "qa_prompt": "QA-REPO",
                "repo_grounded_required": True,
                "output_kind": "analysis",
                "compose_mode": "template_first",
            }

        orch = OrchestratorRunner(
            _make_orchestrator(tmp_path)._config,
            final_rework_enabled=False,
            template_provider=_template_provider,
        )
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}
        state_path = Path(tmp_path) / "run-state-structured.json"
        metrics_path = Path(tmp_path) / "METRICS-structured.json"
        state_path.write_text(
            json.dumps(
                {
                    "mode_context": {
                        "input_bundle": {
                            "template_resolution": {
                                "selected_template_id": "default",
                                "intent_template_id": "default",
                                "effective_template_id": "change_spec",
                                "document_kind": "spec",
                                "change_scope": "broad_change",
                            }
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        metrics_path.write_text(json.dumps({"version": 1, "run_id": "r2"}, ensure_ascii=False), encoding="utf-8")

        session = type(
            "S",
            (),
            {
                "id": "sess-structured-stage",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "analyst_run_artifact_handle": type(
                    "H",
                    (),
                    {"state_path": str(state_path), "metrics_path": str(metrics_path)},
                )(),
            },
        )()

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            review = PlanStep(id="use_cli_repo_final_review", title="Final review", instruction="review", step_type="use_cli")
            setattr(review, "_use_cli_response_format", "repo_review_bundle_json")
            return [review]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id="use_cli_repo_final_review",
                status="ok",
                summary="review ok",
                outputs=[{"type": "repo_review_verdict", "content": "repo review ok", "content_preview": "repo review ok"}],
                claims=[],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "DRAFT"

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        await orch.run(session, "Сделай финальную repo-grounded сверку", bot=_FakeBot(), context=None, dest=dest)

        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        quality = payload.get("analyst_quality") or {}
        assert quality.get("structured_bundle_calls") == 1
        assert quality.get("structured_bundle_successes") == 1
        assert quality.get("structured_bundle_stages", {}).get("final_review", {}).get("calls") == 1
        assert quality.get("structured_bundle_stages", {}).get("final_review", {}).get("successes") == 1

    asyncio.run(_run())


def test_orchestrator_spills_large_text_outputs_to_artifacts(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        session = type("S", (), {"id": "sess-spill", "workdir": str(tmp_path), "project_root": str(tmp_path)})()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}
        long_output = "L" * 8000

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Long step", instruction="do work")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done",
                outputs=[{"type": "text", "content": long_output}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        out = await orch.run(session, "collect facts", bot=None, context=None, dest=dest)

        assert "done" in out
        session_path = Path(tmp_path) / "_sandbox" / "chats" / "chat_101" / "SESSION.json"
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        run_entry = payload["orchestrator_by_task"]["sess-spill"][-1]
        step_results = run_entry["step_results"]
        assert len(step_results) == 1
        outputs = step_results[0]["outputs"]
        assert len(outputs) == 1
        output = outputs[0]
        assert output["type"] == "text"
        assert output["content_len"] == len(long_output)
        assert output["content_spilled"] is True
        assert output["content_preview"] == long_output[:2000]
        spill_path = Path(output["path"])
        assert spill_path.exists()
        assert spill_path.read_text(encoding="utf-8").strip() == long_output

    asyncio.run(_run())


def test_orchestrator_use_cli_step_artifact_keeps_full_output_without_trim_marker(tmp_path, monkeypatch):
    async def _run():
        orch = _make_orchestrator(tmp_path)
        session = type(
            "S",
            (),
            {
                "id": "sess-use-cli-artifact",
                "workdir": str(tmp_path),
                "project_root": str(tmp_path),
                "executor_profile": "analyst",
            },
        )()
        dest = {"kind": "telegram", "chat_id": 101, "chat_type": "private"}
        long_output = ("repo-grounded paragraph\n" * 400).strip()

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [
                PlanStep(
                    id="use_cli_repo_grounding",
                    title="Grounding",
                    instruction="collect repo facts",
                    step_type="use_cli",
                )
            ]

        async def _fake_tool_execute(name, args, ctx):
            assert name == "use_cli"
            del args, ctx
            return {"success": True, "output": long_output}

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None, **_kwargs):
            if response_format is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "FINAL"

        profile = type("P", (), {"name": "analyst", "allowed_tools": ["use_cli"]})

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._tool_registry, "execute", _fake_tool_execute)
        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda step, session: profile)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        out = await orch.run(session, "collect facts", bot=None, context=None, dest=dest)

        assert out
        artifact_path = (
            Path(tmp_path)
            / "_sandbox"
            / "chats"
            / "chat_101"
            / "_orchestrator"
            / "use-cli-repo-grounding.md"
        )
        artifact_text = artifact_path.read_text(encoding="utf-8")
        assert long_output in artifact_text
        assert "...(truncated" not in artifact_text

    asyncio.run(_run())
