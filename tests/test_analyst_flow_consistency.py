import asyncio
import json
import types
from pathlib import Path

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.analyst.mode import AnalystMode
from modes.analyst.runner_service import AnalystModeRunnerService
from modes.analyst.state_store import (
    AnalystContext,
    AnalystStateStore,
    build_context_key,
    resolve_analyst_state_root,
)
from modes.analyst.ui import build_analyst_status_text
from modes.sdk import SessionControlService
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep
from session import session_runtime_uid


class _IntentTooling:
    def __init__(self, payloads) -> None:
        if isinstance(payloads, list):
            self._payloads = list(payloads)
        else:
            self._payloads = [payloads]
        self._index = 0

    async def execute(self, _name, _args, _ctx):
        payload = self._payloads[min(self._index, len(self._payloads) - 1)]
        self._index += 1
        return {"success": True, "output": json.dumps(payload, ensure_ascii=False)}

    async def ask_user(self, **_kwargs):
        return "unused"


class _Runtime:
    def __init__(self) -> None:
        self.prompt = ""

    async def run(self, _session, prompt, _bot_app, _context, _dest) -> str:
        self.prompt = str(prompt or "")
        return "Черновик аналитики"


class _RuntimeCaptureSource:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, session, prompt, _bot_app, _context, _dest) -> str:
        self.calls.append(
            {
                "prompt": str(prompt or ""),
                "source_user_text": str(getattr(session, "analyst_source_user_text_runtime", "") or ""),
            }
        )
        return "Черновик аналитики"


def test_analyst_on_disable_cancels_mode_tasks() -> None:
    async def _run() -> None:
        cancel_calls = []
        persist_calls = {"n": 0}

        async def _cancel_mode(session_id: str, mode_id: str, timeout_s: float) -> int:
            cancel_calls.append((session_id, mode_id, timeout_s))
            return 1

        mode = AnalystMode()
        mode.initialize(
            services={
                "session_control": SessionControlService(
                    persist_sessions=lambda: persist_calls.__setitem__("n", persist_calls["n"] + 1),
                    cancel_mode_tasks=_cancel_mode,
                    cancel_session_tasks=(lambda *_a, **_k: asyncio.sleep(0, result=0)),
                ),
            }
        )
        session = types.SimpleNamespace(
            id="s1",
            active_mode="analyst",
            modes=types.SimpleNamespace(active_mode="analyst", analyst_mode="spec"),
            cli_work_type="analytics",
            executor_profile="analyst",
        )
        bot_app = types.SimpleNamespace()
        await mode.on_disable({"session": session, "bot_app": bot_app})

        assert session.modes.active_mode is None
        assert cancel_calls == [(session_runtime_uid(session), "analyst", 0.2)]
        assert persist_calls["n"] >= 1

    asyncio.run(_run())


def test_analyst_status_uses_explicit_stage_markers() -> None:
    session = types.SimpleNamespace(
        id="s1",
        name=None,
        tool=types.SimpleNamespace(name="dummy"),
        workdir="/tmp",
        active_mode="analyst",
        modes=types.SimpleNamespace(active_mode="analyst", analyst_mode="spec"),
        busy=False,
        started_at=None,
        last_output_ts=None,
        last_tick_ts=None,
        tick_seen=0,
        queue=[],
    )
    text_wait = build_analyst_status_text(
        session,
        analyst_context=types.SimpleNamespace(mode="awaiting_input"),
        analyst_running=False,
        pending_questions={},
    )
    assert "Стадия: ожидает выбор пути для аудита" in text_wait

    text_audit = build_analyst_status_text(
        session,
        analyst_context=types.SimpleNamespace(mode="audit"),
        analyst_running=True,
        pending_questions={},
    )
    assert "Стадия: проводит аудит" in text_audit

    text_spec = build_analyst_status_text(
        session,
        analyst_context=types.SimpleNamespace(mode="spec"),
        analyst_running=True,
        pending_questions={},
    )
    assert "Стадия: анализирует запрос" in text_spec


