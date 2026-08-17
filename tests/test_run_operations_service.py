import asyncio
import json
import logging
import types
from pathlib import Path

import pytest

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_doctor_service import RunDoctorReport, RunDoctorService
from app.services.run_observability_service import RunObservabilityService
from app.services.run_operations_service import RunOperationsService, blocked_run_operation_signals


def _build_config(tmp_path):
    defaults = types.SimpleNamespace(
        workdir=str(tmp_path),
        run_artifacts_enabled=True,
        run_doctor_enabled=True,
        run_metrics_enabled=True,
        run_boundary_validation_enabled=True,
    )
    return types.SimpleNamespace(defaults=defaults)


def _build_session(tmp_path):
    return types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        conversation_scope=types.SimpleNamespace(session_uid="thread:doctor:1"),
        busy=False,
        run_lock=asyncio.Lock(),
        is_active_by_tick=lambda: False,
    )


def _mapper_state_payload(**overrides) -> dict:
    payload = {
        "state": "validated",
        "operation": "verify",
        "nodes_count": 0,
        "tree": [],
        "review_items": [],
        "needs_review": [],
        "reviewed": {},
        "validate_queue": [],
        "repair_queue": [],
        "validation_report": {},
        "nodes_status": {},
        "relation_graph": {},
    }
    payload.update(overrides)
    return payload


class _BrokenLock:
    def locked(self) -> bool:
        raise RuntimeError("lock unavailable")


class _UnlockedLock:
    def locked(self) -> bool:
        return False


@pytest.mark.parametrize(
    ("session_factory", "signal_name"),
    [
        (
            lambda tmp_path: types.SimpleNamespace(
                id="broken-lock",
                conversation_scope=types.SimpleNamespace(session_uid="thread:run-lock"),
                busy=False,
                run_lock=_BrokenLock(),
                is_active_by_tick=lambda: False,
            ),
            "run_lock",
        ),
        (
            lambda tmp_path: types.SimpleNamespace(
                id="broken-tick",
                conversation_scope=types.SimpleNamespace(session_uid="thread:tick"),
                busy=False,
                run_lock=_UnlockedLock(),
                is_active_by_tick=lambda: (_ for _ in ()).throw(RuntimeError("tick unavailable")),
            ),
            "tick",
        ),
    ],
)
def test_blocked_run_operation_signals_logs_legacy_guard_fallback(caplog, tmp_path, session_factory, signal_name) -> None:
    session = session_factory(tmp_path)

    with caplog.at_level(logging.WARNING, logger="app.services.run_operations_service"):
        assert blocked_run_operation_signals(session, "recover") == ()

    messages = [record.getMessage() for record in caplog.records]
    assert any("legacy fallback used" in message and f"signal={signal_name}" in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signal_name", "expected_signal"),
    [
        ("busy", "busy"),
        ("run_lock", "run_lock"),
        ("tick", "tick"),
    ],
)
async def test_run_operations_write_guards_block_then_recover_after_signal_clears(
    tmp_path,
    signal_name: str,
    expected_signal: str,
) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    tick_state = {"active": False}
    session.is_active_by_tick = lambda: bool(tick_state["active"])
    service = RunOperationsService(
        enabled=True,
        artifact_store=RunArtifactStore(cfg),
        doctor_service=types.SimpleNamespace(),
    )

    if signal_name == "busy":
        session.busy = True
    elif signal_name == "run_lock":
        await session.run_lock.acquire()
    elif signal_name == "tick":
        tick_state["active"] = True

    assert blocked_run_operation_signals(session, "doctor") == ()
    blocked = await service.recover_run(session=session, mode_id="analyst")

    assert blocked.status == "blocked"
    assert blocked.blocked_by == (expected_signal,)
    assert expected_signal in blocked.message

    if signal_name == "busy":
        session.busy = False
    elif signal_name == "run_lock":
        session.run_lock.release()
    elif signal_name == "tick":
        tick_state["active"] = False

    recovered = await service.recover_run(session=session, mode_id="analyst")

    assert recovered.status == "not_found"
    assert recovered.blocked_by == ()
    assert "нет run artifacts" in recovered.message


