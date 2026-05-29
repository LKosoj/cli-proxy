from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from app.services.mode_run_lifecycle_service import (
    ModeRunLifecycleEventResult,
    ModeRunLifecycleFinishResult,
    ModeRunLifecyclePhaseResult,
    ModeRunLifecycleService,
    ModeRunLifecycleStartResult,
)
from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_observability_service import RunObservabilityService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


class _ExplodingBoundaryValidator:
    def is_enabled(self) -> bool:
        return True

    def validate(self, *_args, **_kwargs):
        raise RuntimeError("boundary validator exploded")


def _build_config(tmp_path: Path) -> AppConfig:
    workdir = tmp_path / "workdir"
    runtime = tmp_path / "runtime"
    logs = tmp_path / "logs"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def _session(tmp_path: Path) -> SimpleNamespace:
    workdir = tmp_path / "session-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        id="session-1",
        workdir=str(workdir),
        conversation_scope=SimpleNamespace(session_uid="thread:-100:lifecycle"),
        chat_id=1,
    )


def test_mode_run_lifecycle_service_start_save_finish_event_happy_path(tmp_path) -> None:
    config = _build_config(tmp_path)
    store = RunArtifactStore(config)
    observability = RunObservabilityService(enabled=True, artifact_store=store)
    boundary_validator = RunBoundaryValidationService(enabled=True)
    service = ModeRunLifecycleService(
        artifact_store=store,
        observability=observability,
        boundary_validator=boundary_validator,
    )
    session = _session(tmp_path)
    map_dir = tmp_path / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)

    start = service.start(
        session=session,
        mode_id="codebase_mapper",
        run_id="run_20260312T190000Z_lifecycle",
        phase="operation",
        mode_context={"operation": "verify", "map_dir": str(map_dir)},
    )

    assert isinstance(start, ModeRunLifecycleStartResult)
    assert isinstance(start.handle, RunArtifactHandle)
    assert start.state["phase"] == "operation"
    assert start.boundary_report is not None
    assert start.boundary_report.status == "ok"
    assert start.phase_event is not None
    assert start.phase_event["event_type"] == "phase_start"
    assert getattr(session, RunObservabilityService.BRIDGE_RUN_ATTR) == start.handle.run_id

    phase = service.save_phase(
        start.handle,
        phase="operation",
        mode_context={"status": "running", "needs_review": []},
    )

    assert isinstance(phase, ModeRunLifecyclePhaseResult)
    assert phase.state["mode_context"]["operation"] == "verify"
    assert phase.state["mode_context"]["map_dir"] == str(map_dir)
    assert phase.state["mode_context"]["status"] == "running"
    assert phase.boundary_report is not None
    assert phase.boundary_report.status == "ok"

    event = service.record_event(
        start.handle,
        event_type="runtime_progress",
        payload={
            "mode_id": "codebase_mapper",
            "phase": "operation",
            "status": "running",
            "message": "tick",
        },
    )

    assert isinstance(event, ModeRunLifecycleEventResult)
    assert event.event["event_type"] == "runtime_progress"
    assert event.event["message"] == "tick"

    finish = service.mark_finished(
        start.handle,
        status="completed",
        phase="operation",
        session=session,
        duration_sec=1.5,
        tool_calls=2,
        message="done",
    )

    assert isinstance(finish, ModeRunLifecycleFinishResult)
    assert finish.state["status"] == "completed"
    assert finish.state["finished_at"] is not None
    assert finish.boundary_report is not None
    assert finish.boundary_report.status == "ok"
    assert finish.phase_event is not None
    assert finish.phase_event["event_type"] == "phase_end"
    assert not hasattr(session, RunObservabilityService.BRIDGE_RUN_ATTR)

    events = store.load_events_tail(start.handle, limit=10)
    assert [item["event_type"] for item in events] == [
        "phase_start",
        "runtime_progress",
        "phase_end",
    ]
    metrics = store.load_metrics(start.handle)
    assert metrics["runtime_progress"]["events"] == 1
    operation_metrics = [
        item for item in metrics["phase_aggregates"] if item.get("phase") == "operation"
    ][0]
    assert operation_metrics["starts"] == 1
    assert operation_metrics["ends"] == 1
    assert operation_metrics["last_status"] == "completed"


def test_mode_run_lifecycle_service_logs_boundary_validator_failure_as_issue(tmp_path, caplog) -> None:
    config = _build_config(tmp_path)
    store = RunArtifactStore(config)
    service = ModeRunLifecycleService(
        artifact_store=store,
        boundary_validator=_ExplodingBoundaryValidator(),
    )
    session = _session(tmp_path)

    with caplog.at_level(logging.ERROR, logger="app.services.mode_run_lifecycle_service"):
        start = service.start(
            session=session,
            mode_id="agent",
            run_id="run_20260312T191000Z_boundary",
            phase="plan",
            mode_context={"required_use_cli_steps": []},
        )

    report = start.boundary_report

    assert report is not None
    assert report.status == "error"
    assert report.mode_id == "agent"
    assert report.phase == "plan"
    assert report.next_allowed_phases == []
    assert [issue.code for issue in report.issues] == ["boundary_validation_exception"]
    assert report.issues[0].details == {
        "category": "best_effort",
        "error_type": "RuntimeError",
    }
    assert "mode run boundary validation failed fallback_category=best_effort" in caplog.text
    assert "mode=agent" in caplog.text
    assert "phase=plan" in caplog.text
    assert "run_id=run_20260312T191000Z_boundary" in caplog.text