def test_analyst_draft_text_prefers_stable_context_snapshot(tmp_path) -> None:
    async def _run() -> None:
        mode = AnalystMode()
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        mode._store(session).save(
            AnalystContext(
                key=mode._context_key(session),
                document_kind="spec",
                last_draft="Стабильный черновик",
            )
        )
        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(workdir=str(tmp_path), openai_api_key="", openai_model="")
            )
        )
        out = await mode._build_draft_text(bot_app, session, chat_id=1)
        assert "Стабильный черновик" in out
        assert "# Черновик ТЗ" in out
        assert "Тип документа: техническое задание" in out

    asyncio.run(_run())


def test_analyst_draft_text_uses_audit_header_for_audit_context(tmp_path) -> None:
    async def _run() -> None:
        mode = AnalystMode()
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), analyst_mode="audit")
        mode._store(session).save(
            AnalystContext(
                key=mode._context_key(session),
                mode="audit",
                active_flow="audit",
                runtime_template_id="audit",
                effective_template_id="audit",
                document_kind="audit",
                last_draft="Найдено критичное наблюдение",
            )
        )
        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(workdir=str(tmp_path), openai_api_key="", openai_model="")
            )
        )

        out = await mode._build_draft_text(bot_app, session, chat_id=1)

        assert "# Черновик отчета по аудиту" in out
        assert "Тип документа: отчет по аудиту" in out
        assert "Найдено критичное наблюдение" in out

    asyncio.run(_run())


def test_analyst_mode_context_is_isolated_by_chat_for_same_session_id(tmp_path) -> None:
    mode = AnalystMode()
    session_chat1 = types.SimpleNamespace(id="s1", chat_id=1, workdir=str(tmp_path))
    session_chat2 = types.SimpleNamespace(id="s1", chat_id=2, workdir=str(tmp_path))

    key_chat1 = mode._context_key(session_chat1)
    key_chat2 = mode._context_key(session_chat2)
    assert key_chat1 == "1_s1"
    assert key_chat2 == "2_s1"

    store = mode._store(session_chat1)
    ctx_chat1 = store.load(key_chat1)
    ctx_chat1.runtime_template_id = "audit"
    ctx_chat1.intent_reason = "chat1-only"
    store.save(ctx_chat1)

    loaded_chat2 = mode._store(session_chat2).load(key_chat2)
    assert loaded_chat2.runtime_template_id == ""
    assert loaded_chat2.intent_reason == ""


def test_analyst_prompt_rules_do_not_hardcode_surface_list() -> None:
    mode = AnalystMode()
    prompts = mode._load_prompts()

    checklist = str(prompts.get("affected_surfaces_checklist") or "")

    assert checklist
    assert "- telegram" not in checklist.lower()
    assert "- desktop" not in checklist.lower()
    assert "- miniapp" not in checklist.lower()
    assert "фиксированный список" in checklist.lower()


def test_analyst_templates_do_not_use_rollback_wording() -> None:
    template_path = Path(__file__).resolve().parents[1] / "modes" / "analyst" / "templates" / "analyst_config.yaml"
    content = template_path.read_text(encoding="utf-8")

    assert "rollback" not in content.lower()
    assert "откат" not in content.lower()


