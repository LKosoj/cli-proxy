import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.analyst.template_service import get_effective_template, get_template_for_session, resolve_effective_template_id
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep
from modes.sdk.orchestrator_runner import OrchestratorRunner


def test_get_template_for_session_resolves_from_session(tmp_path):
    (tmp_path / "analyst_config.yaml").write_text(
        """\
templates:
  default:
    name: "T-Default"
    description: "D"
    required_sections: ["S0"]
    system_prompt_addition: "SYS0"
    qa_prompt: "QA0"
  audit:
    name: "T-Audit"
    description: "D"
    required_sections: ["S1"]
    system_prompt_addition: "SYS1"
    qa_prompt: "QA1"
""",
        encoding="utf-8",
    )

    session = type("S", (), {"id": "s1", "analyst_template_id": "audit"})
    tmpl = get_template_for_session(session, templates_path=str(tmp_path / "analyst_config.yaml"))
    assert tmpl["name"] == "T-Audit"
    assert tmpl["required_sections"] == ["S1"]


def test_resolve_effective_template_id_uses_priority_order() -> None:
    registry = {
        "default": {"name": "Default"},
        "change_spec": {"name": "Change"},
        "audit": {"name": "Audit"},
    }
    session = types.SimpleNamespace(analyst_template_id="change_spec")

    assert (
        resolve_effective_template_id(
            registry,
            runtime_template_id="audit",
            intent_template_id="default",
            session=session,
        )
        == "audit"
    )
    assert (
        resolve_effective_template_id(
            registry,
            runtime_template_id="missing",
            intent_template_id="default",
            session=session,
        )
        == "default"
    )
    assert (
        resolve_effective_template_id(
            registry,
            runtime_template_id="",
            intent_template_id="missing",
            session=session,
        )
        == "change_spec"
    )
    assert (
        resolve_effective_template_id(
            registry,
            runtime_template_id="",
            intent_template_id="",
            session=types.SimpleNamespace(analyst_template_id="missing"),
        )
        == "default"
    )


def test_get_effective_template_returns_selected_template_with_optional_fields() -> None:
    registry = {
        "default": {"name": "Default"},
        "change_spec": {
            "name": "Change",
            "compose_mode": "template_first",
            "output_kind": "spec",
            "min_functional_requirements": 12,
        },
    }

    template = get_effective_template(
        registry,
        runtime_template_id="",
        intent_template_id="change_spec",
        session_template_id="default",
    )

    assert template["_id"] == "change_spec"
    assert template["compose_mode"] == "template_first"
    assert template["output_kind"] == "spec"
    assert template["min_functional_requirements"] == 12


def test_orchestrator_execute_step_accepts_constraints_from_template(tmp_path, monkeypatch):
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

        (tmp_path / "analyst_config.yaml").write_text(
            """\
templates:
  default:
    name: "T-Default"
    description: "D"
    required_sections: ["S0"]
    system_prompt_addition: "SYS0"
    qa_prompt: "QA0"
""",
            encoding="utf-8",
        )

        orch = OrchestratorRunner(cfg, final_rework_enabled=True, final_rework_passes=0)
        session = type("S", (), {"id": "s1", "analyst_template_id": "default"})
        tmpl = get_template_for_session(session, templates_path=str(tmp_path / "analyst_config.yaml"))

        class _P:
            name = "default"
            allowed_tools = []

        monkeypatch.setattr(orch._dispatcher, "get_profile", lambda _step, _session=None: _P)

        async def _fake_executor_run(_session, req, _bot, _context, _dest, _profile):
            assert req.constraints == "SYS0"
            return ExecutorResponse(task_id=req.task_id, status="ok", summary="ok")

        monkeypatch.setattr(orch._executor, "run", _fake_executor_run)

        step = PlanStep(id="s", title="t", instruction="do")
        resp = await orch._execute_step(
            step,
            session,
            bot=None,
            context=None,
            dest={"chat_id": 1},
            orchestrator_context="",
            constraints=tmpl["system_prompt_addition"],
        )
        assert resp.status == "ok"

    asyncio.run(_run())