@pytest.mark.asyncio
async def test_run_operations_doctor_and_recover_read_execution_states(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    executor_calls = []

    async def _executor(**kwargs):
        executor_calls.append(dict(kwargs))
        return {
            "status": "ok",
            "message": "Rollback action executed.",
            "executed_operation": kwargs.get("operation"),
            "executed_via": "fake_executor",
        }

    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
        recommended_action_executor=_executor,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="analyst",
        run_id="run_20260312T201500Z_a1b2c3d4",
        phase="plan",
        source_prompt_hash="sha256:test",
    )
    artifact_store.save_state(
        run,
        {
            "phase": "plan",
            "status": "running",
            "mode_context": {
                "intent_payload": {
                    "template_id": "default",
                    "user_text": "Проанализируй требования",
                }
            },
        },
    )
    Path(run.plan_path).unlink()

    doctor_result = await service.doctor_run(session=session, mode_id="analyst")
    recover_result = await service.recover_run(session=session, mode_id="analyst")

    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    event_lines = [json.loads(line) for line in Path(run.events_path).read_text(encoding="utf-8").splitlines() if line.strip()]

    assert doctor_result.status == "ok"
    assert doctor_result.run_id == run.run_id
    assert doctor_result.recommended_action == "rollback_to_checkpoint"
    assert doctor_result.report is not None
    assert doctor_result.report["status"] == "needs_recovery"
    assert any(item["code"] == "missing_plan" for item in doctor_result.report["issues"])

    assert recover_result.status == "ok"
    assert recover_result.run_id == run.run_id
    assert recover_result.recommended_action == "rollback_to_checkpoint"
    assert recover_result.report is not None
    assert recover_result.report["status"] == "needs_recovery"
    assert recover_result.message == "Rollback action executed."
    assert not Path(run.plan_path).exists()
    assert executor_calls[0]["operation"] == "rollback_to_checkpoint"

    assert recovery["status"] == "needs_recovery"
    assert recovery["recommended_action"] == "rollback_to_checkpoint"
    assert recovery["last_requested_operation"]["operation"] == "recover"
    assert recovery["last_requested_operation"]["status"] == "executed"
    assert recovery["last_requested_operation"]["executed_operation"] == "rollback_to_checkpoint"
    assert recovery["last_requested_operation"]["executed_via"] == "fake_executor"
    assert recovery["requested_operations"][-1]["recommended_action"] == "rollback_to_checkpoint"

    assert any(item["event_type"] == "run_operation" and item["operation"] == "doctor" for item in event_lines)
    assert any(item["event_type"] == "run_operation" and item["operation"] == "recover" for item in event_lines)
    assert any(item["event_type"] == "recovery_attempt" and item["action"] == "recover" for item in event_lines)


@pytest.mark.asyncio
async def test_run_operations_recover_executes_restart_from_phase_via_executor(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    executor_calls = []

    async def _executor(**kwargs):
        executor_calls.append(dict(kwargs))
        return {
            "status": "ok",
            "message": "Restart action executed.",
            "executed_operation": kwargs.get("operation"),
            "executed_via": "fake_executor",
        }

    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
        recommended_action_executor=_executor,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260313T205650Z_restart",
        phase="execute",
        source_prompt_hash="sha256:agent-recover",
    )
    artifact_store.save_state(
        run,
        {
            "phase": "execute",
            "status": "running",
            "mode_context": {
                "source_prompt": "Переиспользуй агентный prompt",
                "required_use_cli_steps": ["agent:use_cli:1"],
            },
        },
    )

    result = await service.recover_run(session=session, mode_id="agent")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "ok"
    assert result.recommended_action == "restart_from_phase"
    assert result.message == "Restart action executed."
    assert executor_calls[0]["operation"] == "restart_from_phase"
    assert recovery["last_requested_operation"]["status"] == "executed"
    assert recovery["last_requested_operation"]["executed_operation"] == "restart_from_phase"
    assert recovery["last_requested_operation"]["executed_via"] == "fake_executor"