@pytest.mark.parametrize("template_id", ["change_spec", "refactor_spec"])
def test_analyst_runner_service_large_repo_grounded_templates_use_three_qc_passes(
    tmp_path,
    monkeypatch,
    template_id: str,
) -> None:
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
    output_kind: "spec"
    target_size_hint: "large"
    repo_grounded_required: true
  refactor_spec:
    name: "Refactor"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: ""
    qa_prompt: "Q2"
    output_kind: "spec"
    target_size_hint: "large"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates))

    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            return [
                PlanStep(id="use_cli_repo_grounding", title="Repo grounding",
                         instruction=f"grounding in {tmp_path}", step_type="use_cli"),
            ]

        async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
            return orch._deps.ExecutorResponse(
                task_id=step.id, status="ok", summary="repo grounding ok",
                outputs=[{"type": "text", "content": "evidence", "path": str(tmp_path / "app.py")}],
                claims=[{"claim_id": "c1", "status": "confirmed", "text": "grounded",
                         "evidence": [{"type": "text", "path": str(tmp_path / "app.py"), "preview": "ev"}]}],
                tool_calls=[], next_questions=[],
            )

        async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
            if response_format is not None:
                calls["qc"] += 1
                if calls["qc"] < 4:
                    return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            calls["compose"] += 1
            return "DRAFT"

        async def _fake_run_prompt(prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": ["repo_step:use_cli_repo_grounding"],
                        "remaining_obligations": [],
                        "corrections_applied": ["Уточнён repo-grounded spec."],
                        "claims": [],
                        "evidence": [],
                        "degraded_modes": [],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
                calls["followup_review"] += 1
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
            raise AssertionError(f"Unexpected run_prompt call: {prompt[:200]}")

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            analyst_template_id="default",
            analyst_intent_flags={"clarification_is_blocking": False},
            run_prompt=_fake_run_prompt,
        )
        store = AnalystStateStore(resolve_analyst_state_root(session))
        store.save(
            AnalystContext(
                key=build_context_key(session.chat_id, session.id),
                effective_template_id=template_id,
                mode="spec",
            )
        )

        template = service._get_effective_template_for_session(session)
        assert template["target_size_hint"] == "large"
        assert template["repo_grounded_required"] is True

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь большой repo-grounded spec",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert "POLISHED" in out
        assert calls["compose"] >= 1
        assert calls["qc"] >= 3
        assert calls["gap_closure"] >= 1
        assert calls["followup_review"] >= 1
        assert "# Техническое задание" in out
        assert "## Исходная задача" in out
        assert "## Открытые вопросы и валидационные шаги" in out

        artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
        persisted_draft = (artifacts_dir / "s1_draft.md").read_text(encoding="utf-8")
        polished_draft = (artifacts_dir / "s1_draft_polished.md").read_text(encoding="utf-8")
        for content in (persisted_draft, polished_draft):
            assert "# Техническое задание" in content
            assert "## Исходная задача" in content
            assert "## Открытые вопросы и валидационные шаги" in content

    asyncio.run(_run())


def test_analyst_runner_service_delivers_final_document_even_when_required_repo_use_cli_steps_missing(
    tmp_path,
    monkeypatch,
) -> None:
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
    target_size_hint: "large"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates))

    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"compose": 0}
        delivered: list[str] = []
        sent_via_output = {"value": False}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            return []

        async def _fake_chat_completion(*_args, **_kwargs):
            if _kwargs.get("response_format") is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            calls["compose"] += 1
            return "## Spec\nFINAL DOC"

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            active_mode="analyst",
            analyst_template_id="default",
            analyst_intent_flags={
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
        )
        AnalystStateStore(resolve_analyst_state_root(session)).save(
            AnalystContext(
                key=build_context_key(session.chat_id, session.id),
                effective_template_id="change_spec",
                mode="spec",
            )
        )

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, _session, _dest, output, _context, **_kwargs):
                delivered.append(str(output or ""))
                sent_via_output["value"] = True
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь repo-grounded spec",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert "Документ не считается завершённым" not in out
        assert "FINAL DOC" in out
        assert calls["compose"] >= 1

    asyncio.run(_run())


