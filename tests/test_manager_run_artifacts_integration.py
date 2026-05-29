import json
from pathlib import Path

import pytest

from agent.manager import ManagerOrchestrator
from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_doctor_service import RunDoctorService
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.sdk.planning import load_plan, save_plan
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan, ReviewResult


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
            manager_auto_resume=True,
            manager_auto_commit=False,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def _run_store(app: BotApp) -> RunArtifactStore:
    return RunArtifactStore(app.config)


def _silence_transport(app: BotApp, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _send_message(_context, **_kwargs):
        return True

    async def _send_output(_session, _dest, _output, _context, **_kwargs):
        return None

    monkeypatch.setattr(app, "_send_message", _send_message)
    monkeypatch.setattr(app, "send_output", _send_output)


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


def _prepare_session(app: BotApp, tmp_path):
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = "manager"
    session.project_root = str(tmp_path / "project-root")
    Path(session.project_root).mkdir(parents=True, exist_ok=True)
    return session


def _install_runtime(monkeypatch: pytest.MonkeyPatch, app: BotApp, *, reports: list[str]) -> ManagerOrchestrator:
    runtime = app.mode_runtime_registry.get("manager")
    assert runtime is not None
    orch = runtime._orchestrator
    report_iter = iter(list(reports))
    last_report = reports[-1]

    async def _decompose(_session, user_goal: str, **_kwargs):
        return ProjectPlan(
            project_goal=str(user_goal or "").strip(),
            tasks=[
                DevTask(
                    id="TASK-1",
                    title="Task 1",
                    description="Сделать первую доработку",
                    acceptance_criteria=["REQ-1"],
                )
            ],
            analysis=ProjectAnalysis(
                current_state="initial state",
                already_done=[],
                remaining_work=["Task 1"],
                requirements=["REQ-1"],
                checklist_table=[{"item": "REQ-1", "status": "done", "how": "mapped", "why_not": ""}],
            ),
            status="active",
        )

    async def _notify_plan(*_args, **_kwargs):
        return None

    async def _delegate_develop(_session, _plan, task, **_kwargs):
        return True, f"dev report for {task.id}"

    async def _delegate_review(_session, _plan, _task, _bot, _context, _dest):
        return ReviewResult(approved=True, summary="ok", comments="review ok")

    async def _make_decision(_task, _review, workdir=""):
        _ = workdir
        return "approved", []

    async def _auto_commit(*_args, **_kwargs):
        return False

    async def _reconcile_noop(*_args, **_kwargs):
        return None

    async def _final_audit(*_args, **_kwargs):
        return {
            "passed": True,
            "summary_text": "",
            "result": {"status": "PASS", "fixes_applied": [], "remaining_gaps": []},
        }

    async def _compose_final_report(_plan, workdir=""):
        _ = workdir
        nonlocal last_report
        try:
            last_report = next(report_iter)
        except StopIteration:
            return last_report
        return last_report

    async def _auto_commit_baseline(*_args, **_kwargs):
        return None

    orch._decompose = _decompose
    orch._notify_plan = _notify_plan
    orch._delegate_develop = _delegate_develop
    orch._delegate_review = _delegate_review
    orch._make_decision = _make_decision
    orch._auto_commit = _auto_commit
    orch._reconcile_plan_after_commit = _reconcile_noop
    orch._reconcile_plan_after_change_audit = _reconcile_noop
    orch._run_final_spec_audit_and_close_gaps = _final_audit
    orch._compose_final_report = _compose_final_report
    orch._auto_commit_baseline_before_first_step = _auto_commit_baseline
    orch._git_is_usable = lambda _workdir: False
    orch._snapshot_workdir = lambda _workdir: {}
    orch._diff_snapshots = lambda _before, _after: {"created": [], "modified": [], "deleted": []}
    orch._format_change_audit = lambda _diff: ""
    monkeypatch.setattr("agent.manager_core.archive_plan", lambda *_args, **_kwargs: None)
    return orch


@pytest.mark.asyncio
async def test_manager_plan_updates_echo_into_run_state(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T230100Z_manager_success001"])
    app = _build_app(tmp_path)
    _silence_transport(app, monkeypatch)
    _install_runtime(monkeypatch, app, reports=["Manager final report"])
    session = _prepare_session(app, tmp_path)

    await app.run_mode_pipeline(
        session,
        "Составь и выполни план разработки",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="manager",
    )

    store = _run_store(app)
    run = store.latest_run(session=session, mode_id="manager")
    assert run is not None

    assert Path(run.state_path).exists()
    assert Path(run.plan_path).exists()
    assert Path(run.checkpoints_path).exists()
    assert Path(run.recovery_path).exists()
    assert Path(run.metrics_path).exists()
    assert Path(run.events_path).exists()

    state = store.load_state(run)
    legacy_plan = load_plan(session.workdir)
    assert legacy_plan is not None

    assert state["status"] == "completed"
    assert state["phase"] == "complete"
    assert state["mode_context"]["legacy_phase"] == "final"
    assert state["mode_context"]["final_report"] == "Manager final report"
    sync_payload = state["mode_context"]["legacy_plan_sync"]
    assert sync_payload["synced"] is True
    assert sync_payload["legacy_status"] == "completed"
    assert sync_payload["task_count"] == len(legacy_plan.tasks)
    assert sync_payload["legacy_updated_at"] == legacy_plan.updated_at
    assert sync_payload["current_task_id"] == legacy_plan.current_task_id
    assert sync_payload["completion_report_present"] is True

    plan_payload = json.loads(Path(run.plan_path).read_text(encoding="utf-8"))
    assert plan_payload["legacy_plan_sync"] == sync_payload
    assert [item["run_phase"] for item in plan_payload["boundary_map"]] == ["plan", "develop", "review", "complete"]

    checkpoints_payload = json.loads(Path(run.checkpoints_path).read_text(encoding="utf-8"))
    assert [item["phase"] for item in checkpoints_payload["items"]] == ["develop", "review", "complete"]


@pytest.mark.asyncio
async def test_manager_doctor_detects_plan_desync_when_run_plan_lags_legacy(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T230200Z_manager_desync001"])
    app = _build_app(tmp_path)
    _silence_transport(app, monkeypatch)
    _install_runtime(monkeypatch, app, reports=["Manager report before desync"])
    session = _prepare_session(app, tmp_path)

    await app.run_mode_pipeline(
        session,
        "Собери и заверши manager-план",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="manager",
    )

    store = _run_store(app)
    run = store.latest_run(session=session, mode_id="manager")
    assert run is not None

    legacy_plan = load_plan(session.workdir)
    assert legacy_plan is not None
    legacy_plan.current_task_id = "TASK-1-stale"
    legacy_plan.set_status("failed")
    monkeypatch.setattr("modes.sdk.planning._now_iso", lambda: "2030-01-01 00:00:00")
    save_plan(session.workdir, legacy_plan)

    doctor = RunDoctorService(
        enabled=True,
        artifact_store=store,
        boundary_validator=RunBoundaryValidationService(enabled=True),
    )
    report = doctor.diagnose(run, mode_id="manager", phase="complete")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert report.status == "needs_recovery"
    assert report.recommended_action == "replay_finalize"
    assert any(
        issue.code == "legacy_store_mismatch" and issue.details.get("reason") == "plan_lagging_behind_legacy"
        for issue in report.issues
    )
    assert any(
        issue.code == "legacy_store_mismatch" and issue.details.get("reason") == "legacy_status_mismatch"
        for issue in report.issues
    )
    assert recovery["status"] == "needs_recovery"
    assert recovery["recommended_action"] == "replay_finalize"


@pytest.mark.asyncio
async def test_manager_recover_run_uses_historical_snapshot_without_touching_live_plan(tmp_path, monkeypatch) -> None:
    _patch_run_ids(
        monkeypatch,
        [
            "run_20260312T230250Z_manager_recover001",
            "run_20260312T230251Z_manager_recover002",
        ],
    )
    app = _build_app(tmp_path)
    _silence_transport(app, monkeypatch)
    _install_runtime(monkeypatch, app, reports=["Manager report before replay"])
    session = _prepare_session(app, tmp_path)

    await app.run_mode_pipeline(
        session,
        "Собери manager-план и доведи его до завершения",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="manager",
    )

    store = _run_store(app)
    run = store.latest_run(session=session, mode_id="manager")
    assert run is not None

    legacy_plan = load_plan(session.workdir)
    assert legacy_plan is not None
    legacy_plan.current_task_id = "TASK-LIVE"
    legacy_plan.completion_report = "Live manager plan should stay untouched"
    legacy_plan.set_status("active")
    monkeypatch.setattr("modes.sdk.planning._now_iso", lambda: "2030-01-01 00:00:00")
    save_plan(session.workdir, legacy_plan)
    monkeypatch.setattr(session, "is_active_by_tick", lambda: False)

    result = await app.mode_run_operations.recover_run(
        session=session,
        mode_id="manager",
        run_id=run.run_id,
    )

    state = store.load_state(run)
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    plan_payload = json.loads(Path(run.plan_path).read_text(encoding="utf-8"))
    latest = store.latest_run(session=session, mode_id="manager")
    assert latest is not None
    latest_state = store.load_state(latest)
    latest_plan_payload = json.loads(Path(latest.plan_path).read_text(encoding="utf-8"))
    restored_live_plan = load_plan(session.workdir)
    assert restored_live_plan is not None

    assert result.status == "ok"
    assert result.recommended_action == "replay_finalize"
    assert "replay finalize выполнен" in result.message
    assert latest.run_id != run.run_id
    assert result.report is not None
    assert state["status"] == "superseded"
    assert state["phase"] == "complete"
    assert state["mode_context"]["superseded_by_run_id"] == latest.run_id
    assert state["mode_context"]["recovery_action"] == "replay_finalize"
    assert plan_payload["legacy_plan_sync"]["current_task_id"] == "TASK-1"
    assert plan_payload["recovery_nodes"]["replay_finalize"]["source_run_id"] == run.run_id
    assert plan_payload["recovery_nodes"]["replay_finalize"]["plan_snapshot"]["completion_report"] == "Manager report before replay"
    assert latest_state["status"] == "completed"
    assert latest_state["phase"] == "complete"
    assert latest_state["mode_context"]["final_report"] == "Manager report before replay"
    assert latest_state["mode_context"]["legacy_plan_sync"]["current_task_id"] == "TASK-1"
    assert latest_state["mode_context"]["recovery_request"]["source_run_id"] == run.run_id
    assert latest_plan_payload["legacy_plan_sync"]["current_task_id"] == "TASK-1"
    assert latest_plan_payload["recovery_nodes"]["replay_finalize"]["source_run_id"] == run.run_id
    assert latest_plan_payload["recovery_nodes"]["replay_finalize"]["plan_snapshot"]["completion_report"] == "Manager report before replay"
    assert recovery["last_requested_operation"]["operation"] == "recover"
    assert recovery["last_requested_operation"]["status"] == "executed"
    assert recovery["last_requested_operation"]["executed_operation"] == "replay_finalize"
    assert recovery["last_requested_operation"]["executed_via"] == "manager_replay_finalize"
    assert recovery["last_requested_operation"]["spawned_run_id"] == latest.run_id
    assert recovery["last_requested_operation"]["recovery_nodes"]["replay_finalize"]["source_run_id"] == run.run_id
    assert recovery["recommended_action"] == ""
    assert recovery["can_resume"] is False
    assert restored_live_plan.current_task_id == "TASK-LIVE"
    assert restored_live_plan.completion_report == "Live manager plan should stay untouched"


@pytest.mark.asyncio
async def test_manager_resume_run_accepts_healthy_no_action_report(tmp_path, monkeypatch) -> None:
    _patch_run_ids(monkeypatch, ["run_20260312T230275Z_manager_resume001"])
    app = _build_app(tmp_path)
    _silence_transport(app, monkeypatch)
    _install_runtime(monkeypatch, app, reports=["Manager resume-ready report"])
    session = _prepare_session(app, tmp_path)

    await app.run_mode_pipeline(
        session,
        "Собери стабильный manager-план для resume-проверки",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="manager",
    )

    store = _run_store(app)
    run = store.latest_run(session=session, mode_id="manager")
    assert run is not None

    monkeypatch.setattr(session, "is_active_by_tick", lambda: False)
    result = await app.mode_run_operations.resume_run(
        session=session,
        mode_id="manager",
        run_id=run.run_id,
    )

    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    state = store.load_state(run)

    assert result.status == "ok"
    assert result.recommended_action is None
    assert result.report is not None
    assert result.report["recommended_action"] == "no_action"
    assert result.report["can_resume"] is True
    assert "готов к продолжению" in result.message
    assert recovery["last_requested_operation"]["operation"] == "resume"
    assert recovery["last_requested_operation"]["status"] == "prepared"
    assert state["status"] == "completed"
    assert store.latest_run(session=session, mode_id="manager").run_id == run.run_id


@pytest.mark.asyncio
async def test_manager_run_artifacts_isolate_sequential_runs_with_different_prompts(tmp_path, monkeypatch) -> None:
    _patch_run_ids(
        monkeypatch,
        [
            "run_20260312T230300Z_manager_first001",
            "run_20260312T230400Z_manager_second002",
        ],
    )
    app = _build_app(tmp_path)
    _silence_transport(app, monkeypatch)
    _install_runtime(monkeypatch, app, reports=["Первый manager report", "Второй manager report"])
    session = _prepare_session(app, tmp_path)
    store = _run_store(app)

    await app.run_mode_pipeline(
        session,
        "Сначала доведи до конца первую ветку плана",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="manager",
    )
    first = store.latest_run(session=session, mode_id="manager")
    assert first is not None
    first_state = store.load_state(first)

    await app.run_mode_pipeline(
        session,
        "Потом создай и выполни уже другой manager-план",
        {"kind": "telegram", "chat_id": 1},
        object(),
        mode_id="manager",
    )
    second = store.latest_run(session=session, mode_id="manager")
    assert second is not None
    second_state = store.load_state(second)

    assert first.run_id != second.run_id
    assert first_state["source_prompt_hash"] != second_state["source_prompt_hash"]
    assert first_state["mode_context"]["final_report"] == "Первый manager report"
    assert second_state["mode_context"]["final_report"] == "Второй manager report"
    assert first_state["mode_context"].get("resume_guard", {}) == {}
    assert second_state["mode_context"].get("resume_guard", {}) == {}
