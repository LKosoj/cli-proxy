import json
import types

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.analyst import template_service as analyst_templates
from modes.analyst.mode import AnalystMode
from modes.analyst.routing_rules import build_template_from_profile
from modes.analyst.runner_service import AnalystModeRunnerService
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime import planner as planner_mod


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


class _RuntimeWithPreview:
    def __init__(self, full_text: str, preview_text: str):
        self.prompt = ""
        self.full_text = full_text
        self.preview_text = preview_text
        self.preview_calls = 0

    async def run(self, _session, analyst_prompt, bot_app, _context, _dest):
        self.prompt = analyst_prompt
        if hasattr(bot_app, "send_output"):
            self.preview_calls += 1
            await bot_app.send_output(None, None, self.preview_text, None)
        return self.full_text

    def get_template_for_session(self, _session):
        return {
            "_id": "default",
            "required_sections": ["Контекст и обзор"],
            "system_prompt_addition": "",
            "qa_prompt": "qa",
        }


class _Tooling:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "bug_investigation",
                    "template_id": "bug_investigation",
                    "confidence": 0.9,
                    "reason": "Запрос выглядит как расследование бага",
                    "detail_level": "full",
                    "needs_cli": True,
                    "needs_clarification": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingAskFailure:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "spec",
                    "template_id": "default",
                    "confidence": 0.7,
                    "needs_cli": False,
                    "needs_clarification": True,
                    "clarification_question": "Уточните платформу",
                    "clarification_options": ["web", "mobile"],
                },
                ensure_ascii=False,
            ),
        }


class _ToolingUIChange:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "ui_change_spec",
                    "template_id": "ui_change_spec",
                    "confidence": 0.93,
                    "reason": "Локальная UI/UX-доработка существующего компонента",
                    "detail_level": "standard",
                    "document_kind": "spec",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": True,
                    "requires_final_repo_review": True,
                    "clarification_is_blocking": False,
                    "clarification_question": "",
                    "clarification_options": [],
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingBroadChangeForUI:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "change_spec",
                    "template_id": "change_spec",
                    "confidence": 0.9,
                    "reason": "Нужно подготовить ТЗ на доработку",
                    "detail_level": "standard",
                    "document_kind": "spec",
                    "change_scope": "local_ui",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": True,
                    "requires_final_repo_review": True,
                    "clarification_is_blocking": False,
                    "clarification_question": "",
                    "clarification_options": [],
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingSpecDefault:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "spec",
                    "template_id": "default",
                    "confidence": 0.85,
                    "reason": "Нужно сделать полноценное ТЗ",
                    "detail_level": "standard",
                    "document_kind": "spec",
                    "change_scope": "broad_change",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": False,
                    "requires_final_repo_review": True,
                    "clarification_is_blocking": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingSpecDefaultGreenfield:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "spec",
                    "template_id": "default",
                    "confidence": 0.86,
                    "reason": "Нужно подготовить полноценное ТЗ",
                    "detail_level": "full",
                    "document_kind": "spec",
                    "change_scope": "none",
                    "needs_cli": False,
                    "needs_clarification": False,
                    "requires_codebase_grounding": False,
                    "requires_repo_audit": False,
                    "requires_final_repo_review": False,
                    "clarification_is_blocking": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingAuditDefault:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "audit",
                    "template_id": "default",
                    "confidence": 0.9,
                    "reason": "Нужен аудит текущего проекта",
                    "detail_level": "full",
                    "document_kind": "audit",
                    "change_scope": "none",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": False,
                    "requires_final_repo_review": False,
                    "clarification_is_blocking": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingBugfixSpec:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "bug_investigation",
                    "template_id": "bug_investigation",
                    "confidence": 0.92,
                    "reason": "Нужно оформить план исправления бага",
                    "detail_level": "standard",
                    "document_kind": "spec",
                    "change_scope": "broad_change",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": False,
                    "requires_final_repo_review": True,
                    "clarification_is_blocking": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingIntegrationSpec:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "integration_contract",
                    "template_id": "integration_contract",
                    "confidence": 0.91,
                    "reason": "Меняется интеграционный контракт",
                    "detail_level": "standard",
                    "document_kind": "spec",
                    "change_scope": "broad_change",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": False,
                    "requires_final_repo_review": True,
                    "clarification_is_blocking": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