def test_analyst_runner_service_replans_to_inject_missing_repo_steps_before_finalizing(
    tmp_path,
    monkeypatch,
) -> None:
    """When repo-grounded steps are missing at finalization, the orchestrator
    should attempt one replan to inject them before proceeding with
    finalization anyway. This covers the template-switch scenario: plan built
    under change_spec (no audit), but finalization checks against ui_change_spec
    (audit required). The final document must still be delivered to the user.
    """
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
    target_size_hint: "large"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates))

    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        plan_call_count = {"value": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            plan_call_count["value"] += 1
            return []

        async def _fake_chat_completion(*_args, **_kwargs):
            if _kwargs.get("response_format") is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            return "FINAL DOC"

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            active_mode="analyst",
            analyst_template_id="default",
            analyst_intent_flags={
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
                "needs_clarification": False,
                "clarification_is_blocking": False,
            },
        )
        AnalystStateStore(resolve_analyst_state_root(session)).save(
            AnalystContext(
                key=build_context_key(session.chat_id, session.id),
                effective_template_id="change_spec",
                mode="spec",
            )
        )

        class _FakeBot:
            async def send_output(self, _session, _dest, output, _context, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь repo-grounded spec",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert "Документ не считается завершённым" not in out
        assert "FINAL DOC" in out
        # The orchestrator must have called plan_steps at least twice:
        # once for the initial plan, once for the recovery replan.
        assert plan_call_count["value"] >= 2, (
            f"Expected at least 2 plan_steps calls (initial + recovery replan), got {plan_call_count['value']}"
        )

    asyncio.run(_run())


def test_analyst_runner_service_pauses_before_repo_finalization_when_blocking_clarification_unanswered(
    tmp_path,
    monkeypatch,
) -> None:
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
    target_size_hint: "large"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates))

    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"compose": 0}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            return []

        async def _fake_chat_completion(*_args, **_kwargs):
            if _kwargs.get("response_format") is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            calls["compose"] += 1
            return "## Spec\nFINAL DOC"

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            active_mode="analyst",
            analyst_template_id="default",
            analyst_intent_flags={
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
                "clarification_is_blocking": True,
            },
        )
        AnalystStateStore(resolve_analyst_state_root(session)).save(
            AnalystContext(
                key=build_context_key(session.chat_id, session.id),
                effective_template_id="change_spec",
                mode="spec",
            )
        )

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь repo-grounded spec",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert out == "Нужно уточнение пользователя, но вопрос не сформирован."
        assert calls["compose"] == 0

    asyncio.run(_run())


def test_analyst_runner_service_finishes_after_required_repo_use_cli_steps_complete(
    tmp_path,
    monkeypatch,
) -> None:
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["Spec"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
    output_kind: "spec"
    target_size_hint: "large"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates))

    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"compose": 0, "use_cli": [], "final_review_instruction": "", "final_review_response_format": ""}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            return [
                PlanStep(
                    id="use_cli_repo_audit",
                    title="Repo audit",
                    instruction=f"Сделай начальный аудит репозитория через CLI в директории:\n{tmp_path}",
                    step_type="use_cli",
                ),
                PlanStep(
                    id="use_cli_repo_final_review",
                    title="Repo final review",
                    instruction=f"Сделай финальный second-opinion review репозитория через CLI в директории:\n{tmp_path}",
                    step_type="use_cli",
                    depends_on=["use_cli_repo_audit"],
                ),
            ]

        async def _fake_execute_use_cli_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
            profile,
            corr_id,
        ):
            calls["use_cli"].append((step.id, profile.name, corr_id))
            if step.id == "use_cli_repo_final_review":
                calls["final_review_instruction"] = str(getattr(step, "instruction", "") or "")
                calls["final_review_response_format"] = str(getattr(step, "_use_cli_response_format", "") or "")
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"ok {step.id}"}],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        async def _fake_chat_completion(*_args, **_kwargs):
            if _kwargs.get("response_format") is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            calls["compose"] += 1
            return "## Spec\nFINAL DOC"

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_use_cli_step", _fake_execute_use_cli_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            analyst_template_id="default",
            analyst_intent_flags={
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
        )
        AnalystStateStore(resolve_analyst_state_root(session)).save(
            AnalystContext(
                key=build_context_key(session.chat_id, session.id),
                effective_template_id="change_spec",
                mode="spec",
            )
        )

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь repo-grounded spec",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert "FINAL DOC" in out
        assert calls["compose"] >= 1
        assert [step_id for step_id, _profile, _corr_id in calls["use_cli"]] == [
            "use_cli_repo_audit",
            "use_cli_repo_final_review",
        ]
        assert "Файл черновика ТЗ" in calls["final_review_instruction"]
        assert "_repo_final_review_draft.md" in calls["final_review_instruction"]
        assert str(tmp_path) in calls["final_review_instruction"]
        assert calls["final_review_response_format"] == CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON
        artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
        repo_review_draft = (artifacts_dir / "s1_repo_final_review_draft.md").read_text(encoding="utf-8")
        assert "# Техническое задание" in repo_review_draft
        assert "## Исходная задача" in repo_review_draft
        assert "## Открытые вопросы и валидационные шаги" in repo_review_draft

    asyncio.run(_run())