@pytest.mark.asyncio
async def test_run_operations_recover_persists_manager_recovery_nodes_nested_roundtrip(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)

    async def _executor(**kwargs):
        return {
            "status": "ok",
            "message": "Replay finalize executed.",
            "executed_operation": kwargs.get("operation"),
            "executed_via": "fake_executor",
        }

    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
        recommended_action_executor=_executor,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="manager",
        run_id="run_20260314T111500Z_managersnap",
        phase="complete",
        source_prompt_hash="sha256:manager-recover",
    )
    artifact_store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "legacy_phase": "final",
                "final_report": "Historical report",
                "recovery_nodes": {
                    "replay_finalize": {
                        "source_run_id": run.run_id,
                        "phase": "complete",
                    }
                },
            },
        },
    )
    artifact_store.save_plan(
        run,
        {
            "plan_kind": "manager_plan",
            "recovery_nodes": {
                "replay_finalize": {
                    "source_run_id": run.run_id,
                    "phase": "complete",
                    "plan_snapshot": {
                        "project_goal": "Ship nested recovery",
                        "status": "completed",
                        "created_at": "2026-03-14 11:00:00",
                        "updated_at": "2026-03-14 11:10:00",
                        "current_task_id": "TASK-1",
                        "completion_report": "Historical report",
                        "analysis": {
                            "current_state": "done",
                            "already_done": ["audit"],
                            "remaining_work": [],
                            "requirements": ["REQ-1"],
                            "checklist_table": [
                                {"item": "REQ-1", "status": "done", "how": "captured", "why_not": ""}
                            ],
                        },
                        "tasks": [
                            {
                                "id": "TASK-1",
                                "title": "Finalize",
                                "description": "Finalize nested payload",
                                "acceptance_criteria": ["REQ-1"],
                                "covers_requirements": ["REQ-1"],
                                "depends_on": [],
                                "status": "approved",
                                "attempt": 1,
                                "max_attempts": 3,
                                "dev_report": "done",
                                "review_verdict": "approved",
                                "review_comments": "ok",
                                "rejection_history": [{"ts": "2026-03-14 10:00:00", "reason": "fixed"}],
                            }
                        ],
                    },
                }
            },
        },
    )
    doctor.diagnose = lambda *_a, **_k: RunDoctorReport(  # type: ignore[method-assign]
        mode_id="manager",
        phase="complete",
        status="needs_recovery",
        issues=[],
        recommended_action="replay_finalize",
        can_resume=False,
        diagnosed_at=1_711_500_000.0,
        last_consistent_checkpoint=1,
    )

    result = await service.recover_run(session=session, mode_id="manager", run_id=run.run_id)
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    persisted_node = recovery["last_requested_operation"]["recovery_nodes"]["replay_finalize"]

    assert result.status == "ok"
    assert result.recommended_action == "replay_finalize"
    assert persisted_node["source_run_id"] == run.run_id
    assert persisted_node["plan_snapshot"]["analysis"]["checklist_table"][0]["item"] == "REQ-1"
    assert persisted_node["plan_snapshot"]["tasks"][0]["rejection_history"][0]["reason"] == "fixed"