class _ToolingBackendSpec:
    async def execute(self, name, _args, _ctx):
        assert name == "analyst_intent_plugin"
        return {
            "success": True,
            "output": json.dumps(
                {
                    "task_type": "refactor_spec",
                    "template_id": "refactor_spec",
                    "confidence": 0.9,
                    "reason": "Нужна узкая backend-доработка",
                    "detail_level": "standard",
                    "document_kind": "spec",
                    "change_scope": "broad_change",
                    "needs_cli": True,
                    "needs_clarification": False,
                    "requires_codebase_grounding": True,
                    "requires_repo_audit": False,
                    "requires_final_repo_review": True,
                    "clarification_is_blocking": False,
                },
                ensure_ascii=False,
            ),
        }

    async def ask_user(self, **_kwargs):
        return "unused"


def test_assess_template_fitness_flags_local_ui_oversize() -> None:
    result = analyst_templates.assess_template_fitness(
        selected_template_id="default",
        intent_template_id="project_analysis",
        effective_template_id="project_analysis",
        document_kind="spec",
        change_scope="local_ui",
    )

    assert result["applicable"] is True
    assert result["status"] == "needs_adjustment"
    assert result["expected_template_id"] == "ui_change_spec"


def test_assess_template_fitness_skips_runtime_override() -> None:
    result = analyst_templates.assess_template_fitness(
        selected_template_id="default",
        intent_template_id="default",
        effective_template_id="audit",
        document_kind="audit",
        change_scope="none",
        runtime_template_id="audit",
    )

    assert result["applicable"] is False
    assert result["status"] == "forced_runtime_override"


def test_assess_template_fitness_prefers_bugfix_spec_for_bug_fix_spec_scope() -> None:
    result = analyst_templates.assess_template_fitness(
        selected_template_id="default",
        intent_template_id="bug_investigation",
        effective_template_id="change_spec",
        document_kind="spec",
        change_scope="broad_change",
    )

    assert result["applicable"] is True
    assert result["status"] == "needs_adjustment"
    assert result["expected_template_id"] == "bugfix_spec"


def test_assess_template_fitness_prefers_integration_change_spec() -> None:
    result = analyst_templates.assess_template_fitness(
        selected_template_id="default",
        intent_template_id="integration_contract",
        effective_template_id="change_spec",
        document_kind="spec",
        change_scope="broad_change",
    )

    assert result["applicable"] is True
    assert result["status"] == "needs_adjustment"
    assert result["expected_template_id"] == "integration_change_spec"


def test_build_template_from_profile_does_not_treat_session_transfer_format_as_ui_change() -> None:
    user_text = (
        "Сейчас в проекте реализован перенос сессий между Gemini/cloud code/qwen coder, "
        "но, возможно, не реализован перенос сессий для codex. "
        "Необходимо реализовать чтение сессии из codex в единый формат и запись сессии в codex."
    )

    template_id = build_template_from_profile("codebase", "spec", user_text)

    assert template_id in {"change_spec", "integration_change_spec"}


def test_assess_template_fitness_prefers_narrow_backend_change_spec() -> None:
    result = analyst_templates.assess_template_fitness(
        selected_template_id="default",
        intent_template_id="refactor_spec",
        effective_template_id="change_spec",
        document_kind="spec",
        change_scope="broad_change",
    )

    assert result["applicable"] is True
    assert result["status"] == "needs_adjustment"
    assert result["expected_template_id"] == "narrow_backend_change_spec"


def test_assess_template_fitness_prefers_new_spec_for_greenfield_generic_spec() -> None:
    result = analyst_templates.assess_template_fitness(
        selected_template_id="default",
        intent_template_id="default",
        effective_template_id="default",
        document_kind="spec",
        change_scope="none",
        source_user_text="Подготовь полное ТЗ для нового сервиса обработки заявок с нуля",
    )

    assert result["applicable"] is True
    assert result["status"] == "needs_adjustment"
    assert result["expected_template_id"] == "new_spec"