def test_analyst_runner_service_delivers_final_document_even_when_required_repo_grounding_use_cli_step_missing(
    tmp_path,
    monkeypatch,
) -> None:
    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"compose": 0}
        delivered: list[str] = []
        sent_via_output = {"value": False}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            return []

        async def _fake_chat_completion(*_args, **_kwargs):
            if _kwargs.get("response_format") is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            calls["compose"] += 1
            return "FINAL DOC"

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            active_mode="analyst",
            analyst_template_id="default",
            analyst_intent_flags={
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
        )

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, _session, _dest, output, _context, **_kwargs):
                delivered.append(str(output or ""))
                sent_via_output["value"] = True
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь repo-grounded analysis",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert "Документ не считается завершённым" not in out
        assert "FINAL DOC" in out
        assert calls["compose"] >= 1

    asyncio.run(_run())


def test_analyst_runner_service_finishes_after_required_repo_grounding_use_cli_step_complete(
    tmp_path,
    monkeypatch,
) -> None:
    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"compose": 0, "use_cli": []}

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            return [
                PlanStep(
                    id="use_cli_repo_grounding",
                    title="Repo grounding",
                    instruction=f"Сделай базовый repo-grounded анализ репозитория через CLI в директории:\n{tmp_path}",
                    step_type="use_cli",
                ),
            ]

        async def _fake_execute_use_cli_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
            profile,
            corr_id,
        ):
            calls["use_cli"].append((step.id, profile.name, corr_id))
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary=f"done {step.id}",
                outputs=[{"type": "text", "content": f"ok {step.id}"}],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )

        async def _fake_chat_completion(*_args, **_kwargs):
            if _kwargs.get("response_format") is not None:
                return '{"needs_rework": false, "issues": [], "missing_sections": []}'
            calls["compose"] += 1
            return "FINAL DOC"

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_use_cli_step", _fake_execute_use_cli_step)
        monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            analyst_template_id="default",
            analyst_intent_flags={
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
        )

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Подготовь repo-grounded analysis",
            bot_app=_FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )

        assert "FINAL DOC" in out
        assert calls["compose"] >= 1
        assert [step_id for step_id, _profile, _corr_id in calls["use_cli"]] == [
            "use_cli_repo_grounding",
        ]

    asyncio.run(_run())


def test_analyst_runner_service_blocking_clarification_replans_after_answer(
    tmp_path,
    monkeypatch,
) -> None:
    async def _run() -> None:
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
        service = AnalystModeRunnerService(cfg)
        orch = service.runner
        calls = {"plan": 0}
        executed = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
            calls["plan"] += 1
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнить обязательный параметр",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                ),
                PlanStep(id="final", title="Продолжить работу", instruction="do final"),
            ]

        async def _fake_execute_step(
            step,
            _session,
            _bot,
            _context,
            _dest,
            _orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            executed.append(step.id)
            if step.id == "ask1":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Ответ пользователя получен",
                    outputs=[{"type": "text", "content": "User selected: B"}],
                    tool_calls=[{"tool": "ask_user"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done final",
                outputs=[{"type": "text", "content": "final"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        async def _no_memory(*_args, **_kwargs):
            return None

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

        session = types.SimpleNamespace(
            id="s1",
            chat_id=1,
            workdir=str(tmp_path),
            analyst_template_id="default",
            analyst_intent_flags={
                "clarification_is_blocking": True,
                "document_kind": "spec",
                "requires_codebase_grounding": False,
                "requires_final_repo_review": False,
                "requires_repo_audit": False,
            },
        )

        class _FakeBot:
            async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
                return True

            async def send_output(self, *_args, **_kwargs):
                return None

            async def _send_document(self, *_args, **_kwargs):
                return None

        out = await service.run(
            session,
            "Нужно уточнение перед продолжением",
            bot_app=_FakeBot(),
            context=object(),
            dest={"kind": "telegram", "chat_id": 1, "chat_type": "private"},
        )

        assert "Автоматическое продолжение остановлено" not in out
        assert "final" in out
        assert calls["plan"] == 2
        assert executed == ["ask1", "final"]

    asyncio.run(_run())