@pytest.mark.asyncio
async def test_run_operations_resume_blocks_when_doctor_requires_non_resume_workflow(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="analyst",
        run_id="run_20260313T205500Z_rollback",
        phase="plan",
        source_prompt_hash="sha256:analyst-resume",
    )
    artifact_store.save_state(
        run,
        {
            "phase": "plan",
            "status": "running",
            "mode_context": {
                "intent_payload": {
                    "template_id": "default",
                    "user_text": "Проведи аудит",
                }
            },
        },
    )
    Path(run.plan_path).unlink()

    result = await service.resume_run(session=session, mode_id="analyst")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "blocked"
    assert result.recommended_action == "rollback_to_checkpoint"
    assert "doctor рекомендует другой recovery workflow" in result.message
    assert recovery["last_requested_operation"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_run_operations_resume_accepts_healthy_agent_run_with_no_action(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260313T205575Z_agent_resume",
        phase="plan",
        source_prompt_hash="sha256:agent-resume",
    )
    artifact_store.save_state(
        run,
        {
            "phase": "plan",
            "status": "running",
            "mode_context": {},
        },
    )
    artifact_store.save_plan(
        run,
        {
            "units": [{"id": "agent:plan:1", "step_type": "plan", "title": "Agent plan"}],
        },
    )

    result = await service.resume_run(session=session, mode_id="agent")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "ok"
    assert result.recommended_action is None
    assert result.report is not None
    assert result.report["recommended_action"] == "no_action"
    assert result.report["can_resume"] is True
    assert "готов к продолжению" in result.message
    assert recovery["last_requested_operation"]["operation"] == "resume"
    assert recovery["last_requested_operation"]["status"] == "prepared"


@pytest.mark.asyncio
async def test_run_operations_recover_mark_failed_reports_store_failure(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)

    class _MarkFailedDoctor:
        def diagnose(self, _run, *, mode_id: str, phase: str) -> RunDoctorReport:
            return RunDoctorReport(
                mode_id=mode_id,
                phase=phase,
                status="needs_recovery",
                issues=[],
                recommended_action="mark_failed",
                can_resume=False,
                diagnosed_at=123.0,
                last_consistent_checkpoint=-1,
            )

    def _mark_finished_raises(*_args, **_kwargs) -> None:
        raise RuntimeError("store is read-only")

    artifact_store.mark_finished = _mark_finished_raises  # type: ignore[method-assign]
    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=_MarkFailedDoctor(),
    )
    run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260313T205580Z_markfailed",
        phase="execute",
        source_prompt_hash="sha256:markfailed",
    )
    artifact_store.save_state(run, {"phase": "execute", "status": "running"})

    result = await service.recover_run(session=session, mode_id="agent")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "error"
    assert result.recommended_action == "mark_failed"
    assert "Не удалось перевести run" in result.message
    assert recovery["last_requested_operation"]["status"] == "error"


