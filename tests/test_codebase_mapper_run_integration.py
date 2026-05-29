import json
import asyncio
import types
from pathlib import Path
from unittest.mock import Mock

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_observability_service import RunObservabilityService
from app.services.run_doctor_service import RunDoctorService, RunDoctorIssue
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from modes.codebase_mapper.mode import CodebaseMapperMode
from modes.codebase_mapper.runtime import CodebaseMapperRuntime


def _mapper_state_payload(**overrides) -> dict:
    payload = {
        "state": "validated",
        "operation": "run",
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


def test_codebase_mapper_generates_run_artifacts(tmp_path: Path):
    config = types.SimpleNamespace(defaults=types.SimpleNamespace(workdir=str(tmp_path), run_artifacts_enabled=True))
    store = RunArtifactStore(config=config)
    obs = RunObservabilityService(enabled=True, artifact_store=store)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(enabled=True, artifact_store=store, boundary_validator=boundary)

    # Mock the mode
    mode = Mock(spec=CodebaseMapperMode)
    mode.get_service.side_effect = lambda x: {
        "run_artifacts": store,
        "run_observability": obs,
        "run_doctor": doctor,
        "run_boundary_validation": boundary
    }.get(x)

    runtime = CodebaseMapperRuntime(mode=mode)

    session = types.SimpleNamespace(
        id="test_sess",
        workdir=str(tmp_path),
        conversation_scope=types.SimpleNamespace(session_uid="uid")
    )

    # Mock run method
    map_dir = tmp_path / ".cli-proxy/.codebase_map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(json.dumps(_mapper_state_payload(nodes_count=2), ensure_ascii=False), encoding="utf-8")
    (map_dir / "graph.json").write_text('{"nodes":[{"id":"n1"},{"id":"n2"}],"edges":[],"tree":[]}', encoding="utf-8")
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")

    runtime.run = Mock(return_value=types.SimpleNamespace(
        status="completed",
        mode="enabled",
        reason="ok",
        map_dir=str(map_dir),
        changed_files=[],
        as_dict=lambda: {"status": "completed", "map_dir": str(map_dir)}
    ))

    async def run_test():
        await runtime.maybe_run(
            session=session,
            workdir=str(tmp_path),
            usage="auto",
            operation="run"
        )

    asyncio.run(run_test())

    # Verify artifacts
    run = store.latest_run(session=session, mode_id="codebase_mapper")
    assert run is not None

    state = store.load_state(run)
    assert state["mode_id"] == "codebase_mapper"
    assert state["status"] == "completed"

    ctx = state.get("mode_context", {})
    assert ctx.get("operation") == "run"
    assert ctx.get("map_dir") == str(map_dir)

    checkpoints = store.load_checkpoints(run)
    plan = store.load_plan(run)
    events = [json.loads(line) for line in Path(run.events_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert list(checkpoints.get("items") or [])
    assert state.get("checkpoint_index") == 1
    assert plan.get("operation") == "run"
    assert plan.get("usage") == "auto"
    assert any(item.get("event_type") == "codebase_mapper_operation_start" for item in events)
    assert any(item.get("event_type") == "codebase_mapper_operation_end" for item in events)


def test_codebase_mapper_marks_run_failed_when_boundary_validation_fails(tmp_path: Path):
    config = types.SimpleNamespace(defaults=types.SimpleNamespace(workdir=str(tmp_path), run_artifacts_enabled=True))
    store = RunArtifactStore(config=config)
    obs = RunObservabilityService(enabled=True, artifact_store=store)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(enabled=True, artifact_store=store, boundary_validator=boundary)

    mode = Mock(spec=CodebaseMapperMode)
    mode.get_service.side_effect = lambda x: {
        "run_artifacts": store,
        "run_observability": obs,
        "run_doctor": doctor,
        "run_boundary_validation": boundary
    }.get(x)

    runtime = CodebaseMapperRuntime(mode=mode)
    session = types.SimpleNamespace(
        id="test_sess",
        workdir=str(tmp_path),
        conversation_scope=types.SimpleNamespace(session_uid="uid")
    )

    runtime.run = Mock(return_value=types.SimpleNamespace(
        status="completed",
        mode="enabled",
        reason="ok",
        map_dir=str(tmp_path / ".cli-proxy/.codebase_map_missing"),
        changed_files=[],
        as_dict=lambda: {"status": "completed", "map_dir": str(tmp_path / ".cli-proxy/.codebase_map_missing")}
    ))

    async def run_test():
        await runtime.maybe_run(
            session=session,
            workdir=str(tmp_path),
            usage="auto",
            operation="run"
        )

    asyncio.run(run_test())

    run = store.latest_run(session=session, mode_id="codebase_mapper")
    assert run is not None

    state = store.load_state(run)
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))

    assert state["status"] == "failed"
    assert recovery["status"] == "needs_recovery"
    assert recovery["recommended_action"] == "run_validate"


def test_mapper_specific_doctor_recovery_options():
    doctor = RunDoctorService(enabled=True, artifact_store=Mock(), boundary_validator=Mock())

    # Missing state -> rerun_same_operation
    action = doctor.recommend_action(
        issues=[RunDoctorIssue(code="missing_state", message="")],
        phase="operation",
        mode_id="codebase_mapper"
    )
    assert action == "rerun_same_operation"

    # Validation failed -> run_repair
    action = doctor.recommend_action(
        issues=[RunDoctorIssue(code="validation_failed", message="")],
        phase="operation",
        mode_id="codebase_mapper"
    )
    assert action == "run_repair"

    # Manual review pending -> manual_review_required
    action = doctor.recommend_action(
        issues=[RunDoctorIssue(code="manual_review_pending", message="")],
        phase="operation",
        mode_id="codebase_mapper"
    )
    assert action == "manual_review_required"

    # Graph corruption -> run_validate
    action = doctor.recommend_action(
        issues=[RunDoctorIssue(code="graph_corrupted", message="")],
        phase="operation",
        mode_id="codebase_mapper"
    )
    assert action == "run_validate"

    # Empty issues -> no_action
    action = doctor.recommend_action(
        issues=[],
        phase="operation",
        mode_id="codebase_mapper"
    )
    assert action == "no_action"


def test_codebase_mapper_skips_doctor_finalize_when_doctor_service_disabled(tmp_path: Path):
    config = types.SimpleNamespace(defaults=types.SimpleNamespace(workdir=str(tmp_path), run_artifacts_enabled=True))
    store = RunArtifactStore(config=config)
    obs = RunObservabilityService(enabled=True, artifact_store=store)
    boundary = RunBoundaryValidationService(enabled=True)
    doctor = RunDoctorService(enabled=False, artifact_store=store, boundary_validator=boundary)

    mode = Mock(spec=CodebaseMapperMode)
    mode.get_service.side_effect = lambda x: {
        "run_artifacts": store,
        "run_observability": obs,
        "run_doctor": doctor,
        "run_boundary_validation": boundary,
    }.get(x)

    runtime = CodebaseMapperRuntime(mode=mode)
    session = types.SimpleNamespace(
        id="test_sess",
        workdir=str(tmp_path),
        conversation_scope=types.SimpleNamespace(session_uid="uid"),
    )

    runtime.run = Mock(
        return_value=types.SimpleNamespace(
            status="completed",
            mode="enabled",
            reason="ok",
            map_dir=str(tmp_path / ".cli-proxy/.codebase_map_missing"),
            changed_files=[],
            as_dict=lambda: {"status": "completed", "map_dir": str(tmp_path / ".cli-proxy/.codebase_map_missing")},
        )
    )
    doctor.diagnose = Mock(wraps=doctor.diagnose)

    async def run_test():
        await runtime.maybe_run(
            session=session,
            workdir=str(tmp_path),
            usage="auto",
            operation="run",
        )

    asyncio.run(run_test())

    run = store.latest_run(session=session, mode_id="codebase_mapper")
    assert run is not None
    recovery = store.load_recovery(run)

    assert doctor.diagnose.call_count == 0
    assert recovery["recommended_action"] == ""
    assert recovery["can_resume"] is False
