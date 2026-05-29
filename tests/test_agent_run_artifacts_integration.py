import json
import time
from pathlib import Path

import pytest

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.agent.runner_service import AgentModeRunnerService


class _RunnerBackend:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        self.clear_calls: list[str] = []
        self.runner = object()

    async def run(self, session, prompt: str, _bot_app, _context, dest):
        index = min(len(self.calls), len(self.outputs) - 1)
        output = str(self.outputs[index] if self.outputs else "")
        self.calls.append(
            {
                "prompt": str(prompt or ""),
                "dest": dict(dest or {}),
                "session_id": str(getattr(session, "id", "") or ""),
            }
        )
        return output

    def clear_session_cache(self, session_id: str) -> None:
        self.clear_calls.append(str(session_id or ""))

    def resolve_question(self, _question_id: str, _answer: str) -> bool:
        return True

    def record_message(self, _chat_id: int, _message_id: int) -> None:
        return None

    def get_plugin_ui(self, _profile):
        return {}


def _build_app(tmp_path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            openai_api_key="k",
            openai_model="m",
            openai_big_model="m-big",
            run_artifacts_enabled=True,
            run_doctor_enabled=True,
            run_boundary_validation_enabled=True,
            run_metrics_enabled=True,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def _install_runtime(app: BotApp, outputs: list[str]) -> _RunnerBackend:
    service = AgentModeRunnerService(app.config)
    backend = _RunnerBackend(outputs)
    service._runtime = backend
    app.mode_runtime_registry["agent"] = service
    return backend


def _prepare_session(app: BotApp, tmp_path):
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = "agent"
    session.project_root = str(tmp_path / "project-root")
    Path(session.project_root).mkdir(parents=True, exist_ok=True)
    session.cli.cli_work_type = "coding"
    session.executor_profile = "agent-profile"
    return session


def _run_store(app: BotApp) -> RunArtifactStore:
    return RunArtifactStore(app.config)


def _patch_run_ids(monkeypatch: pytest.MonkeyPatch, run_ids: list[str]) -> None:
    iterator = iter(run_ids)
    last = run_ids[-1]

    def _next_run_id() -> str:
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            return last
        return last

    monkeypatch.setattr("app.services.run_artifact_store._sortable_run_id", _next_run_id)


@pytest.mark.asyncio
async def test_agent_run_maintains_orchestrator_checkpoints(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T220100Z_agent_success001"])

    app = _build_app(tmp_path)
    _install_runtime(app, ["Agent final answer"])
    session = _prepare_session(app, tmp_path)

    await app.run_mode_pipeline(
        session,
        "Разберись с проектом и предложи следующее действие",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="agent",
    )

    artifact_store = _run_store(app)
    run = artifact_store.latest_run(session=session, mode_id="agent")
    assert run is not None

    assert Path(run.state_path).exists()
    assert Path(run.plan_path).exists()
    assert Path(run.checkpoints_path).exists()
    assert Path(run.recovery_path).exists()
    assert Path(run.metrics_path).exists()
    assert Path(run.events_path).exists()

    state = artifact_store.load_state(run)
    assert state["status"] == "completed"
    assert state["phase"] == "complete"
    mode_context = state["mode_context"]
    assert mode_context["cli_work_type"] == "coding"
    assert mode_context["executor_profile"] == "agent-profile"
    assert mode_context["blocking_clarification_open"] is False
    assert mode_context["blocking_clarifications"]["count"] == 0
    assert mode_context["final_deliverable"] == "Agent final answer"
    execution_context = mode_context["execution_context"]
    assert execution_context["dest_kind"] == "telegram"
    assert execution_context["chat_id"] == 1
    assert execution_context["runner_dest_kind"] == "telegram"
    assert execution_context["runner_chat_id"] == 1
    assert execution_context["runner_prompt_preview"]

    plan_payload = json.loads(Path(run.plan_path).read_text(encoding="utf-8"))
    assert plan_payload["units"]
    assert plan_payload["units"][0]["id"] == "agent:orchestrator"

    checkpoints_payload = json.loads(Path(run.checkpoints_path).read_text(encoding="utf-8"))
    assert [item.get("status") for item in checkpoints_payload["items"]] == ["started", "ok"]

    event_types = [item.get("event_type") for item in artifact_store.load_events_tail(run, limit=20)]
    assert "phase_start" in event_types
    assert "phase_end" in event_types

    validator = RunBoundaryValidationService(enabled=True)
    report = validator.validate(run, mode_id="agent", phase="complete")
    assert report.status == "ok"


@pytest.mark.asyncio
async def test_agent_boundary_blocks_complete_with_blocking_clarification(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T220200Z_agent_blocked001"])

    app = _build_app(tmp_path)
    _install_runtime(app, ["Agent final answer"])
    session = _prepare_session(app, tmp_path)
    sent: list[str] = []

    async def _send_message(_ctx, *, text: str, **_kwargs):
        sent.append(str(text or ""))
        return True

    monkeypatch.setattr(app, "_send_message", _send_message)
    app.ui_state.pending_questions["q-blocking-1"] = {
        "session_id": session.id,
        "created_at": time.time(),
        "awaiting_custom": False,
    }

    await app.run_mode_pipeline(
        session,
        "Заверши агентную задачу при незакрытом уточнении",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="agent",
    )

    artifact_store = _run_store(app)
    run = artifact_store.latest_run(session=session, mode_id="agent")
    assert run is not None

    state = artifact_store.load_state(run)
    assert state["status"] == "failed"
    assert state["phase"] == "complete"
    assert state["mode_context"]["blocking_clarification_open"] is True
    assert state["mode_context"]["blocking_clarifications"]["count"] == 1
    assert state["mode_context"]["blocking_clarifications"]["active_question_id"] == "q-blocking-1"

    validator = RunBoundaryValidationService(enabled=True)
    report = validator.validate(run, mode_id="agent", phase="complete")
    assert report.status == "error"
    issue_codes = {issue.code for issue in report.issues}
    assert "agent_blocking_clarification_open" in issue_codes
    assert any("Ошибка режима agent" in text for text in sent)


@pytest.mark.asyncio
async def test_agent_recover_run_executes_restart_from_phase_via_mode_hook(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T220250Z_agent_recover003"])

    app = _build_app(tmp_path)
    _install_runtime(app, ["Agent recovered answer"])
    session = _prepare_session(app, tmp_path)
    artifact_store = _run_store(app)

    previous = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T220200Z_agent_broken002",
        phase="execute",
        source_prompt_hash="sha256:agent-broken",
    )
    artifact_store.save_plan(
        previous,
        {
            "kind": "agent_orchestrator",
            "units": [{"id": "use_cli_repo_final_review", "step_type": "use_cli"}],
        },
    )
    artifact_store.save_state(
        previous,
        {
            "phase": "execute",
            "status": "completed",
            "mode_context": {
                "source_prompt": "Исходный агентный запрос",
                "required_use_cli_steps": ["use_cli_repo_final_review"],
                "blocking_clarification_open": False,
                "blocking_clarifications": {"count": 0},
                "execution_context": {
                    "dest_kind": "telegram",
                    "chat_id": 1,
                    "runner_prompt_preview": "Runner agent prompt",
                    "user_text_preview": "Fallback agent prompt",
                },
            },
        },
    )
    monkeypatch.setattr(session, "is_active_by_tick", lambda: False)

    result = await app.mode_run_operations.recover_run(
        session=session,
        mode_id="agent",
        run_id=previous.run_id,
    )

    previous_recovery = json.loads(Path(previous.recovery_path).read_text(encoding="utf-8"))
    latest = artifact_store.latest_run(session=session, mode_id="agent")
    assert latest is not None
    latest_state = artifact_store.load_state(latest)

    assert result.status == "ok"
    assert result.recommended_action == "restart_from_phase"
    assert latest.run_id == "run_20260312T220250Z_agent_recover003"
    assert latest_state["status"] == "completed"
    assert latest_state["mode_context"]["final_deliverable"] == "Agent recovered answer"
    assert latest_state["mode_context"]["recovery_request"]["action"] == "restart_from_phase"
    previous_state = artifact_store.load_state(previous)
    assert previous_state["status"] == "superseded"
    assert previous_state["mode_context"]["superseded_by_run_id"] == latest.run_id
    assert previous_recovery["last_requested_operation"]["executed_operation"] == "restart_from_phase"
    assert previous_recovery["last_requested_operation"]["executed_via"] == "agent_recovery_hook:restart_from_phase"
    assert previous_recovery["last_requested_operation"]["spawned_run_id"] == latest.run_id


@pytest.mark.asyncio
async def test_agent_resume_run_accepts_healthy_no_action_report(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T220275Z_agent_resume004"])

    app = _build_app(tmp_path)
    _install_runtime(app, ["Agent resume-ready answer"])
    session = _prepare_session(app, tmp_path)
    artifact_store = _run_store(app)

    run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T220260Z_agent_resume003",
        phase="plan",
        source_prompt_hash="sha256:agent-resume",
    )
    artifact_store.save_plan(
        run,
        {
            "kind": "agent_orchestrator",
            "units": [{"id": "agent:plan:1", "step_type": "plan"}],
        },
    )
    artifact_store.save_state(
        run,
        {
            "phase": "plan",
            "status": "running",
            "mode_context": {},
        },
    )
    monkeypatch.setattr(session, "is_active_by_tick", lambda: False)

    result = await app.mode_run_operations.resume_run(
        session=session,
        mode_id="agent",
        run_id=run.run_id,
    )

    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    state = artifact_store.load_state(run)

    assert result.status == "ok"
    assert result.recommended_action is None
    assert result.report is not None
    assert result.report["recommended_action"] == "no_action"
    assert result.report["can_resume"] is True
    assert "готов к продолжению" in result.message
    assert recovery["last_requested_operation"]["operation"] == "resume"
    assert recovery["last_requested_operation"]["status"] == "prepared"
    assert state["status"] == "running"
    assert artifact_store.latest_run(session=session, mode_id="agent").run_id == run.run_id


@pytest.mark.asyncio
async def test_agent_run_artifacts_isolate_sequential_runs_with_different_prompts(tmp_path, monkeypatch) -> None:
    _patch_run_ids(
        monkeypatch,
        [
            "run_20260312T220300Z_agent_first001",
            "run_20260312T220301Z_agent_second002",
        ],
    )

    app = _build_app(tmp_path)
    _install_runtime(app, ["Первый ответ агента", "Второй ответ агента"])
    session = _prepare_session(app, tmp_path)
    artifact_store = _run_store(app)

    await app.run_mode_pipeline(
        session,
        "Сначала проанализируй кодовую базу",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="agent",
    )
    first = artifact_store.latest_run(session=session, mode_id="agent")
    assert first is not None
    first_state = artifact_store.load_state(first)

    await app.run_mode_pipeline(
        session,
        "Потом предложи конкретный план доработки",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="agent",
    )
    second = artifact_store.latest_run(session=session, mode_id="agent")
    assert second is not None
    second_state = artifact_store.load_state(second)

    assert first.run_id != second.run_id
    assert first_state["source_prompt_hash"] != second_state["source_prompt_hash"]
    assert first_state["mode_context"]["final_deliverable"] == "Первый ответ агента"
    assert second_state["mode_context"]["final_deliverable"] == "Второй ответ агента"
    assert first_state["mode_context"].get("resume_guard", {}) == {}
    assert second_state["mode_context"].get("resume_guard", {}) == {}
