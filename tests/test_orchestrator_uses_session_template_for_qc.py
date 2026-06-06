import asyncio
import builtins
import json
from pathlib import Path

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk import orchestrator_runner as orchestrator_runner_module
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.analyst.template_service import get_template_for_session
from modes.sdk.runtime.cli_contracts import CLIOutputType, CLIResponseFormat
from modes.sdk.runtime.contracts import PlanStep
from modes.sdk.runtime.obligations import build_task_contract


async def _session_run_prompt(prompt: str, calls: dict, tmp_path) -> str:
    if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
        calls["gap_closure"] += 1
        return json.dumps(
            {
                "final_text": "POLISHED",
                "closed_obligations": ["repo_step:use_cli_repo_final_review"],
                "remaining_obligations": [],
                "corrections_applied": ["Убрано неподтвержденное утверждение про desktop app."],
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
                "verdict": "Критичных расхождений после правок не осталось.",
                "closed_blocking_obligations": ["repo_step:use_cli_repo_final_review"],
                "open_blocking_obligations": [],
                "false_closures": [],
                "unsupported_assertions": [],
                "required_corrections": [],
                "claims": [
                    {
                        "claim_id": "claim_followup_1",
                        "status": "confirmed",
                        "text": "Неподтвержденное утверждение про desktop app удалено.",
                        "evidence": [
                            {
                                "type": "repo_evidence",
                                "path": str(Path(tmp_path) / "views" / "header.blade.php"),
                                "preview": "read_file: header.blade.php",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "type": "repo_evidence",
                        "path": str(Path(tmp_path) / "views" / "header.blade.php"),
                        "preview": "read_file: header.blade.php",
                    }
                ],
                "degraded_modes": [],
            },
            ensure_ascii=False,
        )
    raise AssertionError(f"Unexpected run_prompt call: {prompt[:200]}")


async def _spec_fix_claims_run_prompt(prompt: str, calls: dict, tmp_path, spec_fix_claim: dict) -> str:
    if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
        calls["gap_closure"] += 1
        evidence = list(spec_fix_claim.get("evidence") or [])
        return json.dumps(
            {
                "final_text": "POLISHED",
                "closed_obligations": ["repo_step:use_cli_repo_grounding"],
                "remaining_obligations": [],
                "corrections_applied": ["Уточнены repo-grounded claims."],
                "claims": [spec_fix_claim],
                "evidence": evidence,
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


def test_orchestrator_qc_uses_audit_template_prompts(tmp_path, monkeypatch) -> None:
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
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(tmp_path / "analyst_config.yaml"))

    (tmp_path / "analyst_config.yaml").write_text(
        """\
templates:
  default:
    name: "T-Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "QA-DEFAULT"
  audit:
    name: "T-Audit"
    description: "D"
    required_sections: ["S1"]
    system_prompt_addition: ""
    qa_prompt: "QA-AUDIT"
""",
        encoding="utf-8",
    )

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=get_template_for_session,
    )

    # Avoid real planning and any tool execution.
    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

    captured = {"qc_system": None, "qc_user": None}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        # Compose final answer uses response_format=None. QC assess uses response_format={"type": "json_object"}.
        if response_format is not None:
            captured["qc_system"] = system
            captured["qc_user"] = user
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "grounding evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    # Prevent memory update from calling additional LLM requests and polluting captured prompts.
    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type("S", (), {"id": "s1", "analyst_template_id": "audit"})

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    bot = _FakeBot()
    dest = {"chat_id": 1}

    asyncio.run(orch.run(session, "user request", bot, context=object(), dest=dest))

    assert captured["qc_system"] is not None
    assert captured["qc_user"] is not None
    assert "QA-AUDIT" in captured["qc_system"]
    assert "QA-DEFAULT" not in captured["qc_system"]
    assert '"unverified_claims"' in captured["qc_system"]
    assert "Не оставляй гипотезы" in captured["qc_system"]
    assert "telegram, desktop, miniapp" not in captured["qc_system"]
    assert "S1" in captured["qc_user"]
    assert "D0" not in captured["qc_user"]


def test_orchestrator_compose_artifacts_use_raw_user_request_instead_of_compiled_prompt(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-RAW",
            "repo_grounded_required": False,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    captured = {"compose_payload": None, "qc_user": None}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        del system
        if response_format is not None:
            captured["qc_user"] = user
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        prefix = "Материалы (JSON):\n"
        if user.startswith(prefix):
            captured["compose_payload"] = json.loads(user[len(prefix):])
        return "FINAL DOC"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "grounding evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    session = type(
        "S",
        (),
        {
            "id": "s-raw-user-query",
            "active_mode": "analyst",
            "analyst_template_id": "default",
            "analyst_source_user_text_runtime": "Подготовь ТЗ для analyst mode",
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    compiled_prompt = "SYSTEM PROMPT\nОтвет пользователя: Нужен Telegram path"
    out = asyncio.run(orch.run(session, compiled_prompt, _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "FINAL DOC"
    assert captured["compose_payload"] is not None
    assert captured["compose_payload"]["user_query"] == "Подготовь ТЗ для analyst mode"
    assert captured["compose_payload"]["clarification_answers"] == ["Нужен Telegram path"]
    assert captured["qc_user"] is not None
    assert "Исходный запрос пользователя:\nПодготовь ТЗ для analyst mode" in captured["qc_user"]
    assert "Полученные уточнения пользователя:\n- Нужен Telegram path" in captured["qc_user"]
    assert "SYSTEM PROMPT" not in captured["qc_user"]
    artifact_path = tmp_path / "_sandbox" / "chats" / "chat_1" / "_orchestrator" / "s-raw-user-query_original_user_text.md"
    assert artifact_path.read_text(encoding="utf-8").strip() == "Подготовь ТЗ для analyst mode"


def test_orchestrator_external_reference_section_is_conditional_and_does_not_leak_between_runs(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["Контекст"],
            "qa_prompt": "QA-EXTREF",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["Контекст"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
                "external_references_section": "Внешние референсы и примеры реализации",
                "external_references_conditional": True,
            },
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="step1", title="Context", instruction="collect", step_type="task")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="step1",
            status="ok",
            summary="Затронут app/services/session_transfer/service.py.",
            outputs=[{"type": "text", "content": "Подтвержден файл app/services/session_transfer/service.py"}],
            claims=[],
            tool_calls=[],
            next_questions=[],
        )

    captured = {"qc_users": []}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            captured["qc_users"].append(_user)
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "## Контекст\nКонтекст backend-зоны\n- Подтвержден файл app/services/session_transfer/service.py."

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-ext-ref",
            "analyst_template_id": "default",
            "analyst_intent_flags": {"document_kind": "spec", "clarification_is_blocking": False},
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    query_with_ref = (
        "Подготовь ТЗ для session transfer и используй как внешний референс "
        "https://github.com/vakovalskii/codedash"
    )
    out_with_ref = asyncio.run(orch.run(session, query_with_ref, _FakeBot(), context=object(), dest={"chat_id": 1}))

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    draft_with_ref = (artifacts_dir / "s-ext-ref_draft.md").read_text(encoding="utf-8")
    qc_user_with_ref = captured["qc_users"][0]

    assert "## Внешние референсы и примеры реализации" in out_with_ref
    assert "https://github.com/vakovalskii/codedash" in out_with_ref
    assert "app/services/session_transfer/service.py" in out_with_ref
    assert "requires-validation" in out_with_ref
    assert "- Извлечённый паттерн:" in out_with_ref
    assert "- Local mapping: app/services/session_transfer/service.py" in out_with_ref
    assert "- Статус адаптации: requires-validation" in out_with_ref
    assert "## Внешние референсы и примеры реализации" in draft_with_ref
    assert "- Источник: https://github.com/vakovalskii/codedash" in draft_with_ref
    assert "- Local mapping: app/services/session_transfer/service.py" in draft_with_ref
    assert "- Статус адаптации: requires-validation" in draft_with_ref
    assert "Repo evidence из выполненных шагов" in qc_user_with_ref
    assert "Внешние референсы и implementation guidance:" in qc_user_with_ref
    assert "- Источник: https://github.com/vakovalskii/codedash" in qc_user_with_ref
    assert (
        "- Извлечённый паттерн: Внешний референс из исходного запроса; "
        "использовать как пример реализации и источник паттернов для адаптации."
        in qc_user_with_ref
    )
    assert "- Local mapping: app/services/session_transfer/service.py" in qc_user_with_ref
    assert "- Статус адаптации: requires-validation" in qc_user_with_ref

    query_without_ref = "Подготовь ТЗ для session transfer без внешних ссылок"
    out_without_ref = asyncio.run(orch.run(session, query_without_ref, _FakeBot(), context=object(), dest={"chat_id": 1}))
    draft_without_ref = (artifacts_dir / "s-ext-ref_draft.md").read_text(encoding="utf-8")
    qc_user_without_ref = captured["qc_users"][1]

    assert "## Внешние референсы и примеры реализации" not in out_without_ref
    assert "## Внешние референсы и примеры реализации" not in draft_without_ref
    assert "Внешние референсы и implementation guidance:" not in qc_user_without_ref
    assert "- Статус адаптации: requires-validation" not in qc_user_without_ref
    assert "[Нужно уточнить локальные файлы/контракты для адаптации]" not in out_without_ref


def test_orchestrator_external_reference_section_uses_research_artifact_without_query_url(tmp_path, monkeypatch) -> None:
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
    research_path = tmp_path / "research-codedash-reference.md"
    research_path.write_text(
        "\n".join(
            [
                "# Codedash reference",
                "- В референсе есть пример session transfer для Codex.",
                "- Паттерн можно напрямую адаптировать для reader_codex.",
                "- Источник: https://github.com/vakovalskii/codedash",
            ]
        ),
        encoding="utf-8",
    )

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["Контекст"],
            "qa_prompt": "QA-EXTREF",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["Контекст"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
                "external_references_section": "Внешние референсы и примеры реализации",
                "external_references_conditional": True,
            },
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="step1", title="Research", instruction="collect", step_type="task")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="step1",
            status="ok",
            summary="Исследование показало пример для app/services/session_transfer/service.py.",
            outputs=[
                {"type": "file", "path": str(research_path)},
                {"type": "text", "content": "Локальная адаптация нужна в app/services/session_transfer/service.py"},
            ],
            claims=[],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "Контекст backend-зоны\n- Нужна адаптация app/services/session_transfer/service.py."

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-ext-research",
            "analyst_template_id": "default",
            "analyst_intent_flags": {"document_kind": "spec", "clarification_is_blocking": False},
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь ТЗ для session transfer по исследованию репозитория.",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    draft = (artifacts_dir / "s-ext-research_draft.md").read_text(encoding="utf-8")

    assert "## Внешние референсы и примеры реализации" in out
    assert "https://github.com/vakovalskii/codedash" in out
    assert "app/services/session_transfer/service.py" in out
    assert "direct-adapt" in out
    assert "Паттерн можно напрямую адаптировать для reader_codex." in out
    assert "- Local mapping: app/services/session_transfer/service.py" in out
    assert "- Статус адаптации: direct-adapt" in out
    assert f"- Артефакт исследования: {research_path}" in out
    assert "- Local mapping: app/services/session_transfer/service.py" in draft
    assert "- Статус адаптации: direct-adapt" in draft


def test_orchestrator_external_reference_loss_opens_blocking_obligation(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["Контекст"],
            "qa_prompt": "QA-EXTREF-LOSS",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["Контекст"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
                "external_references_section": "Внешние референсы и примеры реализации",
                "external_references_conditional": True,
            },
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="step1", title="Context", instruction="collect", step_type="task")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="step1",
            status="ok",
            summary="Подтвержден файл app/services/session_transfer/service.py.",
            outputs=[{"type": "text", "content": "Подтвержден файл app/services/session_transfer/service.py"}],
            claims=[],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return (
            "## Контекст\nПодтвержден файл app/services/session_transfer/service.py.\n\n"
            "## Внешние референсы и примеры реализации\n"
            "### Референс 1\n"
            "- Источник: https://github.com/vakovalskii/codedash"
        )

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-ext-ref-loss",
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": False,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
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

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь ТЗ для session transfer и используй как внешний референс "
            "https://github.com/vakovalskii/codedash",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    obligation_matrix = json.loads((artifacts_dir / "s-ext-ref-loss_obligation_matrix.json").read_text(encoding="utf-8"))
    open_gaps = (artifacts_dir / "s-ext-ref-loss_open_gaps.md").read_text(encoding="utf-8")

    assert "## Внешние референсы и примеры реализации" in out
    assert "Потеря внешних референсов и implementation guidance" in open_gaps
    assert "app/services/session_transfer/service.py" in open_gaps
    by_statement = {item["statement"]: item for item in obligation_matrix}
    assert (
        by_statement[
            "Сохранить implementation guidance для внешнего референса: "
            "https://github.com/vakovalskii/codedash "
            "-> app/services/session_transfer/service.py [requires-validation]"
        ]["status"]
        == "open"
    )


def test_orchestrator_external_reference_status_change_does_not_open_gap(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["Контекст"],
            "qa_prompt": "QA-EXTREF-STATUS",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["Контекст"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
                "external_references_section": "Внешние референсы и примеры реализации",
                "external_references_conditional": True,
            },
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="step1", title="Context", instruction="collect", step_type="task")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="step1",
            status="ok",
            summary="Подтвержден файл app/services/session_transfer/service.py.",
            outputs=[{"type": "text", "content": "Подтвержден файл app/services/session_transfer/service.py"}],
            claims=[],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return (
            "## Контекст\nПодтвержден файл app/services/session_transfer/service.py.\n\n"
            "## Внешние референсы и примеры реализации\n"
            "### Референс 1\n"
            "- Источник: https://github.com/vakovalskii/codedash\n"
            "- Local mapping: app/services/session_transfer/service.py\n"
            "- Статус адаптации: direct-adapt"
        )

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-ext-ref-status",
            "analyst_template_id": "default",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": False,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
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

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь ТЗ для session transfer и используй как внешний референс "
            "https://github.com/vakovalskii/codedash",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    obligation_matrix = json.loads((artifacts_dir / "s-ext-ref-status_obligation_matrix.json").read_text(encoding="utf-8"))
    open_gaps_path = artifacts_dir / "s-ext-ref-status_open_gaps.md"
    open_gaps = open_gaps_path.read_text(encoding="utf-8") if open_gaps_path.exists() else ""

    assert "## Внешние референсы и примеры реализации" in out
    assert "direct-adapt" in out
    assert "Потеря внешних референсов и implementation guidance" not in open_gaps
    by_statement = {item["statement"]: item for item in obligation_matrix}
    assert (
        by_statement[
            "Сохранить implementation guidance для внешнего референса: "
            "https://github.com/vakovalskii/codedash "
            "-> app/services/session_transfer/service.py [requires-validation]"
        ]["status"]
        == "closed"
    )


def test_orchestrator_qc_parse_error_does_not_silently_trigger_rework(tmp_path, monkeypatch) -> None:
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
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(tmp_path / "analyst_config.yaml"))
    (tmp_path / "analyst_config.yaml").write_text(
        """\
templates:
  default:
    name: "T-Default"
    description: "D"
    required_sections: ["D0"]
    system_prompt_addition: ""
    qa_prompt: "QA-DEFAULT"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=2,
        template_provider=get_template_for_session,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"final": 0, "qc": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return "not-json"
        calls["final"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        if getattr(step, "id", "") != "use_cli_repo_grounding":
            raise AssertionError(f"Unexpected step executed: {getattr(step, 'id', '?')}")
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "repo evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    session = type("S", (), {"id": "s1", "analyst_template_id": "default"})

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))
    assert out == "DRAFT"
    assert "Статус готовности" not in out
    assert "assessment model" not in out
    assert calls["final"] == 1
    assert calls["qc"] == 4


def test_orchestrator_task_contract_uses_merged_required_inputs(tmp_path, monkeypatch) -> None:
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
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(tmp_path / "analyst_config.yaml"))
    (tmp_path / "analyst_config.yaml").write_text(
        """\
templates:
  default:
    name: "T-Default"
    description: "D"
    required_sections: ["S1"]
    required_inputs: ["Template input"]
    system_prompt_addition: ""
    qa_prompt: "QA-DEFAULT"
    repo_grounded_required: true
""",
        encoding="utf-8",
    )

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=get_template_for_session,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    captured: dict[str, list[str]] = {}
    original_build_task_contract = orchestrator_runner_module.build_task_contract

    def _capture_build_task_contract(*args, **kwargs):
        captured["required_inputs"] = list(kwargs.get("required_inputs") or [])
        return original_build_task_contract(*args, **kwargs)

    monkeypatch.setattr(orchestrator_runner_module, "build_task_contract", _capture_build_task_contract)

    session = type(
        "S",
        (),
        {
            "id": "s-required-inputs",
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "needs_clarification": False,
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
                "clarification_topic": "",
                "clarification_question": "",
                "clarification_options": [],
                "required_inputs": ["Session-specific input"],
            },
        },
    )

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert captured["required_inputs"] == ["Session-specific input"]


def test_orchestrator_review_correction_is_preserved_in_rework_prompt(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": [],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
            "final_repo_review_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli"),
            PlanStep(id="use_cli_repo_final_review", title="review", instruction="review", step_type="use_cli"),
        ]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        if getattr(step, "id", "") == "use_cli_repo_grounding":
            return orch._deps.ExecutorResponse(
                task_id="use_cli_repo_grounding",
                status="ok",
                summary="grounding ok",
                outputs=[{"type": "text", "content": "repo evidence"}],
                claims=[],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )
        if getattr(step, "id", "") == "use_cli_repo_final_review":
            return orch._deps.ExecutorResponse(
                task_id="use_cli_repo_final_review",
                status="ok",
                summary="review ok",
                outputs=[
                    {
                        "type": CLIOutputType.REPO_REVIEW_CORRECTION,
                        "content": "Need one concrete correction",
                    }
                ],
                claims=[],
                tool_calls=[{"tool": "use_cli"}],
                next_questions=[],
            )
        raise AssertionError(f"Unexpected step: {getattr(step, 'id', '?')}")

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    class _Session:
        id = "s-review-correction"
        analyst_template_id = "default"

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [
                            "repo_step:use_cli_repo_grounding",
                            "repo_step:use_cli_repo_final_review",
                        ],
                        "remaining_obligations": [],
                        "corrections_applied": ["Need one concrete correction"],
                        "claims": [],
                        "evidence": [],
                        "degraded_modes": [],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "verdict": "Все blocking obligations закрыты.",
                        "closed_blocking_obligations": [
                            "repo_step:use_cli_repo_grounding",
                            "repo_step:use_cli_repo_final_review",
                        ],
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

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(_Session(), "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    open_gaps_path = tmp_path / "_sandbox" / "chats" / "chat_1" / "_orchestrator" / "s-review-correction_open_gaps.md"
    assert open_gaps_path.exists()
    assert "Need one concrete correction" in open_gaps_path.read_text(encoding="utf-8")


def test_orchestrator_repo_qc_and_rework_include_required_input_gaps(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "required_inputs": ["Template input"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    captured = {"qc_user": None, "gap_prompt": None}
    qc_calls = {"count": 0}
    repo_file = tmp_path / "app.py"
    repo_file.write_text("print('ok')\n", encoding="utf-8")
    task_contract = build_task_contract(
        user_query="user request",
        required_sections=["S1"],
        repo_grounded_required=True,
        required_inputs=["Template input", "Session-specific input"],
    )
    required_input_obligation_id = next(
        item["obligation_id"]
        for item in task_contract["task_obligations"]
        if item.get("assessment_target") == "Session-specific input"
    )

    async def _fake_chat_completion(_cfg, _system: str, user: str, response_format=None):
        if response_format is not None:
            qc_calls["count"] += 1
            captured["qc_user"] = user
            if qc_calls["count"] == 1:
                return (
                    '{"needs_rework": false, "issues": [], "missing_sections": [], '
                    '"required_input_gaps": ["Session-specific input"]}'
                )
            return '{"needs_rework": false, "issues": [], "missing_sections": [], "required_input_gaps": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        if getattr(step, "id", "") != "use_cli_repo_grounding":
            raise AssertionError(f"Unexpected step executed: {getattr(step, 'id', '?')}")
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[
                {
                    "type": "repo_evidence",
                    "path": str(repo_file),
                    "preview": "read_file: app.py",
                }
            ],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    async def _capture_run_prompt(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            captured["gap_prompt"] = prompt
            return json.dumps(
                {
                    "final_text": "POLISHED",
                    "closed_obligations": [required_input_obligation_id],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "claims": [],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "verdict": "Все blocking obligations закрыты.",
                    "closed_blocking_obligations": [required_input_obligation_id],
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

    session = type(
        "S",
        (),
        {
            "id": "s-required-input-gaps",
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "needs_clarification": False,
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
                "clarification_topic": "",
                "clarification_question": "",
                "clarification_options": [],
                "required_inputs": ["Session-specific input"],
            },
            "run_prompt": lambda self, prompt: _capture_run_prompt(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out.startswith("POLISHED")
    assert "Статус готовности" not in out
    assert "Пробелы evidence/traceability" not in out
    assert "Confirmed repo-grounded claim without repo/file anchor" not in out
    assert "Обязательные входы задачи:\n- Session-specific input" in str(captured["qc_user"])
    assert "не закрывай их разделом \"Допущения и незакрытые входы\"" in str(captured["gap_prompt"])
    assert "Открытые вопросы и валидационные шаги" in str(captured["gap_prompt"])
    assert "open gaps:" in str(captured["gap_prompt"]).lower()
    workspace_dir = tmp_path / "_sandbox" / "chats" / "chat_1"
    claim_ledger = json.loads(
        (workspace_dir / "_orchestrator" / "s-required-input-gaps_claim_ledger.json").read_text(encoding="utf-8")
    )
    claim_texts = {str(item.get("text") or "") for item in claim_ledger}
    assert "grounding ok" not in claim_texts
    assert "Все blocking obligations закрыты." not in claim_texts


def test_orchestrator_gap_closure_retries_once_on_retryable_cli_output(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        if calls["compose"] == 1:
            return "DRAFT"
        return '{"final_text":"POLISHED REWORKED"}'

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        if getattr(step, "id", "") != "use_cli_repo_grounding":
            raise AssertionError(f"Unexpected step executed: {getattr(step, 'id', '?')}")
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "grounding evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        def __init__(self):
            self.run_calls = 0

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                self.run_calls += 1
                if self.run_calls == 1:
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
    assert calls["gap_closure"] >= 2


def test_orchestrator_generic_repo_step_summary_does_not_become_claim(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": [],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "repo evidence but no file anchor"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    session = type("S", (), {"id": "s-generic-summary", "analyst_template_id": "default"})()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "DRAFT"
    assert "Статус готовности" not in out
    assert "Пробелы evidence/traceability" not in out
    workspace_dir = tmp_path / "_sandbox" / "chats" / "chat_1"
    claim_ledger = json.loads((workspace_dir / "_orchestrator" / "s-generic-summary_claim_ledger.json").read_text(encoding="utf-8"))
    assert claim_ledger == []


def test_orchestrator_gap_closure_retries_once_after_execution_failure(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        if getattr(step, "id", "") != "use_cli_repo_grounding":
            raise AssertionError(f"Unexpected step executed: {getattr(step, 'id', '?')}")
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "grounding evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        def __init__(self):
            self.run_calls = 0

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                self.run_calls += 1
                if self.run_calls == 1:
                    raise RuntimeError("temporary cli transport failure")
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": ["Усилено repo-grounded описание."],
                        "claims": [],
                        "evidence": [],
                        "degraded_modes": [],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "verdict": "Все blocking obligations закрыты.",
                        "closed_blocking_obligations": [],
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
    assert calls["gap_closure"] >= 2


def test_orchestrator_gap_closure_bundle_retries_once_on_open_gaps_persist_failure(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        if getattr(step, "id", "") != "use_cli_repo_grounding":
            raise AssertionError(f"Unexpected step executed: {getattr(step, 'id', '?')}")
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_grounding",
            status="ok",
            summary="grounding ok",
            outputs=[{"type": "text", "content": "grounding evidence"}],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    real_open = builtins.open
    fault = {"raised": False}

    def _flaky_open(file, mode="r", *args, **kwargs):
        path = str(file)
        if "w" in mode and path.endswith("_open_gaps.md") and not fault["raised"]:
            fault["raised"] = True
            raise OSError("temporary open_gaps write failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _flaky_open)

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": ["Усилено repo-grounded описание."],
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
                        "closed_blocking_obligations": [],
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
    assert "POLISHED" in out
    assert fault["raised"] is True
    assert calls["gap_closure"] >= 1
    assert calls["followup_review"] >= 1


def test_orchestrator_repo_gap_closure_empty_result_finishes_without_repeat_qc(
    tmp_path,
    monkeypatch,
) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1", "S2"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "spec",
            "compose_mode": "template_first",
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["S1"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
            },
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return '{"needs_rework": true, "issues": ["expand"], "missing_sections": ["S2"]}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

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

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    class _Session:
        id = "s1"
        analyst_template_id = "default"
        executor_profile = "analyst"

        def __init__(self):
            self.prompts = []

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            self.prompts.append(prompt)
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "final_text": "## S1\nГотово.\n\n## S2\nНужна доработка.",
                        "claims": [],
                        "evidence": [],
                        "open_gaps": [],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                return json.dumps(
                    {
                        "final_text": "",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": [],
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

    session = _Session()
    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))
    assert out
    assert calls["compose"] == 0
    assert calls["gap_closure"] == 2
    assert calls["qc"] == 1
    assert len(session.prompts) == 3


def test_orchestrator_gap_closure_polished_draft_persist_retries_once(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

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

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    real_open = builtins.open
    fault = {"raised": False}

    def _flaky_open(file, mode="r", *args, **kwargs):
        path = str(file)
        if "w" in mode and path.endswith("_draft_polished.md") and not fault["raised"]:
            fault["raised"] = True
            raise OSError("temporary polished draft write failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _flaky_open)

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": ["Усилено repo-grounded описание."],
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
                        "closed_blocking_obligations": [],
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
    assert "POLISHED" in out
    assert fault["raised"] is True
    assert calls["gap_closure"] >= 1
    assert calls["followup_review"] >= 1


def test_orchestrator_followup_review_persist_retries_once_after_payload_parse(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

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

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    real_open = builtins.open
    fault = {"raised": False}

    def _flaky_open(file, mode="r", *args, **kwargs):
        path = str(file)
        if "w" in mode and path.endswith("_obligation_review_followup.json") and not fault["raised"]:
            fault["raised"] = True
            raise OSError("temporary followup review persist failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _flaky_open)

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                calls["gap_closure"] += 1
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": ["Усилено repo-grounded описание."],
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
                        "closed_blocking_obligations": [],
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
    assert "POLISHED" in out
    assert fault["raised"] is True
    assert calls["gap_closure"] >= 1
    assert calls["followup_review"] >= 1


def test_orchestrator_spec_fix_degraded_modes_affect_final_readiness(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            if '"needs_rework": true' not in (_user or ""):
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

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

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    class _Session:
        id = "s1"
        analyst_template_id = "default"

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": [],
                        "claims": [],
                        "evidence": [],
                        "degraded_modes": ["spec_fixer execution_failed_partial_context"],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "verdict": "Все blocking obligations закрыты.",
                        "closed_blocking_obligations": [],
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
    assert "Статус готовности" not in out
    assert "Незакрытые blocking obligations" not in out
    assert "Критичные degraded runtime/CLI режимы" not in out


def test_orchestrator_followup_review_can_clear_initial_invalid_bundle_runtime_gap(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            if '"needs_rework": true' not in (_user or ""):
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="grounding ok",
            outputs=[
                {"type": "text", "content": "grounding evidence"},
                {
                    "type": CLIOutputType.DEGRADED_MODE,
                    "content": "use_cli response_format=repo_review_bundle_json invalid_bundle_fallback_to_text",
                    "content_preview": "use_cli response_format=repo_review_bundle_json invalid_bundle_fallback_to_text",
                },
            ],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    class _Session:
        id = "s-followup-clears-invalid-bundle"
        analyst_template_id = "default"

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "final_text": "POLISHED",
                        "closed_obligations": [],
                        "remaining_obligations": [],
                        "corrections_applied": [],
                        "claims": [],
                        "evidence": [],
                        "degraded_modes": [],
                    },
                    ensure_ascii=False,
                )
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "verdict": "Все blocking obligations закрыты.",
                        "closed_blocking_obligations": [],
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
    assert "Статус готовности" not in out
    assert "Критичные degraded runtime/CLI режимы" not in out


def test_orchestrator_repo_grounded_rework_does_not_fallback_to_chat_without_cli(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "output_kind": "analysis",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_grounding", title="grounding", instruction="ground", step_type="use_cli")]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

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

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    session = type("S", (), {"id": "s1", "analyst_template_id": "default"})()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))
    assert out == "DRAFT"
    assert "Статус готовности" not in out
    assert "Blocking-step retry exhausted: 1" not in out
    assert calls["compose"] == 1
    assert calls["qc"] >= 2


def test_orchestrator_repo_grounded_cli_polish_uses_artifacts_before_final_qc(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["S1"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
                "external_references_section": "Внешние референсы и примеры реализации",
                "external_references_conditional": True,
            },
            "repo_grounded_required": True,
            "target_size_hint": "large",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(id="step1", title="Repo findings", instruction="collect", step_type="task"),
            PlanStep(id="use_cli_repo_final_review", title="Final repo review", instruction="review", step_type="use_cli"),
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="Подтверждено: меню содержит логин и account dropdown.",
            outputs=[{"type": "text", "content": "evidence from repo files"}],
            claims=[
                {
                    "claim_id": "claim_step1_1",
                    "status": "confirmed",
                    "text": "Меню содержит login CTA.",
                    "evidence": [{"type": "text", "path": str(tmp_path / "views" / "header.blade.php"), "preview": "login CTA in header"}],
                },
                {
                    "claim_id": "claim_step1_2",
                    "status": "confirmed",
                    "text": "В header есть account dropdown.",
                    "evidence": [
                        {
                            "type": "text",
                            "path": str(tmp_path / "views" / "header.blade.php"),
                            "preview": "account dropdown in header",
                        }
                    ],
                },
            ],
            tool_calls=[{"tool": "read_file"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    calls = {"compose": 0, "qc": 0, "cli": 0}
    captured = {"cli_prompts": []}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return (
                    '{"needs_rework": true, "issues": ["Need stronger repo grounding"], '
                    '"missing_sections": [], "codebase_mismatches": ["gap-1"]}'
                )
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT V1"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    class _Session:
        id = "s1"
        workdir = str(tmp_path)
        project_root = str(tmp_path)
        analyst_intent_flags = {
            "document_kind": "spec",
            "requires_codebase_grounding": True,
            "requires_repo_audit": False,
            "requires_final_repo_review": True,
            "clarification_is_blocking": False,
        }

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del args, kwargs
            calls["cli"] += 1
            captured["cli_prompts"].append(prompt)
            if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
                return json.dumps(
                    {
                        "final_text": "DRAFT POLISHED",
                        "closed_obligations": ["repo_step:use_cli_repo_final_review"],
                        "remaining_obligations": [],
                        "corrections_applied": ["Добавлено repo-grounded уточнение."],
                        "claims": [
                            {
                                "claim_id": "claim_polish_1",
                                "status": "confirmed",
                                "text": "В header есть account dropdown.",
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
                        "verdict": "Критичных расхождений после правок не осталось.",
                        "closed_blocking_obligations": ["repo_step:use_cli_repo_final_review"],
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

    assert "DRAFT POLISHED" in out
    assert calls["compose"] >= 1
    assert calls["qc"] >= 2
    assert calls["cli"] >= 2
    assert captured["cli_prompts"]
    assert any("Верни строго JSON-объект" in prompt for prompt in captured["cli_prompts"])
    assert any("claim ledger" in prompt.lower() for prompt in captured["cli_prompts"])
    assert any("fact pack" in prompt.lower() for prompt in captured["cli_prompts"])
    assert any("open gaps" in prompt.lower() for prompt in captured["cli_prompts"])

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    assert (artifacts_dir / "step1.md").exists()
    assert (artifacts_dir / "s1_claim_ledger.json").exists()
    assert (artifacts_dir / "s1_fact_pack.md").exists()
    assert (artifacts_dir / "s1_open_gaps.md").exists()
    assert (artifacts_dir / "s1_draft.md").exists()
    assert (artifacts_dir / "s1_draft_polished.md").exists()
    assert (artifacts_dir / "s1_repo_final_review_draft.md").exists()
    repo_review_draft = (artifacts_dir / "s1_repo_final_review_draft.md").read_text(encoding="utf-8")
    persisted_draft = (artifacts_dir / "s1_draft.md").read_text(encoding="utf-8")
    polished_draft = (artifacts_dir / "s1_draft_polished.md").read_text(encoding="utf-8")
    for content in (repo_review_draft, persisted_draft, polished_draft):
        assert "# Техническое задание" in content
        assert "## Исходная задача" in content
        assert "## Открытые вопросы и валидационные шаги" in content
    open_gaps_text = (artifacts_dir / "s1_open_gaps.md").read_text(encoding="utf-8")
    assert "## Structural QC" in open_gaps_text
    assert "## Evidence QC" in open_gaps_text
    index_path = artifacts_dir / "s1_artifacts_index.json"
    assert index_path.exists()
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    artifact_paths = {str(item.get("path") or "") for item in index_payload.get("artifacts") or []}
    assert str(artifacts_dir / "step1.md") in artifact_paths
    assert str(artifacts_dir / "s1_claim_ledger.json") in artifact_paths
    assert str(artifacts_dir / "s1_fact_pack.md") in artifact_paths
    ledger_payload = json.loads((artifacts_dir / "s1_claim_ledger.json").read_text(encoding="utf-8"))
    claim_texts = [str(item.get("text") or "") for item in ledger_payload]
    assert "Меню содержит login CTA." in claim_texts
    assert "В header есть account dropdown." in claim_texts


def test_orchestrator_repo_grounded_cli_polish_falls_back_when_bundle_missing_final_text(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
            "target_size_hint": "large",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(id="step1", title="Repo findings", instruction="collect", step_type="task"),
            PlanStep(id="use_cli_repo_final_review", title="Final review",
                     instruction=f"review in {tmp_path}", step_type="use_cli"),
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="Подтверждено: меню содержит логин.",
            outputs=[{"type": "text", "content": "evidence from repo files"}],
            claims=[],
            tool_calls=[{"tool": "read_file"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)

    calls = {"compose": 0, "qc": 0, "cli": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return (
                    '{"needs_rework": true, "issues": ["Need stronger repo grounding"], '
                    '"missing_sections": [], "codebase_mismatches": ["gap-1"]}'
                )
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT V1"

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    class _Session:
        id = "s1"
        workdir = str(tmp_path)
        project_root = str(tmp_path)
        analyst_intent_flags = {
            "document_kind": "spec",
            "requires_codebase_grounding": True,
            "requires_repo_audit": False,
            "requires_final_repo_review": True,
            "clarification_is_blocking": False,
        }

        async def run_prompt(self, prompt: str, *args, **kwargs) -> str:
            del prompt, args, kwargs
            calls["cli"] += 1
            return json.dumps(
                {
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "claims": [
                        {
                            "claim_id": "claim_polish_1",
                            "status": "confirmed",
                            "text": "В header есть account dropdown.",
                        }
                    ],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(_Session(), "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out is not None
    assert out == "DRAFT V1"
    assert "Статус готовности" not in out
    assert "Незакрытые blocking obligations" not in out
    assert calls["cli"] >= 1
    assert calls["compose"] >= 1
    assert calls["cli"] >= 2


def test_orchestrator_large_spec_keeps_base_qc_pass_count_when_not_repo_grounded(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Large Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-LARGE",
            "target_size_hint": "large",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    calls = {"compose": 0, "qc": 0, "rework": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        calls["rework"] += 1
        return '{"final_text":"REVISED"}'

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type("S", (), {"id": "s1", "analyst_template_id": "default"})

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))
    assert out == "REVISED"
    assert calls["compose"] == 1
    assert calls["rework"] == 1
    assert calls["qc"] == 1


def test_orchestrator_prepends_runtime_readiness_status_for_repo_grounded_result(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(id="use_cli_repo_final_review", title="Final review",
                     instruction=f"review in {tmp_path}", step_type="use_cli"),
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id, status="ok", summary="review ok",
            outputs=[{"type": "text", "content": "evidence"}],
            claims=[], tool_calls=[], next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": ["S1"]}'
        return "Статус: Готово к реализации\n\nЧерновик документа"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
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

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "Черновик документа"
    assert "Статус готовности" not in out
    assert "Статус: Готово к реализации" not in out
    assert "runtime_verdict" not in out
    assert "blocking_reasons" not in out
    assert "warning_reasons" not in out


def test_orchestrator_runtime_readiness_blocks_confirmed_claim_without_repo_anchor(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(id="step1", title="collect", instruction="collect", step_type="task"),
            PlanStep(id="use_cli_repo_final_review", title="Final review",
                     instruction=f"review in {tmp_path}", step_type="use_cli"),
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="Подтверждено наблюдение по интерфейсу",
            outputs=[{"type": "text", "content": "header contains account dropdown"}],
            claims=[
                {
                    "claim_id": "claim_step1_1",
                    "status": "confirmed",
                    "text": "В header есть account dropdown.",
                    "evidence": [{"type": "text", "path": "", "preview": "account dropdown exists"}],
                }
            ],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "Статус: Готово к реализации\n\nЧерновик документа"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
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

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "Черновик документа"
    assert "Статус готовности" not in out
    assert "Незакрытые blocking obligations: 2" not in out


def test_orchestrator_runtime_readiness_uses_structured_repo_review_outputs(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction="final review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[
                {"type": "text", "content": "review summary"},
                {"type": CLIOutputType.REPO_REVIEW_MISMATCH, "content": "Документ утверждает наличие Telegram WebApp без repo evidence."},
                {"type": CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM, "content": "Telegram WebApp не подтвержден в репозитории."},
                {"type": CLIOutputType.REPO_REVIEW_CORRECTION, "content": "Заменить утверждение на 'не подтверждено'."},
            ],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "Статус: Готово к реализации\n\nЧерновик документа"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
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

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "Черновик документа"
    assert "Статус готовности" not in out
    assert "Незакрытые blocking obligations: 3" not in out


def test_orchestrator_forces_one_rework_pass_from_structured_repo_review_corrections(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=2,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction="final review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[
                {"type": "text", "content": "review summary", "path": str(tmp_path / "app.py")},
                {"type": CLIOutputType.REPO_REVIEW_CORRECTION, "content": "Убрать неподтвержденное утверждение про desktop app."},
            ],
            claims=[
                {
                    "claim_id": "claim_review_1",
                    "status": "confirmed",
                    "text": "repo final review ok",
                    "evidence": [{"type": "text", "path": str(tmp_path / "app.py"), "preview": "review summary"}],
                }
            ],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    calls = {"compose": 0, "qc": 0, "rework": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        calls["rework"] += 1
        return json.dumps({"final_text": f"REVISED-{calls['rework']}"}, ensure_ascii=False)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _session_run_prompt(prompt, calls, tmp_path),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    assert calls["compose"] >= 1
    assert calls["gap_closure"] >= 1
    assert calls["followup_review"] >= 1
    workspace_dir = tmp_path / "_sandbox" / "chats" / "chat_1"
    claim_ledger = json.loads((workspace_dir / "_orchestrator" / "s1_claim_ledger.json").read_text(encoding="utf-8"))
    assert any(item.get("claim_id") == "claim_followup_1" for item in claim_ledger)


def test_orchestrator_forces_rework_from_repo_review_open_gap_output(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=2,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction="final review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[
                {"type": "text", "content": "review summary", "path": str(tmp_path / "app.py")},
                {"type": "open_gap", "content": "Нужно закрыть один repo-grounded gap."},
            ],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-open-gap",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _session_run_prompt(prompt, calls, tmp_path),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    assert calls["compose"] >= 1
    assert calls["gap_closure"] >= 1
    assert calls["followup_review"] >= 1


def test_orchestrator_followup_open_blockers_do_not_trigger_repeat_rework(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["Контекст", "Требования"],
            "qa_prompt": "QA-REPO",
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=2,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction="final review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[
                {"type": "text", "content": "review summary", "path": str(tmp_path / "app.py")},
                {
                    "type": CLIOutputType.REPO_REVIEW_CORRECTION,
                    "content": "Нужно явно сохранить отдельную валидацию нативного Codex transcript schema.",
                },
            ],
            claims=[],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    async def _session_run_prompt(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            calls["gap_closure"] += 1
            return json.dumps(
                {
                    "final_text": (
                        "## Контекст\n"
                        "Подтвержден текущий runtime session-transfer слой.\n\n"
                        "## Требования\n"
                        "- Добавляем reader/writer seam для Codex только после отдельной schema validation.\n\n"
                        "## Открытые вопросы и валидационные шаги\n"
                        "- Проверить нативный Codex transcript schema перед реализацией writer path."
                    ),
                    "closed_obligations": [],
                    "remaining_obligations": [
                        {
                            "obligation_id": "codex_schema_validation",
                            "statement": "Нативный формат Codex transcript schema требует отдельной проверки.",
                            "status": "open",
                            "blocking": True,
                        }
                    ],
                    "corrections_applied": [
                        "Неподтвержденная часть вынесена в открытый validation step вместо ложного факта."
                    ],
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
                    "verdict": "Открытый blocking validation шаг сохранен корректно; дополнительных правок не требуется.",
                    "closed_blocking_obligations": [],
                    "open_blocking_obligations": [
                        {
                            "obligation_id": "codex_schema_validation",
                            "statement": "Нативный формат Codex transcript schema требует отдельной проверки.",
                            "status": "open",
                            "blocking": True,
                        }
                    ],
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

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    session = type(
        "S",
        (),
        {
            "id": "s-followup-open-blocker",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _session_run_prompt(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "Проверить нативный Codex transcript schema" in out
    assert calls["compose"] >= 1
    assert calls["gap_closure"] == 1
    assert calls["followup_review"] == 1
    assert calls["qc"] >= 2


def test_orchestrator_persists_spec_fix_claims_into_claim_ledger(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

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

    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    spec_fix_claim = {
        "claim_id": "claim_spec_fix_1",
        "status": "confirmed",
        "text": "Исправленный документ явно ограничивает scope только подтвержденными файлами.",
        "evidence": [
            {
                "type": "repo_evidence",
                "path": str(tmp_path / "app.py"),
                "preview": "read_file: app.py",
            }
        ],
    }

    session = type(
        "S",
        (),
        {
            "id": "s-spec-fix-claims",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _spec_fix_claims_run_prompt(prompt, calls, tmp_path, spec_fix_claim),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    workspace_dir = tmp_path / "_sandbox" / "chats" / "chat_1"
    claim_ledger = json.loads((workspace_dir / "_orchestrator" / "s-spec-fix-claims_claim_ledger.json").read_text(encoding="utf-8"))
    assert any(item.get("claim_id") == "claim_spec_fix_1" for item in claim_ledger)


def test_orchestrator_followup_review_parses_wrapped_structured_cli_output(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=2,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction="final review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[
                {"type": "text", "content": "review summary", "path": str(tmp_path / "app.py")},
                {"type": CLIOutputType.REPO_REVIEW_CORRECTION, "content": "Убрать неподтвержденное утверждение про desktop app."},
            ],
            claims=[
                {
                    "claim_id": "claim_review_1",
                    "status": "confirmed",
                    "text": "repo final review ok",
                    "evidence": [{"type": "text", "path": str(tmp_path / "app.py"), "preview": "review summary"}],
                }
            ],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    calls = {"compose": 0, "qc": 0, "rework": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        calls["rework"] += 1
        return json.dumps({"final_text": f"REVISED-{calls['rework']}"}, ensure_ascii=False)

    async def _wrapped_session_run_prompt(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            calls["gap_closure"] += 1
            payload = {
                "final_text": "POLISHED",
                "closed_obligations": ["repo_step:use_cli_repo_final_review"],
                "remaining_obligations": [],
                "corrections_applied": ["Убрано неподтвержденное утверждение."],
                "claims": [],
                "evidence": [],
                "degraded_modes": [],
            }
            return f"Комментарий перед structured output.\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            calls["followup_review"] += 1
            payload = {
                "verdict": "Критичных расхождений после правок не осталось.",
                "closed_blocking_obligations": ["repo_step:use_cli_repo_final_review"],
                "open_blocking_obligations": [],
                "false_closures": [],
                "unsupported_assertions": [],
                "required_corrections": [],
                "claims": [
                    {
                        "claim_id": "claim_followup_wrapped_1",
                        "status": "confirmed",
                        "text": "Неподтвержденное утверждение про desktop app удалено.",
                        "evidence": [
                            {
                                "type": "repo_evidence",
                                "path": str(Path(tmp_path) / "views" / "header.blade.php"),
                                "preview": "read_file: header.blade.php",
                            }
                        ],
                    }
                ],
                "evidence": [
                    {
                        "type": "repo_evidence",
                        "path": str(Path(tmp_path) / "views" / "header.blade.php"),
                        "preview": "read_file: header.blade.php",
                    }
                ],
                "degraded_modes": [],
            }
            return (
                "\x1b[33mwarning:\x1b[0m extra prefix\n"
                f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```\n"
                "extra suffix"
            )
        raise AssertionError(f"Unexpected run_prompt call: {prompt[:200]}")

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _wrapped_session_run_prompt(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    assert calls["gap_closure"] == 1
    assert calls["followup_review"] >= 1
    workspace_dir = tmp_path / "_sandbox" / "chats" / "chat_1"
    claim_ledger = json.loads((workspace_dir / "_orchestrator" / "s1_claim_ledger.json").read_text(encoding="utf-8"))
    assert any(item.get("claim_id") == "claim_followup_wrapped_1" for item in claim_ledger)


def test_orchestrator_large_spec_respects_explicit_qc_pass_limit_when_repo_grounding_comes_from_intent_flags(
    tmp_path,
    monkeypatch,
) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Large Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-LARGE",
            "target_size_hint": "large",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction=f"Сделай финальную сверку ТЗ с репозиторием через CLI в директории:\n{tmp_path}",
                step_type="use_cli",
                depends_on=[],
                parallel_group=None,
                parallelizable=False,
                parallelizable_reason=None,
                ask_question=None,
                ask_options=None,
            )
        ]

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

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
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[{"type": "text", "content": "repo final review evidence", "path": str(tmp_path / "app.py")}],
            claims=[
                {
                    "claim_id": "claim_review_1",
                    "status": "confirmed",
                    "text": "repo final review ok",
                    "evidence": [{"type": "text", "path": str(tmp_path / "app.py"), "preview": "evidence"}],
                }
            ],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] < 3:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "analyst_template_id": "default",
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _session_run_prompt(prompt, calls, tmp_path),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))
    assert "POLISHED" in out
    assert calls["compose"] >= 1
    assert calls["gap_closure"] == 1
    assert calls["followup_review"] == 1
    assert calls["qc"] == 2


def test_orchestrator_repo_qc_counts_initial_rework_against_two_pass_budget(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=2,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_final_review",
                title="final review",
                instruction="final review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo final review ok",
            outputs=[
                {"type": "text", "content": "review summary", "path": str(tmp_path / "app.py")},
                {"type": CLIOutputType.REPO_REVIEW_CORRECTION, "content": "Убрать неподтвержденное утверждение."},
            ],
            claims=[
                {
                    "claim_id": "claim_review_1",
                    "status": "confirmed",
                    "text": "repo final review ok",
                    "evidence": [{"type": "text", "path": str(tmp_path / "app.py"), "preview": "review summary"}],
                }
            ],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] < 5:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        calls["compose"] += 1
        return "DRAFT"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "workdir": str(tmp_path),
            "project_root": str(tmp_path),
            "analyst_template_id": "default",
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": False,
                "requires_final_repo_review": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _session_run_prompt(prompt, calls, tmp_path),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    assert calls["compose"] >= 1
    assert calls["gap_closure"] == 2
    assert calls["followup_review"] == 2
    assert calls["qc"] == 4


def test_orchestrator_large_spec_qc_requests_extended_checks_and_uses_them(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Large Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-LARGE",
            "target_size_hint": "large",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    calls = {"compose": 0, "qc": 0, "rework": 0}
    captured = {"qc_system": None}

    async def _fake_chat_completion(_cfg, system: str, _user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if captured["qc_system"] is None:
                captured["qc_system"] = system
            if calls["qc"] == 1:
                return (
                    '{"needs_rework": false, "issues": [], "missing_sections": [], '
                    '"weak_sections": ["Пользовательские сценарии"], '
                    '"missing_counts": ["Недостаточно FR"], '
                    '"traceability_gaps": ["FR-001 без приемки"]}'
                )
            return (
                '{"needs_rework": false, "issues": [], "missing_sections": [], '
                '"weak_sections": [], "missing_counts": [], "traceability_gaps": []}'
            )
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        calls["rework"] += 1
        return '{"final_text":"REVISED"}'

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type("S", (), {"id": "s1", "analyst_template_id": "default"})

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "REVISED"
    assert calls["compose"] == 1
    assert calls["rework"] == 1
    assert calls["qc"] == 1
    assert captured["qc_system"] is not None
    assert '"weak_sections"' in captured["qc_system"]
    assert '"missing_counts"' in captured["qc_system"]
    assert '"traceability_gaps"' in captured["qc_system"]
    assert "не противоречит ли ТЗ текущему коду" in captured["qc_system"]
    assert "не введены ли новые сущности" in captured["qc_system"]
    assert "не названы ли как факты интеграции, поверхности или capability" in captured["qc_system"]
    assert "config/docs/tests" in captured["qc_system"]
    assert "telegram, desktop, miniapp" not in captured["qc_system"]
    assert "low-middle разработчика без устных пояснений" in captured["qc_system"]


def test_orchestrator_repo_spec_execution_handoff_and_placeholder_gaps_trigger_rework(
    tmp_path,
    monkeypatch,
) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["Контекст", "Implementation handoff по компонентам и файлам"],
            "qa_prompt": "QA-REPO-SPEC",
            "repo_grounded_required": True,
            "output_kind": "spec",
            "compose_mode": "template_first",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])
    calls = {"compose": 0, "qc": 0, "gap_closure": 0, "followup_review": 0}
    captured = {"qc_system": None, "rework_user": None}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if captured["qc_system"] is None:
                captured["qc_system"] = system
                if calls["qc"] == 1:
                    return (
                        '{"needs_rework": false, "issues": [], "missing_sections": [], '
                        '"required_input_gaps": [], "placeholder_gaps": [], '
                        '"implementation_handoff_gaps": [], '
                        '"spec_to_plan_gaps": [], '
                        '"codebase_mismatches": [], "unsupported_assumptions": [], '
                        '"unverified_claims": [], "evidence_gaps": [], "config_contract_gaps": [], '
                        '"migration_gaps": [], "doc_sync_gaps": [], "test_gaps": []}'
                    )
            return (
                '{"needs_rework": false, "issues": [], "missing_sections": [], '
                '"required_input_gaps": [], "placeholder_gaps": [], '
                '"implementation_handoff_gaps": [], "spec_to_plan_gaps": [], '
                '"codebase_mismatches": [], "unsupported_assumptions": [], '
                '"unverified_claims": [], "evidence_gaps": [], "config_contract_gaps": [], '
                '"migration_gaps": [], "doc_sync_gaps": [], "test_gaps": []}'
            )
        if calls["compose"] == 0:
            calls["compose"] += 1
            return (
                "## Контекст\n\n"
                "FR-001: Пользователь должен сохранить изменения.\n\n"
                "## Implementation handoff по компонентам и файлам\n\n"
                "TODO\n"
            )
        raise AssertionError("Unexpected non-QC chat_completion call")

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    async def _session_run_prompt(prompt: str):
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            calls["gap_closure"] += 1
            captured["rework_user"] = prompt
            return json.dumps(
                {
                    "final_text": (
                        "## Контекст\n\n"
                        "FR-001: Пользователь должен сохранить изменения.\n\n"
                        "## Implementation handoff по компонентам и файлам\n\n"
                        "- Компонент/файл: app.py\n"
                        "- Что меняется: обновляем обработчик сохранения.\n"
                        "- Как проверить: выполнить сценарий сохранения и убедиться, что ответ успешный.\n"
                        "- Тесты/команды: .venv/bin/pytest -q tests/test_app.py\n"
                    ),
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": ["Closed placeholder and execution handoff gaps"],
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
                    "closed_blocking_obligations": [],
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

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "analyst_template_id": "default",
            "run_prompt": lambda self, prompt: _session_run_prompt(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "Компонент/файл: app.py" in out
    assert calls["compose"] == 1
    assert calls["gap_closure"] >= 1
    assert calls["followup_review"] >= 1
    assert calls["qc"] >= 2
    assert captured["qc_system"] is not None
    assert '"placeholder_gaps"' in captured["qc_system"]
    assert '"implementation_handoff_gaps"' in captured["qc_system"]
    assert '"spec_to_plan_gaps"' in captured["qc_system"]
    assert "execution-ready repo-grounded spec" in captured["qc_system"]
    assert captured["rework_user"] is not None


def test_orchestrator_qc_and_rework_receive_repo_evidence_from_use_cli_steps(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "target_size_hint": "large",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_audit",
                title="audit",
                instruction=f"Сделай начальный аудит репозитория через CLI в директории:\n{tmp_path}",
                step_type="use_cli",
                depends_on=[],
                parallel_group=None,
                parallelizable=False,
                parallelizable_reason=None,
                ask_question=None,
                ask_options=None,
            )
        ]

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
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="repo audit summary",
            outputs=[{"type": "text", "content": "repo evidence preview"}],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    captured = {"qc_user": None, "gap_prompt": None, "followup_prompt": None}
    calls = {"compose": 0, "qc": 0}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            captured["qc_user"] = user
            if calls["qc"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        raise AssertionError("repo-grounded rework must go through CLI")

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s1",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "analysis",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _capture_repo_grounded_prompts(prompt, captured),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    async def _capture_repo_grounded_prompts(prompt: str, captured_prompts: dict) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            captured_prompts["gap_prompt"] = prompt
            return json.dumps(
                {
                    "final_text": "POLISHED",
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "claims": [],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            captured_prompts["followup_prompt"] = prompt
            return json.dumps(
                {
                    "verdict": "Все blocking obligations закрыты.",
                    "closed_blocking_obligations": [],
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

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert "POLISHED" in out
    assert "Claim ledger artifact" in str(captured["qc_user"])
    assert "Repo evidence из выполненных шагов" in str(captured["qc_user"])
    assert "repo audit summary" in str(captured["qc_user"])
    assert "repo evidence preview" in str(captured["qc_user"])
    assert "claim ledger:" in str(captured["gap_prompt"]).lower()
    assert "fact pack:" in str(captured["gap_prompt"]).lower()
    assert "task contract:" in str(captured["gap_prompt"]).lower()
    assert "obligation matrix:" in str(captured["gap_prompt"]).lower()
    assert "preservation-first patch/merge" in str(captured["gap_prompt"])
    assert "preservation regression" in str(captured["gap_prompt"])
    assert "не закрывай их разделом \"Допущения и незакрытые входы\"" in str(captured["gap_prompt"])
    assert "manual validation gate" in str(captured["gap_prompt"])
    assert "out of scope" in str(captured["gap_prompt"])
    assert "реализационная деталь" in str(captured["gap_prompt"])
    assert "claim ledger:" in str(captured["followup_prompt"]).lower()
    assert "expected draft sha1:" in str(captured["followup_prompt"]).lower()
    assert "rewrite-from-scratch" in str(captured["followup_prompt"])
    assert "preservation regression" in str(captured["followup_prompt"])
    assert "его не закрыли ложным разделом \"Допущения и незакрытые входы\"" in str(captured["followup_prompt"])
    assert "manual validation gate" in str(captured["followup_prompt"])
    assert "out of scope" in str(captured["followup_prompt"])
    assert "реализационная деталь" in str(captured["followup_prompt"])


def test_orchestrator_followup_review_detects_stale_validated_artifact(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo spec",
            "required_sections": ["Контекст", "Требования"],
            "qa_prompt": "QA-STALE-REVIEW",
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_audit",
                title="review",
                instruction="repo review",
                step_type="use_cli",
            )
        ]

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
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_audit",
            status="ok",
            summary="repo audit summary",
            outputs=[{"type": "text", "content": "repo evidence preview"}],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    qc_calls = {"count": 0}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        del system, user
        if response_format is not None:
            qc_calls["count"] += 1
            if qc_calls["count"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Реализовать поведение."

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    captured = {"followup_prompt": None}

    async def _capture_repo_grounded_prompts(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "final_text": "## Требования\nОбновлённые требования после patch/merge.",
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "claims": [],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            captured["followup_prompt"] = prompt
            followup_path_line = next(
                line.strip()
                for line in prompt.splitlines()
                if line.strip().endswith("_repo_final_review_followup_draft.md")
            )
            followup_path = followup_path_line.split(":", 1)[1].strip()
            Path(followup_path).write_text(
                "# Техническое задание\n\n## Исходная задача\nstale rewrite\n",
                encoding="utf-8",
            )
            return json.dumps(
                {
                    "verdict": "Все blocking obligations закрыты.",
                    "closed_blocking_obligations": [],
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

    session = type(
        "S",
        (),
        {
            "id": "s-stale-followup-review",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _capture_repo_grounded_prompts(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert "# Техническое задание" in out
    assert captured["followup_prompt"] is not None
    assert "expected draft sha1:" in str(captured["followup_prompt"]).lower()
    artifacts_dir = tmp_path / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    payload = json.loads(
        (artifacts_dir / "s-stale-followup-review_obligation_review_followup.json").read_text(encoding="utf-8")
    )
    assert payload["validated_artifact"]["path"].endswith("_repo_final_review_followup_draft.md")
    assert payload["validated_artifact"]["actual_path"].endswith("_repo_final_review_followup_draft.md")
    assert payload["validated_artifact"]["sha1"]
    assert payload["validated_artifact"]["actual_sha1"]
    assert payload["validated_artifact"]["stale"] is True
    assert "Verifier validated stale persisted draft artifact:" in payload["verdict"]
    assert "expected_sha1=" in payload["verdict"]
    assert any(
        "_repo_final_review_followup_draft.md" in str(item)
        and "expected_sha1=" in str(item)
        for item in (payload.get("required_corrections") or [])
    )
    assert any(
        item.get("obligation_id") == "followup_review:artifact_binding"
        and item.get("evidence_refs") == [payload["validated_artifact"]["path"]]
        for item in payload.get("open_blocking_obligations") or []
    )
    assert any(
        item.get("obligation_id") == "followup_review:artifact_binding"
        and item.get("evidence_refs") == [payload["validated_artifact"]["path"]]
        for item in payload.get("false_closures") or []
    )
    assert "followup_obligation_review stale_artifact_validation" in (payload.get("degraded_modes") or [])


def test_orchestrator_followup_review_prompt_uses_refreshed_artifacts_index(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo spec",
            "required_sections": ["Контекст", "Требования"],
            "qa_prompt": "QA-REFRESHED-INDEX",
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="use_cli_repo_audit", title="review", instruction="repo review", step_type="use_cli")]

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
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_audit",
            status="ok",
            summary="repo audit summary",
            outputs=[{"type": "text", "content": "repo evidence preview"}],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    qc_calls = {"count": 0}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        del system, user
        if response_format is not None:
            qc_calls["count"] += 1
            if qc_calls["count"] == 1:
                return '{"needs_rework": true, "issues": ["expand"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "## Контекст\nПодтвержденный контекст.\n\n## Требования\n- Реализовать поведение."

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    captured = {"checked": False}

    async def _capture_repo_grounded_prompts(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "final_text": "## Требования\nОбновлённые требования после patch/merge.",
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "claims": [],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            expected_sha = next(
                line.split(":", 1)[1].strip()
                for line in prompt.splitlines()
                if line.strip().lower().startswith("- expected draft sha1:")
            )
            index_path = next(
                line.split(":", 1)[1].strip()
                for line in prompt.splitlines()
                if line.strip().lower().startswith("- artifacts index:")
            )
            index_payload = json.loads(Path(index_path).read_text(encoding="utf-8"))
            followup_entry = next(
                item
                for item in index_payload.get("artifacts") or []
                if str(item.get("kind") or "").strip() == "draft_followup_review"
            )
            assert followup_entry["path"].endswith("_repo_final_review_followup_draft.md")
            assert str((followup_entry.get("meta") or {}).get("sha1") or "").strip() == expected_sha
            captured["checked"] = True
            return json.dumps(
                {
                    "verdict": "Все blocking obligations закрыты.",
                    "closed_blocking_obligations": [],
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

    session = type(
        "S",
        (),
        {
            "id": "s-followup-index-refresh",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _capture_repo_grounded_prompts(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert "# Техническое задание" in out
    assert captured["checked"] is True


def test_orchestrator_rework_preserves_spec_shell_sections_when_model_drops_them(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo spec",
            "required_sections": ["Контекст", "Требования"],
            "qa_prompt": "QA-PRESERVE",
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_audit",
                title="review",
                instruction="repo review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_audit",
            status="ok",
            summary="repo audit summary",
            outputs=[{"type": "text", "content": "repo evidence preview"}],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    qc_calls = {"count": 0}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        del system, user
        if response_format is not None:
            qc_calls["count"] += 1
            if qc_calls["count"] == 1:
                return '{"needs_rework": true, "issues": ["preserve-shell"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return (
            "## Контекст\n"
            "Исходный контекст, который нельзя потерять.\n\n"
            "## Требования\n"
            "Исходные требования."
        )

    async def _capture_repo_grounded_prompts(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "final_text": "## Требования\nОбновлённые требования после patch/merge.",
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": ["Обновлён раздел Требования."],
                    "claims": [],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "verdict": "Все blocking obligations закрыты.",
                    "closed_blocking_obligations": [],
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

    session = type(
        "S",
        (),
        {
            "id": "s-preserve-shell",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _capture_repo_grounded_prompts(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert "# Техническое задание" in out
    assert "## Исходная задача" in out
    assert "## Контекст" in out
    assert "Исходный контекст, который нельзя потерять." in out
    assert "## Требования" in out
    assert "Обновлённые требования после patch/merge." in out
    assert "## Открытые вопросы и валидационные шаги" in out

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    persisted_draft = (artifacts_dir / "s-preserve-shell_draft.md").read_text(encoding="utf-8")
    polished_draft = (artifacts_dir / "s-preserve-shell_draft_polished.md").read_text(encoding="utf-8")
    for content in (persisted_draft, polished_draft):
        assert "# Техническое задание" in content
        assert "## Исходная задача" in content
        assert "## Открытые вопросы и валидационные шаги" in content
    assert "Исходный контекст, который нельзя потерять." in persisted_draft
    assert "Обновлённые требования после patch/merge." in polished_draft


def test_orchestrator_retries_compose_when_required_sections_are_out_of_order(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Spec template",
            "required_sections": ["Контекст", "Требования"],
            "output_kind": "spec",
            "compose_mode": "template_first",
            "repo_grounded_required": False,
            "protected_spec_shell": {
                "title": "Техническое задание",
                "source_task_section": "Исходная задача",
                "core_sections": ["Контекст", "Требования"],
                "open_questions_section": "Открытые вопросы и валидационные шаги",
            },
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="collect_context",
                title="collect",
                instruction="collect",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="collect_context",
            status="ok",
            summary="Подтвержден исходный контекст.",
            outputs=[{"type": "text", "content": "repo evidence preview"}],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    compose_calls = {"count": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        assert response_format is None
        compose_calls["count"] += 1
        if compose_calls["count"] == 1:
            return (
                "## Требования\n"
                "- Сначала требования.\n\n"
                "## Контекст\n"
                "Контекст пришёл вторым."
            )
        return (
            "## Контекст\n"
            "Контекст пришёл первым.\n\n"
            "## Требования\n"
            "- Требования идут вторыми."
        )

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-compose-order",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": False,
                "clarification_is_blocking": False,
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

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert compose_calls["count"] >= 1
    assert out.index("## Контекст") < out.index("## Требования")
    assert "Контекст пришёл вторым." in out
    assert "## Исходная задача" in out
    assert "## Открытые вопросы и валидационные шаги" in out


def test_orchestrator_preserves_required_section_order_when_repo_rework_bundle_reorders_sections(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo spec",
            "required_sections": ["Контекст", "Требования"],
            "qa_prompt": "QA-SECTION-CONTRACT",
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(
                id="use_cli_repo_audit",
                title="review",
                instruction="repo review",
                step_type="use_cli",
            )
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del step, session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id="use_cli_repo_audit",
            status="ok",
            summary="repo audit summary",
            outputs=[{"type": "text", "content": "repo evidence preview"}],
            tool_calls=[{"tool": "use_cli"}],
            next_questions=[],
        )

    qc_calls = {"count": 0}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        del system, user
        if response_format is not None:
            qc_calls["count"] += 1
            if qc_calls["count"] == 1:
                return '{"needs_rework": true, "issues": ["tighten"], "missing_sections": []}'
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return (
            "## Контекст\n"
            "Подтверждённый исходный контекст.\n\n"
            "## Требования\n"
            "- Исходные требования."
        )

    async def _capture_repo_grounded_prompts(prompt: str) -> str:
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "final_text": (
                        "## Требования\n"
                        "- Требования из patch/merge ушли вверх.\n\n"
                        "## Контекст\n"
                        "Контекст оказался после требований."
                    ),
                    "closed_obligations": [],
                    "remaining_obligations": [],
                    "corrections_applied": [],
                    "claims": [],
                    "evidence": [],
                    "degraded_modes": [],
                },
                ensure_ascii=False,
            )
        if f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}" in prompt:
            return json.dumps(
                {
                    "verdict": "Все blocking obligations закрыты.",
                    "closed_blocking_obligations": [],
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
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    session = type(
        "S",
        (),
        {
            "id": "s-rework-order",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_repo_audit": True,
                "clarification_is_blocking": False,
            },
            "run_prompt": lambda self, prompt: _capture_repo_grounded_prompts(prompt),
        },
    )()

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert out.index("## Контекст") < out.index("## Требования")
    assert "Контекст оказался после требований." in out
    assert "Требования из patch/merge ушли вверх." in out

    artifacts_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1" / "_orchestrator"
    assert (artifacts_dir / "s-rework-order_draft.md").exists()
    assert (artifacts_dir / "s-rework-order_draft_polished.md").exists()


def test_orchestrator_final_qc_required_input_gaps_do_not_pause_when_legacy_flag_is_false(
    tmp_path,
    monkeypatch,
) -> None:
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    seed_question_calls = {"count": 0}

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
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

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-required-input-assumptions",
            "active_mode": "analyst",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": False,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": False,
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

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec без уточнения сценариев",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert "Какие сценарии нужно сохранить без изменений?" not in out
    assert "## Контекст" in out
    assert getattr(session, "analyst_blocking_clarification_runtime", False) is False
    assert seed_question_calls["count"] == 0
    assert "Допущения и незакрытые входы" not in out


def test_orchestrator_pauses_on_blocking_clarification_instead_of_materializing_assumptions(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Spec with blocking clarification",
            "required_sections": ["Контекст", "Требования"],
            "required_inputs": ["Какие сценарии нельзя сломать"],
            "qa_prompt": "QA-BLOCKING-REQUIRED-INPUTS",
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
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    async def _unexpected_chat_completion(*_args, **_kwargs):
        raise AssertionError("compose/qc should not start while blocking clarification is unresolved")

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch._deps, "chat_completion", _unexpected_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type(
        "S",
        (),
        {
            "id": "s-required-input-blocking",
            "project_root": str(tmp_path),
            "workdir": str(tmp_path),
            "analyst_intent_flags": {
                "document_kind": "spec",
                "requires_codebase_grounding": False,
                "requires_repo_audit": False,
                "requires_final_repo_review": False,
                "clarification_is_blocking": True,
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

    out = asyncio.run(
        orch.run(
            session,
            "Подготовь implementation-ready spec без ответа на уточнение",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert out.startswith("Нужно уточнение пользователя, чтобы завершить работу.")
    assert "Какие сценарии нельзя сломать" in out
    assert "Допущения и незакрытые входы" not in out
    assert "Техническое задание" not in out


def test_orchestrator_claim_ledger_enriches_claims_with_step_level_repo_evidence(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )

    repo_file = tmp_path / "views" / "header.blade.php"
    repo_file.parent.mkdir(parents=True, exist_ok=True)
    repo_file.write_text("<div>header</div>", encoding="utf-8")

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [
            PlanStep(id="step1", title="Repo findings", instruction="collect", step_type="task"),
            PlanStep(id="use_cli_repo_final_review", title="Final review",
                     instruction=f"review in {tmp_path}", step_type="use_cli"),
        ]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="Подтверждено: header содержит account dropdown.",
            outputs=[{"type": "text", "content": "header evidence", "path": str(repo_file)}],
            claims=[
                {
                    "claim_id": "claim_step1_1",
                    "status": "confirmed",
                    "text": "В header есть account dropdown.",
                    "evidence": [{"type": "text", "path": "", "preview": "account dropdown exists"}],
                }
            ],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            type(
                "S",
                (),
                {
                    "id": "s1",
                    "workdir": str(tmp_path),
                    "project_root": str(tmp_path),
                    "analyst_intent_flags": {
                        "document_kind": "spec",
                        "requires_codebase_grounding": True,
                        "requires_repo_audit": False,
                        "requires_final_repo_review": False,
                        "clarification_is_blocking": False,
                    },
                },
            )(),
            "user request",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert out
    workspace_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1"
    ledger_payload = json.loads((workspace_dir / "_orchestrator" / "s1_claim_ledger.json").read_text(encoding="utf-8"))
    first_claim = ledger_payload[0]
    evidence_paths = [str(item.get("path") or "") for item in first_claim.get("evidence") or []]
    assert str(repo_file) in evidence_paths


def test_orchestrator_claim_ledger_downgrades_repo_grounded_claim_without_repo_anchor(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="step1", title="Repo findings", instruction="collect", step_type="task")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="Есть предварительное наблюдение без repo anchor.",
            outputs=[{"type": "text", "content": "analysis note without file path"}],
            claims=[
                {
                    "claim_id": "claim_step1_1",
                    "status": "confirmed",
                    "text": "Writer path для Codex уже реализован.",
                    "evidence": [{"type": "text", "path": "", "preview": "observed in analysis"}],
                }
            ],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            type(
                "S",
                (),
                {
                    "id": "s1-unanchored-claim",
                    "workdir": str(tmp_path),
                    "project_root": str(tmp_path),
                    "analyst_intent_flags": {
                        "document_kind": "spec",
                        "requires_codebase_grounding": True,
                        "requires_repo_audit": False,
                        "requires_final_repo_review": False,
                        "clarification_is_blocking": False,
                    },
                },
            )(),
            "user request",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert out
    workspace_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1"
    ledger_payload = json.loads(
        (workspace_dir / "_orchestrator" / "s1-unanchored-claim_claim_ledger.json").read_text(encoding="utf-8")
    )
    first_claim = next(item for item in ledger_payload if item.get("claim_id") == "claim_step1_1")
    assert first_claim["status"] == "needs_check"


def test_orchestrator_claim_ledger_downgrades_codebase_map_only_evidence(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo change spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
            "repo_grounded_required": True,
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=False,
        final_rework_passes=0,
        template_provider=_template_provider,
    )
    monkeypatch.setattr(orch, "_missing_required_repo_use_cli_step_ids", lambda *args, **kwargs: [])

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return [PlanStep(id="step1", title="Repo findings", instruction="collect", step_type="task")]

    async def _fake_execute_step(step, session, bot, context, dest, orchestrator_context, *, current_user_text="", constraints=None):
        del session, bot, context, dest, orchestrator_context, current_user_text, constraints
        return orch._deps.ExecutorResponse(
            task_id=step.id,
            status="ok",
            summary="Codebase Map содержит навигационную заметку.",
            outputs=[
                {
                    "type": "text",
                    "content": "Codebase Map navigation summary",
                    "path": ".cli-proxy/.codebase_map/INDEX.md",
                }
            ],
            claims=[
                {
                    "claim_id": "claim_step1_1",
                    "status": "confirmed",
                    "text": "Runtime registry уже подтвержден.",
                    "evidence": [],
                }
            ],
            tool_calls=[],
            next_questions=[],
        )

    async def _fake_chat_completion(_cfg, _system: str, _user: str, response_format=None):
        if response_format is not None:
            return '{"needs_rework": false, "issues": [], "missing_sections": []}'
        return "DRAFT"

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(
        orch.run(
            type(
                "S",
                (),
                {
                    "id": "s1-codebase-map-only",
                    "workdir": str(tmp_path),
                    "project_root": str(tmp_path),
                    "analyst_intent_flags": {
                        "document_kind": "spec",
                        "requires_codebase_grounding": True,
                        "requires_repo_audit": False,
                        "requires_final_repo_review": False,
                        "clarification_is_blocking": False,
                    },
                },
            )(),
            "user request",
            _FakeBot(),
            context=object(),
            dest={"chat_id": 1},
        )
    )

    assert out
    workspace_dir = Path(tmp_path) / "_sandbox" / "chats" / "chat_1"
    ledger_payload = json.loads(
        (workspace_dir / "_orchestrator" / "s1-codebase-map-only_claim_ledger.json").read_text(encoding="utf-8")
    )
    first_claim = next(item for item in ledger_payload if item.get("claim_id") == "claim_step1_1")
    assert first_claim["status"] == "needs_check"


def test_orchestrator_large_spec_missing_counts_triggers_rework_with_counts_prompt(tmp_path, monkeypatch) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Large Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-LARGE",
            "target_size_hint": "large",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    calls = {"compose": 0, "qc": 0, "rework": 0}
    captured = {"rework_system": None, "rework_user": None}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if calls["qc"] == 1:
                return (
                    '{"needs_rework": false, "issues": [], "missing_sections": [], '
                    '"weak_sections": [], "missing_counts": ["Недостаточно FR"], '
                    '"traceability_gaps": []}'
                )
            return (
                '{"needs_rework": false, "issues": [], "missing_sections": [], '
                '"weak_sections": [], "missing_counts": [], "traceability_gaps": []}'
            )
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        calls["rework"] += 1
        captured["rework_system"] = system
        captured["rework_user"] = user
        return '{"final_text":"REVISED"}'

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type("S", (), {"id": "s1", "analyst_template_id": "default"})

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "REVISED"
    assert calls["compose"] == 1
    assert calls["rework"] == 1
    assert calls["qc"] == 1
    assert captured["rework_system"] is not None
    assert "Доработай ТЗ" in captured["rework_system"]
    assert "устрани противоречия текущему коду" in captured["rework_system"]
    assert "low-middle разработчиком без устных пояснений" in captured["rework_system"]
    assert "Не придумывай новые сущности" in captured["rework_system"]
    assert "реально подтвержденных затронутых зон" in captured["rework_system"]
    assert "config/docs/tests" in captured["rework_system"]
    assert captured["rework_user"] is not None
    assert "Недобор по количественным требованиям" in captured["rework_user"]
    assert "- Недостаточно FR" in captured["rework_user"]


@pytest.mark.parametrize(
    ("gap_field", "gap_label"),
    [
        ("codebase_mismatches", "Несоответствия кодовой базе"),
        ("unsupported_assumptions", "Неподтвержденные предположения"),
        ("unverified_claims", "Неподтвержденные product/capability claims"),
        ("config_contract_gaps", "Пробелы в config-контракте"),
        ("migration_gaps", "Пробелы миграции"),
        ("doc_sync_gaps", "Пробелы синхронизации документации"),
        ("test_gaps", "Пробелы тестового покрытия"),
    ],
)
def test_orchestrator_qc_repo_gap_fields_trigger_rework(
    tmp_path,
    monkeypatch,
    gap_field: str,
    gap_label: str,
) -> None:
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

    def _template_provider(_session):
        return {
            "name": "Repo Grounded Spec",
            "required_sections": ["S1"],
            "qa_prompt": "QA-REPO",
        }

    orch = OrchestratorRunner(
        cfg,
        final_rework_enabled=True,
        final_rework_passes=1,
        template_provider=_template_provider,
    )

    async def _fake_plan_steps(_cfg, _user_text, _ctx_summary):
        return []

    monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
    calls = {"compose": 0, "qc": 0, "rework": 0}
    captured = {"qc_system": None, "rework_user": None}

    async def _fake_chat_completion(_cfg, system: str, user: str, response_format=None):
        if response_format is not None:
            calls["qc"] += 1
            if captured["qc_system"] is None:
                captured["qc_system"] = system
            if calls["qc"] == 1:
                return (
                    '{"needs_rework": false, "issues": [], "missing_sections": [], '
                    '"weak_sections": [], "missing_counts": [], "traceability_gaps": [], '
                    f'"{gap_field}": ["GAP"]' "}"
                )
            return (
                '{"needs_rework": false, "issues": [], "missing_sections": [], '
                '"weak_sections": [], "missing_counts": [], "traceability_gaps": [], '
                '"codebase_mismatches": [], "unsupported_assumptions": [], '
                '"unverified_claims": [], "config_contract_gaps": [], '
                '"migration_gaps": [], "doc_sync_gaps": [], "test_gaps": []}'
            )
        if calls["compose"] == 0:
            calls["compose"] += 1
            return "DRAFT"
        calls["rework"] += 1
        captured["rework_user"] = user
        return '{"final_text":"REVISED"}'

    monkeypatch.setattr(orch._deps, "chat_completion", _fake_chat_completion)

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_maybe_update_memory", _no_memory)

    session = type("S", (), {"id": "s1", "analyst_template_id": "default"})

    class _FakeBot:
        async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
            return True

        async def send_output(self, *_args, **_kwargs):
            return None

        async def _send_document(self, *_args, **_kwargs):
            return None

    out = asyncio.run(orch.run(session, "user request", _FakeBot(), context=object(), dest={"chat_id": 1}))

    assert out == "REVISED"
    assert calls["compose"] == 1
    assert calls["rework"] == 1
    assert calls["qc"] == 1
    assert captured["qc_system"] is not None
    assert f'"{gap_field}"' in captured["qc_system"]
    assert captured["rework_user"] is not None
    assert gap_label in captured["rework_user"]
    assert "- GAP" in captured["rework_user"]