def test_analyst_runner_template_provider_uses_same_store_as_mode_across_two_intents(monkeypatch, tmp_path) -> None:
    templates = tmp_path / "analyst_config.yaml"
    templates.write_text(
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["Быстрый анализ"]
    system_prompt_addition: ""
    qa_prompt: "Q0"
  bug_investigation:
    name: "Bug"
    description: "D"
    required_sections: ["Root cause"]
    system_prompt_addition: ""
    qa_prompt: "Q1"
  audit:
    name: "Audit"
    description: "D"
    required_sections: ["Общая оценка"]
    system_prompt_addition: ""
    qa_prompt: "Q2"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(templates))

    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        analyst_template_id="default",
        analyst_runtime_template_id="",
    )
    mode = AnalystMode()
    store = mode._store(session)
    key = mode._context_key(session)

    # Intent #1: mode persists bug_investigation as effective template.
    ctx = store.load(key)
    ctx.runtime_template_id = ""
    ctx.effective_template_id = "bug_investigation"
    store.save(ctx)

    # defaults.state_path intentionally points elsewhere to prove runner does not use it.
    runner = AnalystModeRunnerService.__new__(AnalystModeRunnerService)
    runner._config = types.SimpleNamespace(defaults=types.SimpleNamespace(state_path=str(tmp_path / "other_state_root")))
    t1 = runner._get_effective_template_for_session(session)
    assert t1.get("_id") == "bug_investigation"

    # Intent #2: runtime override switches to audit on the same persisted context.
    ctx2 = store.load(key)
    ctx2.runtime_template_id = "audit"
    ctx2.effective_template_id = "default"
    store.save(ctx2)

    t2 = runner._get_effective_template_for_session(session)
    assert t2.get("_id") == "audit"