@pytest.mark.asyncio
async def test_run_operations_resume_blocks_codebase_mapper_mid_operation_resume(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="codebase_mapper",
        run_id="run_20260313T201600Z_mapperblk",
        phase="operation",
        source_prompt_hash="sha256:mapper",
    )
    map_dir = Path(run.run_dir) / "artifacts" / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(json.dumps(_mapper_state_payload(nodes_count=2), ensure_ascii=False), encoding="utf-8")
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    artifact_store.save_state(
        run,
        {
            "phase": "operation",
            "status": "completed",
            "mode_context": {
                "operation": "verify",
                "map_dir": str(map_dir),
                "status": "validated",
                "validate_queue": [],
                "needs_review": [],
            },
        },
    )

    result = await service.resume_run(session=session, mode_id="codebase_mapper")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "blocked"
    assert result.recommended_action == "run_validate"
    assert "mid-operation resume запрещён" in result.message
    assert recovery["last_requested_operation"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_run_operations_apply_recommendation_executes_codebase_mapper_operation(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    executor_calls = []

    async def _executor(**kwargs):
        executor_calls.append(dict(kwargs))
        return {
            "status": "ok",
            "message": "Validate operation executed.",
            "executed_operation": kwargs.get("operation"),
        }

    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
        recommended_action_executor=_executor,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="codebase_mapper",
        run_id="run_20260313T201605Z_mapperapply",
        phase="operation",
        source_prompt_hash="sha256:mapper-apply",
    )
    map_dir = Path(run.run_dir) / "artifacts" / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(json.dumps(_mapper_state_payload(nodes_count=2), ensure_ascii=False), encoding="utf-8")
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    artifact_store.save_state(
        run,
        {
            "phase": "operation",
            "status": "completed",
            "mode_context": {
                "operation": "verify",
                "map_dir": str(map_dir),
                "status": "validated",
                "validate_queue": [],
                "needs_review": [],
            },
        },
    )

    result = await service.apply_recommendation_run(session=session, mode_id="codebase_mapper")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "ok"
    assert result.operation == "apply_recommendation"
    assert result.recommended_action == "run_validate"
    assert result.message == "Validate operation executed."
    assert executor_calls[0]["operation"] == "validate"
    assert recovery["last_requested_operation"]["operation"] == "apply_recommendation"
    assert recovery["last_requested_operation"]["executed_operation"] == "validate"
    assert recovery["last_requested_operation"]["status"] == "executed"


@pytest.mark.asyncio
async def test_run_operations_apply_recommendation_records_executor_error(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)

    async def _executor(**_kwargs):
        raise RuntimeError("boom")

    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
        recommended_action_executor=_executor,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="codebase_mapper",
        run_id="run_20260313T201606Z_mappererror",
        phase="operation",
        source_prompt_hash="sha256:mapper-error",
    )
    map_dir = Path(run.run_dir) / "artifacts" / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(json.dumps(_mapper_state_payload(nodes_count=2), ensure_ascii=False), encoding="utf-8")
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    artifact_store.save_state(
        run,
        {
            "phase": "operation",
            "status": "completed",
            "mode_context": {
                "operation": "verify",
                "map_dir": str(map_dir),
                "status": "validated",
                "validate_queue": [],
                "needs_review": [],
            },
        },
    )

    result = await service.apply_recommendation_run(session=session, mode_id="codebase_mapper")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    event_lines = [json.loads(line) for line in Path(run.events_path).read_text(encoding="utf-8").splitlines() if line.strip()]

    assert result.status == "error"
    assert recovery["last_requested_operation"]["operation"] == "apply_recommendation"
    assert recovery["last_requested_operation"]["status"] == "error"
    assert recovery["last_requested_operation"]["executed_operation"] == "validate"
    assert any(
        item["event_type"] == "run_operation"
        and item["operation"] == "apply_recommendation"
        and item["status"] == "error"
        for item in event_lines
    )
    assert any(
        item["event_type"] == "recovery_attempt"
        and item["action"] == "apply_recommendation"
        and item["status"] == "error"
        for item in event_lines
    )


@pytest.mark.asyncio
async def test_run_operations_resume_blocks_admin_manual_review_required(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    session = _build_session(tmp_path)
    artifact_store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary,
    )
    observability = RunObservabilityService(enabled=True, artifact_store=artifact_store)
    service = RunOperationsService(
        enabled=True,
        artifact_store=artifact_store,
        doctor_service=doctor,
        observability_service=observability,
    )

    run = artifact_store.start_run(
        session=session,
        mode_id="admin",
        run_id="run_20260313T201700Z_adminblk",
        phase="complete",
        source_prompt_hash="sha256:admin",
    )
    artifact_store.save_state(
        run,
        {
            "phase": "complete",
            "status": "running",
            "mode_context": {
                "operation_payload": {
                    "kind": "manual_run",
                    "action_id": "restart_service",
                    "server_id": "srv-1",
                    "target_transport": "ssh",
                },
                "target_transport": "ssh",
                "execution_context": {
                    "native_transport_execution": True,
                    "skill_selector_bypassed": True,
                    "skill_selector_bypass_reason": "native_admin_transport",
                    "destructive_execution": True,
                    "dry_run": False,
                    "check_only": False,
                    "action_id": "restart_service",
                    "server_id": "srv-1",
                },
            },
        },
    )

    result = await service.resume_run(session=session, mode_id="admin")
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert result.status == "blocked"
    assert result.recommended_action == "manual_review_required"
    assert "ручного подтверждения" in result.message
    assert recovery["last_requested_operation"]["status"] == "blocked"
