import asyncio
import json
import types
from pathlib import Path

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.analyst.mode import AnalystMode
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep
from modes.sdk.runtime.analyst_scorecard import (
    evaluate_golden_scenario,
    evaluate_release_gate,
    summarize_golden_scorecards,
)


class _Runtime:
    def __init__(self):
        self.prompt = ""

    async def run(self, _session, analyst_prompt, _bot_app, _context, _dest):
        self.prompt = analyst_prompt
        return "ok"

    def get_template_for_session(self, _session):
        return {
            "_id": "default",
            "required_sections": ["Контекст и обзор"],
            "system_prompt_addition": "",
            "qa_prompt": "qa",
        }


class _ToolingPayload:
    def __init__(self, payload: dict):
        self.payload = payload

    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {"success": True, "output": json.dumps(self.payload, ensure_ascii=False)}

    async def ask_user(self, **_kwargs):
        return "unused"


def _make_cfg(tmp_path) -> AppConfig:
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


def _write_templates(tmp_path: Path) -> Path:
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["S0"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: "CHANGE"
    qa_prompt: "Q1"
    compose_mode: "template_first"
    output_kind: "spec"
  ui_change_spec:
    name: "UI Change"
    description: "D"
    required_sections: ["Контекст и подтвержденные факты"]
    system_prompt_addition: "UI ONLY"
    qa_prompt: "Q2"
    compose_mode: "template_first"
    output_kind: "spec"
    repo_grounded_required: true
    repo_audit_required: true
    final_repo_review_required: true
  new_spec:
    name: "New Spec"
    description: "D"
    required_sections: ["New Spec"]
    system_prompt_addition: "NEW"
    qa_prompt: "Q4"
    compose_mode: "template_first"
    output_kind: "spec"
  audit:
    name: "Audit"
    description: "D"
    required_sections: ["Audit Findings"]
    system_prompt_addition: "AUDIT ONLY"
    qa_prompt: "Q3"
    compose_mode: "template_first"
    output_kind: "audit"
    repo_grounded_required: true
    repo_audit_required: true
    final_repo_review_required: true
""",
        encoding="utf-8",
    )
    return templates


@pytest.mark.asyncio
async def test_golden_local_ui_change_routes_to_ui_change_spec(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(_write_templates(tmp_path)))
    runtime = _Runtime()
    mode = AnalystMode()
    mode.initialize(
        config=types.SimpleNamespace(),
        services={
            "runtime_by_capability": lambda cap: runtime if str(cap) in {"run_analyst", "template_provider"} else None,
            "tooling": _ToolingPayload(
                {
                    "analysis_profile": "codebase",
                    "document_kind": "spec",
                    "detail_level": "standard",
                    "summary": "UI change",
                    "template_hint": "ui_change_spec",
                }
            ),
        },
    )
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
        analyst_template_id="default",
        analyst_runtime_template_id="",
    )

    await mode.run_pipeline(
        session=session,
        user_text="Подготовь ТЗ на доработку header menu и account dropdown",
        bot_app=types.SimpleNamespace(),
        context=None,
        dest={"kind": "telegram", "chat_id": 1},
    )
    assert "UI ONLY" in runtime.prompt
    scorecard = evaluate_golden_scenario(
        name="local_ui_change",
        observed={"effective_template_id": "ui_change_spec"},
        expectations={"expected_template_id": "ui_change_spec"},
    )
    assert scorecard["passed"] is True


@pytest.mark.asyncio
async def test_golden_broad_change_routes_to_change_spec(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(_write_templates(tmp_path)))
    runtime = _Runtime()
    mode = AnalystMode()
    mode.initialize(
        config=types.SimpleNamespace(),
        services={
            "runtime_by_capability": lambda cap: runtime if str(cap) in {"run_analyst", "template_provider"} else None,
            "tooling": _ToolingPayload(
                {
                    "analysis_profile": "codebase",
                    "document_kind": "spec",
                    "detail_level": "standard",
                    "summary": "Broad change",
                    "template_hint": "change_spec",
                }
            ),
        },
    )
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
        analyst_template_id="default",
        analyst_runtime_template_id="",
    )

    await mode.run_pipeline(
        session=session,
        user_text="Подготовь полное ТЗ на доработку checkout и платёжных модулей",
        bot_app=types.SimpleNamespace(),
        context=None,
        dest={"kind": "telegram", "chat_id": 1},
    )
    assert "CHANGE" in runtime.prompt
    scorecard = evaluate_golden_scenario(
        name="broad_change",
        observed={"effective_template_id": "change_spec"},
        expectations={"expected_template_id": "change_spec"},
    )
    assert scorecard["passed"] is True


@pytest.mark.asyncio
async def test_golden_audit_routes_to_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(_write_templates(tmp_path)))
    runtime = _Runtime()
    mode = AnalystMode()
    mode.initialize(
        config=types.SimpleNamespace(),
        services={
            "runtime_by_capability": lambda cap: runtime if str(cap) in {"run_analyst", "template_provider"} else None,
            "tooling": _ToolingPayload(
                {
                    "analysis_profile": "audit",
                    "document_kind": "audit",
                    "detail_level": "full",
                    "summary": "Audit request",
                    "template_hint": "audit",
                }
            ),
        },
    )
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        project_root=str(tmp_path),
        analyst_template_id="default",
        analyst_runtime_template_id="",
    )

    await mode.run_pipeline(
        session=session,
        user_text="Проведи аудит текущего проекта",
        bot_app=types.SimpleNamespace(),
        context=None,
        dest={"kind": "telegram", "chat_id": 1},
    )
    assert "AUDIT ONLY" in runtime.prompt
    scorecard = evaluate_golden_scenario(
        name="audit",
        observed={"effective_template_id": "audit"},
        expectations={"expected_template_id": "audit"},
    )
    assert scorecard["passed"] is True


def test_golden_blocking_clarification_pauses_cleanly(tmp_path, monkeypatch):
    async def _run():
        orch = OrchestratorRunner(_make_cfg(tmp_path))

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
            if step.id != "ask1":
                raise AssertionError("unexpected execution after needs_input")
            return ExecutorResponse(
                task_id="ask1",
                status="needs_input",
                summary="Нужен ответ пользователя",
                outputs=[],
                tool_calls=[{"tool": "ask_user"}],
                next_questions=["Какой вариант нужен?"],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = types.SimpleNamespace(
            id="sess-clarify",
            workdir=str(tmp_path),
            project_root=str(tmp_path),
            analyst_intent_flags={
                "clarification_is_blocking": True,
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_final_repo_review": True,
                "requires_repo_audit": False,
            },
        )

        out = await orch.run(session, "Сделай анализ", bot=None, context=None, dest={"kind": "telegram", "chat_id": 1})
        assert out == "Какой вариант нужен?"
        scorecard = evaluate_golden_scenario(
            name="blocking_clarification",
            observed={"paused": True},
            expectations={"expected_pause": True},
        )
        assert scorecard["passed"] is True

    asyncio.run(_run())


def test_golden_huge_context_spills_large_output(tmp_path, monkeypatch):
    async def _run():
        orch = OrchestratorRunner(_make_cfg(tmp_path))

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            return [PlanStep(id="step1", title="Большой шаг", instruction="analyze")]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            del session, bot, context, dest, orchestrator_context, current_user_text, constraints
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="huge output done",
                outputs=[{"type": "text", "content": "X" * 9000}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
            del response_format
            return "FINAL"

        class _FakeBot:
            def __init__(self):
                self.messages = []
                self.sent_output = False
                self.sent_docs = False

            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                self.messages.append(text)
                return True

            async def send_output(self, *_args, **_kwargs):
                self.sent_output = True
                return None

            async def _send_document(self, *_args, **_kwargs):
                self.sent_docs = True
                return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        fakebot = _FakeBot()
        out = await orch.run(
            types.SimpleNamespace(id="sess-huge", active_mode="analyst"),
            "Сделай анализ",
            fakebot,
            context=None,
            dest={"kind": "telegram", "chat_id": 1},
        )
        await asyncio.sleep(0)
        artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
        assert "FINAL" in out
        assert "### Артефакты" not in out
        assert fakebot.sent_output is True
        assert fakebot.sent_docs is False
        spilled = any(path.name.endswith(".md") and "_output_" in path.name for path in artifacts_dir.glob("*.md"))
        assert spilled
        scorecard = evaluate_golden_scenario(
            name="huge_context",
            observed={"artifact_spill": spilled},
            expectations={"expected_artifact_spill": True},
        )
        assert scorecard["passed"] is True

    asyncio.run(_run())


def test_golden_cli_failure_recovers_via_retry(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(cfg, final_rework_enabled=True, final_rework_passes=1, template_provider=_template_provider)

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "grounding evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system, _user, response_format=None):
        if response_format is not None:
            if not hasattr(_fake_chat_completion, "calls"):
                _fake_chat_completion.calls = 0
            _fake_chat_completion.calls += 1
            if _fake_chat_completion.calls == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        if not hasattr(_fake_chat_completion, "compose_calls"):
            _fake_chat_completion.compose_calls = 0
        _fake_chat_completion.compose_calls += 1
        return "DRAFT" if _fake_chat_completion.compose_calls == 1 else "POLISHED REWORKED"

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        def __init__(self):
            self.calls = 0

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                self.calls += 1
                if self.calls == 1:
                    return "[API Error: Qwen API quota exceeded]"
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": ["repo_step:use_cli_repo_grounding"],
                        "remaining_obligations": [],
                        "corrections_applied": ["Усилено repo-grounded описание."],
                        "claims": [
                            {
                                "claim_id": "claim_retry_1",
                                "status": "confirmed",
                                "text": "Header dropdown подтверждён по репозиторию.",
                                "evidence": [
                                    {
                                        "type": "repo_evidence",
                                        "path": str(tmp_path / "views" / "header.blade.php"),
                                        "preview": "read_file: header.blade.php",
                                    }
                                ],
                            }
                        ],
                        "evidence": [
                            {
                                "type": "repo_evidence",
                                "path": str(tmp_path / "views" / "header.blade.php"),
                                "preview": "read_file: header.blade.php",
                            }
                        ],
                        "degraded_modes": [],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "verdict": "Все blocking obligations закрыты.",
                        "closed_blocking_obligations": ["repo_step:use_cli_repo_grounding"],
                        "open_blocking_obligations": [],
                        "false_closures": [],
                        "unsupported_assertions": [],
                        "required_corrections": [],
                        "claims": [],
                        "evidence": [],
                        "degraded_modes": [],
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected prompt: {prompt[:200]}")

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(_Session(), "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))
    assert out == "POLISHED"
    scorecard = evaluate_golden_scenario(
        name="cli_failure_recovery",
        observed={"retry_recovered": True},
        expectations={"expected_retry_recovery": True},
    )
    assert scorecard["passed"] is True


def test_golden_scorecard_summary_passes_when_all_scenarios_green():
    summary = summarize_golden_scorecards(
        {"scenario": "local_ui_change", "passed": True},
        {"scenario": "broad_change", "passed": True},
        {"scenario": "audit", "passed": True},
        {"scenario": "blocking_clarification", "passed": True},
        {"scenario": "huge_context", "passed": True},
        {"scenario": "cli_failure_recovery", "passed": True},
    )

    assert summary["total"] == 6
    assert summary["passed"] == 6
    assert summary["failed"] == 0
    assert summary["pass_rate"] == 1.0


def test_golden_scorecard_can_evaluate_quality_payload():
    scorecard = evaluate_golden_scenario(
        name="quality_gate",
        observed={
            "quality": {
                "runtime_verdict": "Готово к реализации",
                "confirmed_claims_without_anchor": 0,
                "false_ready_rate": 0.0,
                "routing_correctness": 1.0,
                "structured_bundle_parse_rate": 1.0,
            }
        },
        expectations={
            "expected_runtime_verdict": "Готово к реализации",
            "max_invented_claims": 0,
            "max_false_ready_rate": 0.0,
            "min_routing_correctness": 1.0,
            "min_structured_bundle_parse_rate": 1.0,
        },
    )

    assert scorecard["passed"] is True
    assert scorecard["checks"]["readiness_correctness"]["passed"] is True
    assert scorecard["checks"]["invented_claims"]["passed"] is True
    assert scorecard["checks"]["false_ready_rate"]["passed"] is True
    assert scorecard["checks"]["routing_metric"]["passed"] is True
    assert scorecard["checks"]["structured_bundle_parse_rate"]["passed"] is True


def test_golden_release_gate_passes_on_target_thresholds():
    gate = evaluate_release_gate(
        quality_metrics={
            "false_ready_rate": 0.0,
            "invented_claim_rate": 0.0,
            "routing_correctness": 1.0,
            "structured_bundle_parse_rate": 1.0,
        },
        golden_summary={"pass_rate": 1.0},
    )

    assert gate["release_ready"] is True
    assert all(item["passed"] is True for item in gate["checks"].values())


def test_golden_release_gate_fails_when_quality_regresses():
    gate = evaluate_release_gate(
        quality_metrics={
            "false_ready_rate": 1.0,
            "invented_claim_rate": 0.25,
            "routing_correctness": 0.8,
            "structured_bundle_parse_rate": 0.75,
        },
        golden_summary={"pass_rate": 0.9},
    )

    assert gate["release_ready"] is False
    assert gate["checks"]["false_ready_rate"]["passed"] is False
    assert gate["checks"]["invented_confirmed_claim_rate"]["passed"] is False
    assert gate["checks"]["correct_template_routing"]["passed"] is False
    assert gate["checks"]["structured_bundle_parse_rate"]["passed"] is False
    assert gate["checks"]["golden_scenarios_pass_rate"]["passed"] is False