@pytest.mark.asyncio
async def test_planner_enforces_repo_audit_and_final_review_flags_without_replan_duplicates(
    tmp_path,
    monkeypatch,
):
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
                        "id": "step1",
                        "title": "Собрать факты",
                        "instruction": "Проанализировать задачу и собрать входные данные",
                        "step_type": "task",
                        "parallel_group": None,
                        "depends_on": [],
                        "parallelizable": False,
                        "parallelizable_reason": None,
                        "ask_question": None,
                        "ask_options": None,
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(planner_mod, "needs_clarification", lambda *_args, **_kwargs: False)

    flags = json.dumps(
        {
            "clarification_is_blocking": False,
            "document_kind": "spec",
            "requires_codebase_grounding": True,
            "requires_final_repo_review": True,
            "requires_repo_audit": True,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    context = (
        f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
        f"analyst_intent_flags:\n{flags}"
    )

    steps = await planner_mod.plan_steps(
        cfg,
        "Подготовь repo-grounded план изменений по проекту",
        context,
    )
    step_ids = [step.id for step in steps]
    audit_step = next(step for step in steps if step.id == "use_cli_repo_audit")
    final_review_step = next(step for step in steps if step.id == "use_cli_repo_final_review")

    assert step_ids.count("use_cli_repo_audit") == 1
    assert step_ids.count("use_cli_repo_final_review") == 1
    assert audit_step.step_type == "use_cli"
    assert final_review_step.step_type == "use_cli"
    assert "use_cli_repo_audit" in final_review_step.depends_on
    # final_review depends only on other repo steps, not all plan steps
    assert "step1" not in final_review_step.depends_on

    prior_steps = json.dumps(
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
    replan_context = f"{context}\nprior_steps:\n{prior_steps}"
    replanned_steps = await planner_mod.plan_steps(
        cfg,
        "Перепланируй то же самое без дублирования уже выполненных use_cli шагов",
        replan_context,
    )
    replanned_ids = [step.id for step in replanned_steps]

    assert "use_cli_repo_audit" not in replanned_ids
    assert "use_cli_repo_final_review" not in replanned_ids


@pytest.mark.asyncio
async def test_planner_repo_grounded_spec_does_not_inject_final_review_without_explicit_flags(
    tmp_path,
    monkeypatch,
):
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
                        "id": "step1",
                        "title": "Собрать контекст",
                        "instruction": "Собрать контекст без обязательного repo audit",
                        "step_type": "task",
                        "parallel_group": None,
                        "depends_on": [],
                        "parallelizable": False,
                        "parallelizable_reason": None,
                        "ask_question": None,
                        "ask_options": None,
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(planner_mod, "needs_clarification", lambda *_args, **_kwargs: False)

    flags = json.dumps(
        {
            "clarification_is_blocking": False,
            "document_kind": "spec",
            "requires_codebase_grounding": True,
            "requires_final_repo_review": False,
            "requires_repo_audit": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    context = (
        f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
        f"analyst_intent_flags:\n{flags}"
    )

    steps = await planner_mod.plan_steps(
        cfg,
        "Подготовь текст ТЗ без обязательного CLI-аудита репозитория",
        context,
    )
    step_ids = [step.id for step in steps]

    assert "use_cli_repo_audit" not in step_ids
    assert "use_cli_repo_final_review" not in step_ids


def test_orchestrator_effective_repo_flags_do_not_force_final_review_for_repo_grounded_spec(tmp_path):
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
    session = types.SimpleNamespace(
        analyst_intent_flags={
            "clarification_is_blocking": False,
            "clarification_topic": "",
            "document_kind": "spec",
            "needs_clarification": False,
            "requires_codebase_grounding": True,
            "requires_final_repo_review": True,
            "requires_repo_audit": False,
        }
    )

    no_template_flags = orch._effective_analyst_repo_flags(session)
    template_flags = orch._effective_analyst_repo_flags(
        session,
        {
            "output_kind": "spec",
            "repo_grounded_required": True,
            "repo_audit_required": True,
            "final_repo_review_required": True,
        },
    )

    assert no_template_flags["requires_final_repo_review"] is True
    assert template_flags["requires_final_repo_review"] is True


@pytest.mark.asyncio
async def test_planner_repo_grounded_analysis_injects_base_use_cli_step(
    tmp_path,
    monkeypatch,
):
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
                        "id": "step1",
                        "title": "Собрать контекст",
                        "instruction": "Собрать repo-grounded контекст",
                        "step_type": "task",
                        "parallel_group": None,
                        "depends_on": [],
                        "parallelizable": False,
                        "parallelizable_reason": None,
                        "ask_question": None,
                        "ask_options": None,
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(planner_mod, "needs_clarification", lambda *_args, **_kwargs: False)

    flags = json.dumps(
        {
            "clarification_is_blocking": False,
            "document_kind": "analysis",
            "requires_codebase_grounding": True,
            "requires_final_repo_review": False,
            "requires_repo_audit": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    context = (
        f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
        f"analyst_intent_flags:\n{flags}"
    )

    steps = await planner_mod.plan_steps(
        cfg,
        "Подготовь repo-grounded анализ проекта",
        context,
    )
    step_ids = [step.id for step in steps]
    grounding_step = next(step for step in steps if step.id == "use_cli_repo_grounding")

    assert "use_cli_repo_grounding" in step_ids
    assert "use_cli_repo_audit" not in step_ids
    assert "use_cli_repo_final_review" not in step_ids
    assert grounding_step.step_type == "use_cli"
    assert grounding_step.depends_on == []
    assert str(tmp_path) in grounding_step.instruction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requires_repo_audit", "requires_final_repo_review", "expected_present", "expected_absent"),
    [
        (
            True,
            False,
            {"use_cli_repo_audit"},
            {"use_cli_repo_final_review"},
        ),
        (
            False,
            True,
            {"use_cli_repo_final_review"},
            {"use_cli_repo_audit"},
        ),
    ],
)
async def test_planner_injects_only_requested_repo_use_cli_steps_by_flags(
    tmp_path,
    monkeypatch,
    requires_repo_audit: bool,
    requires_final_repo_review: bool,
    expected_present: str,
    expected_absent: str,
):
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
                        "id": "step1",
                        "title": "Собрать контекст",
                        "instruction": "Собрать repo-grounded контекст",
                        "step_type": "task",
                        "parallel_group": None,
                        "depends_on": [],
                        "parallelizable": False,
                        "parallelizable_reason": None,
                        "ask_question": None,
                        "ask_options": None,
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(planner_mod, "needs_clarification", lambda *_args, **_kwargs: False)

    flags = json.dumps(
        {
            "clarification_is_blocking": False,
            "document_kind": "spec",
            "requires_codebase_grounding": True,
            "requires_final_repo_review": requires_final_repo_review,
            "requires_repo_audit": requires_repo_audit,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    context = (
        f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
        f"analyst_intent_flags:\n{flags}"
    )

    steps = await planner_mod.plan_steps(
        cfg,
        "Подготовь repo-grounded план работ",
        context,
    )
    step_ids = [step.id for step in steps]
    for step_id in expected_present:
        assert step_id in step_ids
    for step_id in expected_absent:
        assert step_id not in step_ids

    if "use_cli_repo_audit" in expected_present:
        audit_step = next(step for step in steps if step.id == "use_cli_repo_audit")
        assert audit_step.step_type == "use_cli"
        assert audit_step.depends_on == []
    if "use_cli_repo_final_review" in expected_present:
        final_step = next(step for step in steps if step.id == "use_cli_repo_final_review")
        assert final_step.step_type == "use_cli"
        # final_review depends only on other repo steps, not all plan steps
        assert "step1" not in final_step.depends_on
        if "use_cli_repo_audit" in expected_present:
            assert "use_cli_repo_audit" in final_step.depends_on


@pytest.mark.asyncio
async def test_planner_non_repo_grounded_context_keeps_repo_use_cli_steps_disabled(
    tmp_path,
    monkeypatch,
):
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
                        "id": "step1",
                        "title": "Собрать контекст",
                        "instruction": "Подготовить non-repo-grounded аналитический ответ",
                        "step_type": "task",
                        "parallel_group": None,
                        "depends_on": [],
                        "parallelizable": False,
                        "parallelizable_reason": None,
                        "ask_question": None,
                        "ask_options": None,
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(planner_mod, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(planner_mod, "needs_clarification", lambda *_args, **_kwargs: False)

    flags = json.dumps(
        {
            "clarification_is_blocking": False,
            "document_kind": "analysis",
            "requires_codebase_grounding": False,
            "requires_final_repo_review": False,
            "requires_repo_audit": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    context = (
        f"executor_profile=analyst\nproject_root={tmp_path}\nworkdir={tmp_path}\n"
        f"analyst_intent_flags:\n{flags}"
    )

    steps = await planner_mod.plan_steps(
        cfg,
        "Подготовь аналитический ответ без обязательных repo-проверок",
        context,
    )
    step_ids = [step.id for step in steps]

    assert "use_cli_repo_audit" not in step_ids
    assert "use_cli_repo_final_review" not in step_ids


def test_non_repo_grounded_flags_do_not_enable_blocking_clarification_runtime(tmp_path) -> None:
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
    service = AnalystModeRunnerService(cfg)
    session = types.SimpleNamespace(
        id="s1",
        analyst_intent_flags={
            "clarification_is_blocking": False,
            "document_kind": "analysis",
            "requires_codebase_grounding": False,
            "requires_final_repo_review": False,
            "requires_repo_audit": False,
        },
    )

    assert orch._requires_blocking_clarification(session) is False
    assert service._requires_blocking_clarification(session) is False
    assert service._runtime.runner._final_rework_enabled is True


def test_filter_gate1_questions_truncates_to_three_and_drops_empty() -> None:
    mode = AnalystMode()

    filtered = mode._filter_gate1_questions(
        analysis_profile="codebase_plus_research",
        template={"repo_grounded_required": True},
        clarification_questions=[
            "Первый вопрос",
            "",
            "   ",
            "Второй вопрос",
            "Третий вопрос",
            "Четвёртый лишний",
        ],
        user_text="любой текст",
    )

    assert filtered == ["Первый вопрос", "Второй вопрос", "Третий вопрос"]


def test_mark_clarification_resolved_clears_blocking_state_and_persists_answers(tmp_path) -> None:
    mode = AnalystMode()

    session = types.SimpleNamespace(
        id="s1",
        chat_id=1,
        workdir=str(tmp_path),
        project_root=str(tmp_path),
        analyst_runtime_template_id="",
    )

    mode._persist_intent_context(
        session=session,
        intent_data={
            "analysis_profile": "codebase_plus_research",
            "document_kind": "spec",
            "detail_level": "full",
            "summary": "summary",
            "clarification_questions": ["Нужно ли сохранить обратную совместимость?"],
        },
        template_id="change_spec",
        template={
            "repo_grounded_required": True,
            "repo_audit_required": True,
            "final_repo_review_required": True,
            "required_inputs": ["Платформа"],
        },
        user_text="Подготовь ТЗ",
    )

    before = mode._load_context(session=session)
    assert before.needs_clarification is True
    assert before.clarification_is_blocking is True
    assert getattr(session, "analyst_intent_flags")["needs_clarification"] is True

    mode._mark_clarification_resolved(session=session, answers=["Да, без разрыва активной сессии"])

    after = mode._load_context(session=session)
    assert after.needs_clarification is False
    assert after.clarification_is_blocking is False
    assert after.clarification_answers == ["Да, без разрыва активной сессии"]
    assert getattr(session, "analyst_intent_flags")["needs_clarification"] is False
    assert getattr(session, "analyst_intent_flags")["clarification_is_blocking"] is False
    assert getattr(session, "analyst_intent_flags")["clarification_answers"] == [
        "Да, без разрыва активной сессии"
    ]
