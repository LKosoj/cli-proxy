from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_doctor_service import RunDoctorService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.sdk.json_store import read_json_locked, write_json_locked
from modes.sdk.planning import save_plan
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def _build_config(tmp_path: Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
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
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def _session(tmp_path: Path, *, case_id: str, session_uid: str, session_id: str):
    workdir = tmp_path / case_id
    workdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        id=session_id,
        workdir=str(workdir),
        conversation_scope=SimpleNamespace(session_uid=session_uid),
        chat_id=1,
    )


def _prepare_run(tmp_path: Path, *, case_id: str, mode_id: str, phase: str, now_value: float = 1_710_000_000.0):
    cfg = _build_config(tmp_path, intent=case_id)
    store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    service = RunDoctorService(
        enabled=True,
        artifact_store=store,
        boundary_validator=boundary,
        now_fn=lambda: now_value,
    )
    session = _session(
        tmp_path,
        case_id=case_id,
        session_uid=f"thread:-100:{case_id}",
        session_id=f"s-{case_id}",
    )
    run = store.start_run(session=session, mode_id=mode_id, run_id=f"run_20260312T180000Z_{case_id[:8]:0<8}")
    store.save_state(run, {"phase": phase, "mode_context": {}})
    return cfg, store, boundary, service, session, run


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


def _create_manager_plan(workdir: str, *, task_count: int = 1, status: str = "active") -> ProjectPlan:
    tasks = [
        DevTask(
            id=f"TASK-{idx + 1}",
            title=f"Task {idx + 1}",
            description="desc",
            acceptance_criteria=["a"],
            status="pending",
        )
        for idx in range(task_count)
    ]
    plan = ProjectPlan(
        project_goal="Ship feature",
        tasks=tasks,
        analysis=ProjectAnalysis(
            current_state="state",
            already_done=[],
            remaining_work=[],
            requirements=["REQ-1"],
            checklist_table=[{"item": "REQ-1", "status": "done", "how": "ok", "why_not": ""}],
        ),
        status=status,
        current_task_id=tasks[0].id if tasks else None,
    )
    save_plan(workdir, plan)
    return plan


def _save_analyst_context(session, *, template_id: str) -> str:
    key = f"{session.chat_id}_{session.id}"
    path = Path(session.workdir) / ".cli-proxy" / ".analyst_data" / "contexts" / f"{key}.json"
    write_json_locked(
        str(path),
        {
            "mode": "spec",
            "runtime_template_id": template_id,
            "effective_template_id": template_id,
            "intent_reason": "reason",
            "detail_level": "high",
            "document_kind": "spec",
            "last_draft": "",
        },
    )
    return key


def _build_webmaster_user_key(chat_id: int | None, user_id: int | None, session_id: str) -> str:
    c = int(chat_id or 0)
    u = int(user_id or 0)
    if u <= 0 and c > 0:
        u = c
    sid = str(session_id or "").strip()
    if not sid or sid == "0":
        return f"{c}_{u}"
    return f"{c}_{u}_{sid.replace('/', '_').replace('\\\\', '_').strip() or '0'}"


def _save_webmaster_context(session, *, goal: str, validation_json: dict | None = None) -> str:
    key = _build_webmaster_user_key(session.chat_id, session.chat_id, session.id)
    path = Path(session.workdir) / ".cli-proxy" / ".webmaster_data" / "users" / f"{key}.json"
    payload = {
        "task_kind": "new_task",
        "stage": "intent",
        "goal": goal,
        "last_user_text": goal,
    }
    if validation_json is not None:
        payload["last_validation_json"] = dict(validation_json)
    write_json_locked(
        str(path),
        payload,
    )
    return key


def test_run_doctor_missing_plan_after_plan_phase_keeps_analyst_rollback(tmp_path) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id="analyst_missing_plan",
        mode_id="analyst",
        phase="plan",
    )
    analyst_key = _save_analyst_context(session, template_id="default")
    store.save_state(
        run,
        {
            "phase": "plan",
            "status": "running",
            "checkpoint_index": 1,
            "mode_context": {
                "analyst_context_key": analyst_key,
                "intent_payload": {
                    "template_id": "default",
                    "user_text": "Проанализируй требования",
                }
            },
        },
    )
    store.append_checkpoint(run, {"phase": "plan", "status": "ok"})
    Path(run.plan_path).unlink(missing_ok=True)

    report = service.diagnose(run, mode_id="analyst", phase="plan")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "rollback_to_checkpoint"
    assert report.can_resume is False
    assert report.last_consistent_checkpoint == 1
    assert any(issue.code == "missing_plan" for issue in report.issues)
    assert recovery["recommended_action"] == "rollback_to_checkpoint"
    assert any(item["code"] == "missing_plan" for item in recovery["issues"])


def test_run_doctor_allows_not_ready_analyst_final_deliverable_without_state_store_mirror(tmp_path) -> None:
    _cfg, store, _boundary, service, _session, run = _prepare_run(
        tmp_path,
        case_id="analyst_not_ready_final_deliverable",
        mode_id="analyst",
        phase="complete",
    )
    store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "final_deliverable": "Черновик с незакрытыми обязательствами",
            },
        },
    )
    store.save_plan(run, {"units": [{"id": "analyst:complete"}]})
    store.save_metrics(
        run,
        {
            "analyst_quality": {
                "runtime_verdict": "Не готово к реализации",
                "blocking_reasons": ["Незакрытые blocking obligations: 1"],
                "warning_reasons": [],
            }
        },
    )
    store.append_event(run, {"event_type": "analyst_complete", "phase": "complete"})

    report = service.diagnose(run, mode_id="analyst", phase="complete")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "ok"
    assert report.recommended_action == "no_action"
    assert report.can_resume is True
    assert report.issues == []
    assert recovery["recommended_action"] == "no_action"
    assert recovery["issues"] == []


def test_run_doctor_manager_replay_recovery_run_uses_snapshot_without_recursive_action(tmp_path) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id="manager_replay_recovery_healthy",
        mode_id="manager",
        phase="complete",
    )
    live_plan = _create_manager_plan(session.workdir, task_count=1, status="failed")
    live_plan.current_task_id = "TASK-LIVE"
    save_plan(session.workdir, live_plan)
    sync_payload = {
        "synced": True,
        "task_count": 1,
        "legacy_status": "completed",
        "current_task_id": "TASK-1",
        "completion_report_present": True,
        "legacy_updated_at": "2030-01-01 00:00:00",
    }
    replay_snapshot = {
        "status": "completed",
        "tasks": [{"id": "TASK-1"}],
        "current_task_id": "TASK-1",
        "completion_report": "Recovered manager report",
        "updated_at": "2030-01-01 00:00:00",
    }
    store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "final_report": "Recovered manager report",
                "legacy_plan_sync_status": "synced",
                "legacy_plan_sync": dict(sync_payload),
                "recovery_request": {
                    "action": "replay_finalize",
                    "source_run_id": "run_20260312T180500Z_managersrc1",
                },
            },
        },
    )
    store.save_plan(
        run,
        {
            "legacy_plan_sync": dict(sync_payload),
            "recovery_nodes": {
                "replay_finalize": {
                    "source_run_id": "run_20260312T180500Z_managersrc1",
                    "plan_snapshot": dict(replay_snapshot),
                }
            },
        },
    )

    report = service.diagnose(run, mode_id="manager", phase="complete")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "ok"
    assert report.recommended_action == "no_action"
    assert report.issues == []
    assert recovery["recommended_action"] == "no_action"


@pytest.mark.parametrize(
    ("case_suffix", "sync_overrides", "expected_reason"),
    [
        ("task_count", {"task_count": 2}, "replay_snapshot_task_count_mismatch"),
        ("legacy_status", {"legacy_status": "active"}, "replay_snapshot_status_mismatch"),
        ("current_task_id", {"current_task_id": "TASK-2"}, "replay_snapshot_current_task_id_mismatch"),
        ("completion_report", {"completion_report_present": False}, "replay_snapshot_completion_report_mismatch"),
        ("updated_at", {"legacy_updated_at": "2030-01-01 00:05:00"}, "replay_snapshot_updated_at_mismatch"),
        ("updated_at_missing", {"legacy_updated_at": ""}, "replay_snapshot_updated_at_mismatch"),
    ],
    ids=[
        "task_count_mismatch",
        "legacy_status_mismatch",
        "current_task_id_mismatch",
        "completion_report_mismatch",
        "legacy_updated_at_mismatch",
        "legacy_updated_at_missing",
    ],
)
def test_run_doctor_manager_replay_recovery_run_parity_conflicts_require_manual_review(
    tmp_path,
    case_suffix: str,
    sync_overrides: dict,
    expected_reason: str,
) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id=f"manager_replay_recovery_{case_suffix}",
        mode_id="manager",
        phase="complete",
    )
    _create_manager_plan(session.workdir, task_count=1, status="active")
    sync_payload = {
        "synced": True,
        "task_count": 1,
        "legacy_status": "completed",
        "current_task_id": "TASK-1",
        "completion_report_present": True,
        "legacy_updated_at": "2030-01-01 00:00:00",
    }
    sync_payload.update(sync_overrides)
    replay_snapshot = {
        "status": "completed",
        "tasks": [{"id": "TASK-1"}],
        "current_task_id": "TASK-1",
        "completion_report": "Recovered manager report",
        "updated_at": "2030-01-01 00:00:00",
    }
    store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "final_report": "Recovered manager report",
                "legacy_plan_sync_status": "synced",
                "legacy_plan_sync": dict(sync_payload),
                "recovery_request": {
                    "action": "replay_finalize",
                    "source_run_id": f"run_20260312T180600Z_{case_suffix}",
                },
            },
        },
    )
    store.save_plan(
        run,
        {
            "legacy_plan_sync": dict(sync_payload),
            "recovery_nodes": {
                "replay_finalize": {
                    "source_run_id": f"run_20260312T180600Z_{case_suffix}",
                    "plan_snapshot": dict(replay_snapshot),
                }
            },
        },
    )

    report = service.diagnose(run, mode_id="manager", phase="complete")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "manual_review_required"
    assert report.recommended_action != "replay_finalize"
    assert any(
        issue.code == "legacy_store_mismatch" and issue.details.get("reason") == expected_reason
        for issue in report.issues
    )
    assert any(issue.code == "boundary_contract_failed" for issue in report.issues)
    assert recovery["recommended_action"] == "manual_review_required"
    assert recovery["recommended_action"] != "replay_finalize"


def test_run_doctor_manager_replay_recovery_run_state_echo_mismatch_requires_manual_review(tmp_path) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id="manager_replay_recovery_state_echo",
        mode_id="manager",
        phase="complete",
    )
    _create_manager_plan(session.workdir, task_count=1, status="active")
    sync_payload = {
        "synced": True,
        "task_count": 1,
        "legacy_status": "completed",
        "current_task_id": "TASK-1",
        "completion_report_present": True,
        "legacy_updated_at": "2030-01-01 00:00:00",
    }
    replay_snapshot = {
        "status": "completed",
        "tasks": [{"id": "TASK-1"}],
        "current_task_id": "TASK-1",
        "completion_report": "Recovered manager report",
        "updated_at": "2030-01-01 00:00:00",
    }
    store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "final_report": "Recovered manager report",
                "legacy_plan_sync_status": "synced",
                "legacy_plan_sync": {**sync_payload, "current_task_id": "TASK-STALE"},
                "recovery_request": {
                    "action": "replay_finalize",
                    "source_run_id": "run_20260312T180650Z_stateecho",
                },
            },
        },
    )
    store.save_plan(
        run,
        {
            "legacy_plan_sync": dict(sync_payload),
            "recovery_nodes": {
                "replay_finalize": {
                    "source_run_id": "run_20260312T180650Z_stateecho",
                    "plan_snapshot": dict(replay_snapshot),
                }
            },
        },
    )

    report = service.diagnose(run, mode_id="manager", phase="complete")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "manual_review_required"
    assert any(
        issue.code == "legacy_store_mismatch"
        and issue.details.get("reason") == "replay_snapshot_state_echo_mismatch"
        for issue in report.issues
    )
    assert recovery["recommended_action"] == "manual_review_required"


def test_run_doctor_agent_execute_distinguishes_missing_event_evidence_from_malformed_events(tmp_path) -> None:
    _cfg, store, _boundary, service, _session, run = _prepare_run(
        tmp_path,
        case_id="agent_events_malformed",
        mode_id="agent",
        phase="execute",
    )
    store.save_state(
        run,
        {
            "phase": "execute",
            "mode_context": {"required_use_cli_steps": ["use_cli_repo_final_review"]},
        },
    )
    store.append_checkpoint(run, {"phase": "execute", "status": "ok"})
    store.save_metrics(run, {"units": [{"unit_id": "other-step"}]})
    store.append_event(run, {"event_type": "unit_end", "unit_id": "other-step"})

    healthy_report = service.diagnose(run, mode_id="agent", phase="execute")
    healthy_recovery = read_json_locked(run.recovery_path, default={})

    assert healthy_report.status == "needs_recovery"
    assert any(issue.code == "orchestrator_invariant_mismatch" for issue in healthy_report.issues)
    assert all(issue.code != "events_malformed" for issue in healthy_report.issues)
    assert all(item["code"] != "events_malformed" for item in healthy_recovery["issues"])

    Path(run.events_path).write_text("{not-json}\n", encoding="utf-8")

    malformed_report = service.diagnose(run, mode_id="agent", phase="execute")
    malformed_recovery = read_json_locked(run.recovery_path, default={})

    assert malformed_report.status == "needs_recovery"
    assert any(issue.code == "events_malformed" for issue in malformed_report.issues)
    assert any(issue.code == "boundary_contract_failed" for issue in malformed_report.issues)
    assert all(issue.code != "orchestrator_invariant_mismatch" for issue in malformed_report.issues)
    assert any(item["code"] == "events_malformed" for item in malformed_recovery["issues"])


DOCTOR_PARITY_CASES = [
    ("analyst", "intent", "analyst_template_mismatch", "restart_from_phase"),
    ("agent", "execute", "agent_missing_use_cli_evidence", "restart_from_phase"),
    ("manager", "review", "manager_legacy_task_count_mismatch", "replay_finalize"),
    ("webmaster", "validation", "webmaster_validation_json_missing", "replay_finalize"),
]


def _seed_doctor_case(case_id: str, store: RunArtifactStore, session, run) -> None:
    if case_id == "analyst_template_mismatch":
        key = _save_analyst_context(session, template_id="default")
        store.save_state(
            run,
            {
                "phase": "intent",
                "mode_context": {
                    "intent_payload": {"template_id": "strict-template"},
                    "analyst_context_key": key,
                },
            },
        )
        return
    if case_id == "agent_missing_use_cli_evidence":
        store.save_state(
            run,
            {
                "phase": "execute",
                "mode_context": {"required_use_cli_steps": ["use_cli_repo_final_review"]},
            },
        )
        return
    if case_id == "manager_legacy_task_count_mismatch":
        legacy_plan = _create_manager_plan(session.workdir, task_count=2)
        store.save_state(
            run,
            {
                "phase": "review",
                "mode_context": {
                    "review_payload_valid": True,
                    "review_decision_outcome": "approved",
                },
            },
        )
        store.save_plan(
            run,
            {
                "units": [{"id": "TASK-1"}],
                "legacy_plan_sync": {"synced": True, "task_count": len(legacy_plan.tasks) - 1},
            },
        )
        return
    if case_id == "webmaster_validation_json_missing":
        key = _save_webmaster_context(session, goal="Fix layout", validation_json=None)
        store.save_state(
            run,
            {
                "phase": "validation",
                "mode_context": {
                    "webmaster_user_key": key,
                    "developer_report": "\n".join(
                        [
                            "| Пункт | Статус | Evidence |",
                            "| --- | --- | --- |",
                            "| html | PASS | checked |",
                        ]
                    ),
                    "validation_report": {
                        "checklist_rows": [{"item": "html", "status": "PASS", "evidence": "checked"}],
                    },
                },
            },
        )
        return
    raise AssertionError(f"Unhandled doctor case: {case_id}")


@pytest.mark.parametrize(
    ("mode_id", "phase", "case_id", "expected_action"),
    DOCTOR_PARITY_CASES,
    ids=[f"{mode_id}:{phase}:{case_id}" for mode_id, phase, case_id, _action in DOCTOR_PARITY_CASES],
)
def test_run_doctor_deterministic_issue_identification_and_recommendation_parity(
    tmp_path,
    mode_id: str,
    phase: str,
    case_id: str,
    expected_action: str,
) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id=case_id,
        mode_id=mode_id,
        phase=phase,
        now_value=1_710_123_456.0,
    )
    _seed_doctor_case(case_id, store, session, run)

    report_first = service.diagnose(run, mode_id=mode_id, phase=phase)
    report_second = service.diagnose(run, mode_id=mode_id, phase=phase)
    recovery = read_json_locked(run.recovery_path, default={})

    assert report_first.recommended_action == expected_action
    assert report_second.recommended_action == expected_action
    assert report_first.status == "needs_recovery"
    assert report_second.status == "needs_recovery"
    assert [issue.to_dict() for issue in report_first.issues] == [issue.to_dict() for issue in report_second.issues]
    assert report_first.to_dict()["recommended_action"] == report_second.to_dict()["recommended_action"]
    assert len(recovery["attempts"]) == 2
    assert recovery["attempts"][0]["recommended_action"] == expected_action
    assert recovery["attempts"][1]["recommended_action"] == expected_action
    assert recovery["attempts"][0]["issues"] == recovery["attempts"][1]["issues"]


def test_run_doctor_admin_destructive_execution_requires_manual_review(tmp_path) -> None:
    _cfg, store, _boundary, service, _session, run = _prepare_run(
        tmp_path,
        case_id="admin_destructive_manual_review",
        mode_id="admin",
        phase="complete",
        now_value=1_710_777_000.0,
    )
    store.save_plan(run, {"units": [{"id": "admin:manual_run"}]})
    store.append_checkpoint(run, {"phase": "complete", "status": "started"})
    store.save_metrics(run, {"totals": {"units": 1}})
    store.append_event(run, {"event_type": "admin_manual_operation_start"})
    store.save_state(
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

    report = service.diagnose(run, mode_id="admin", phase="complete")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "manual_review_required"
    assert report.can_resume is False
    assert any(issue.code == "admin_destructive_execution_requires_confirmation" for issue in report.issues)
    assert recovery["recommended_action"] == "manual_review_required"


def test_run_doctor_webmaster_corrupt_gate_rows_become_warning_and_constraint(tmp_path) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id="webmaster_gate_payload_degraded",
        mode_id="webmaster",
        phase="validation",
    )
    key = _save_webmaster_context(session, goal="Fix layout", validation_json=None)
    store.save_state(
        run,
        {
            "phase": "validation",
            "status": "running",
            "mode_context": {
                "webmaster_user_key": key,
                "developer_report": "\n".join(
                    [
                        "| Пункт | Статус | Evidence |",
                        "| --- | --- | --- |",
                        "| html | PASS | checked |",
                    ]
                ),
                "validation_report": {
                    "status": "FAIL",
                    "checklist_rows": [{"item": "html", "status": "PASS", "evidence": "checked"}],
                    "gate": {
                        "passed": False,
                        "checklist_table_present": True,
                        "invalid_rows": ["broken-row", {"item": "html", "status": "FAIL"}],
                        "non_pass_rows": [{"item": "styles", "status": "FAIL"}],
                        "missing_evidence_rows": [False, {"item": "scripts"}],
                        "blocking_issue_count": 1,
                    },
                },
            },
        },
    )

    report = service.diagnose(run, mode_id="webmaster", phase="validation")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "replay_finalize"
    assert report.can_resume is False
    assert any(issue.code == "boundary_contract_failed" for issue in report.issues)
    warning_issues = [issue for issue in report.issues if issue.code == "boundary_payload_degraded"]
    assert warning_issues
    assert all(issue.severity == "warning" for issue in warning_issues)
    assert any(issue.details.get("field") == "invalid_rows" for issue in warning_issues)
    assert any(issue.details.get("field") == "missing_evidence_rows" for issue in warning_issues)
    assert recovery["recommended_action"] == "replay_finalize"
    assert any(item["code"] == "boundary_payload_degraded" for item in recovery["issues"])
    assert any(item["code"] == "boundary_contract_failed" for item in recovery["issues"])


def test_run_doctor_isolates_sequential_runs_with_different_intent(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="doctor_isolation")
    store = RunArtifactStore(cfg)
    boundary = RunBoundaryValidationService(enabled=True)
    service = RunDoctorService(
        enabled=True,
        artifact_store=store,
        boundary_validator=boundary,
        now_fn=lambda: 1_710_555_000.0,
    )
    session_a = _session(tmp_path, case_id="intent-a", session_uid="thread:-100:intent-a", session_id="s-a")
    session_b = _session(tmp_path, case_id="intent-b", session_uid="thread:-100:intent-b", session_id="s-b")
    run_a = store.start_run(session=session_a, mode_id="analyst", run_id="run_20260312T181000Z_aaaabbbb")
    run_b = store.start_run(session=session_b, mode_id="analyst", run_id="run_20260312T181100Z_ccccdddd")

    key_a = _save_analyst_context(session_a, template_id="default")
    store.save_state(
        run_a,
        {
            "phase": "intent",
            "mode_context": {"intent_payload": {"template_id": "default"}, "analyst_context_key": key_a},
        },
    )
    store.save_state(
        run_b,
        {
            "phase": "intent",
            "mode_context": {"intent_payload": {"template_id": "missing-context"}},
        },
    )

    report_a = service.diagnose(run_a, mode_id="analyst", phase="intent")
    report_b = service.diagnose(run_b, mode_id="analyst", phase="intent")

    assert report_a.status == "ok"
    assert report_b.status == "needs_recovery"
    assert report_a.issues == []
    assert any(issue.code == "legacy_store_mismatch" for issue in report_b.issues)


def test_run_doctor_codebase_mapper_boundary_failure_recommends_repair(tmp_path) -> None:
    _cfg, store, _boundary, service, _session, run = _prepare_run(
        tmp_path,
        case_id="codebase_mapper_doctor",
        mode_id="codebase_mapper",
        phase="operation",
        now_value=1_710_999_000.0,
    )
    map_dir = Path(run.run_dir) / "artifacts" / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(
        json.dumps(
            _mapper_state_payload(
                state="repair_pending",
                operation="repair",
                nodes_count=3,
                needs_review=["n1"],
                validate_queue=["n1"],
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (map_dir / "graph.json").write_text(
        '{"nodes":[{"id":"n1"},{"id":"n2"}],"edges":[],"tree":[]}',
        encoding="utf-8",
    )
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    store.save_state(
        run,
        {
            "phase": "operation",
            "status": "completed",
            "mode_context": {
                "operation": "repair",
                "map_dir": str(map_dir),
                "status": "completed",
                "validate_queue": ["n1"],
                "needs_review": ["n1"],
            },
        },
    )

    report = service.diagnose(run, mode_id="codebase_mapper", phase="operation")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "manual_review_required"
    assert any(issue.code == "boundary_contract_failed" for issue in report.issues)
    assert recovery["recommended_action"] == "manual_review_required"


def test_run_doctor_preserves_requested_operation_audit_trail_across_sequential_diagnoses(tmp_path) -> None:
    _cfg, store, _boundary, service, session, run = _prepare_run(
        tmp_path,
        case_id="doctor_recovery_merge",
        mode_id="analyst",
        phase="plan",
        now_value=1_711_001_000.0,
    )
    analyst_key = _save_analyst_context(session, template_id="default")
    store.save_state(
        run,
        {
            "phase": "plan",
            "status": "running",
            "checkpoint_index": 1,
            "mode_context": {
                "analyst_context_key": analyst_key,
                "intent_payload": {"template_id": "default", "user_text": "Проведи аудит"},
            },
        },
    )
    store.append_checkpoint(run, {"phase": "plan", "status": "ok"})
    Path(run.plan_path).unlink(missing_ok=True)
    store.save_recovery(
        run,
        {
            "status": "needs_recovery",
            "recommended_action": "rollback_to_checkpoint",
            "last_requested_operation": {
                "operation": "recover",
                "status": "executed",
                "requested_at": 55.0,
                "executed_at": 56.0,
            },
            "requested_operations": [
                {
                    "operation": "recover",
                    "status": "executed",
                    "requested_at": 55.0,
                    "executed_at": 56.0,
                }
            ],
            "attempts": [
                {
                    "diagnosed_at": 54.0,
                    "mode_id": "analyst",
                    "phase": "plan",
                    "recommended_action": "rollback_to_checkpoint",
                    "issues": [{"code": "missing_plan"}],
                }
            ],
        },
    )
    diag_times = iter([1_711_001_001.0, 1_711_001_002.0])
    service._now = lambda: next(diag_times)

    report_first = service.diagnose(run, mode_id="analyst", phase="plan")
    report_second = service.diagnose(run, mode_id="analyst", phase="plan")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report_first.status == "needs_recovery"
    assert report_second.status == "needs_recovery"
    assert recovery["recommended_action"] == "rollback_to_checkpoint"
    assert recovery["diagnosed_at"] == 1_711_001_002.0
    assert recovery["last_requested_operation"]["operation"] == "recover"
    assert recovery["last_requested_operation"]["requested_at"] == 55.0
    assert recovery["last_requested_operation"]["executed_at"] == 56.0
    assert len(recovery["requested_operations"]) == 1
    assert recovery["requested_operations"][0]["requested_at"] == 55.0
    assert len(recovery["attempts"]) == 3
    assert recovery["attempts"][0]["diagnosed_at"] == 54.0
    assert recovery["attempts"][1]["diagnosed_at"] == 1_711_001_001.0
    assert recovery["attempts"][2]["diagnosed_at"] == 1_711_001_002.0


def test_run_doctor_codebase_mapper_graph_corruption_prefers_validation_without_resume(tmp_path) -> None:
    _cfg, store, _boundary, service, _session, run = _prepare_run(
        tmp_path,
        case_id="codebase_mapper_graph_corruption",
        mode_id="codebase_mapper",
        phase="operation",
        now_value=1_710_999_500.0,
    )
    map_dir = Path(run.run_dir) / "artifacts" / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(json.dumps(_mapper_state_payload(nodes_count=2), ensure_ascii=False), encoding="utf-8")
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    store.save_state(
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

    report = service.diagnose(run, mode_id="codebase_mapper", phase="operation")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "needs_recovery"
    assert report.recommended_action == "run_validate"
    assert report.can_resume is False
    assert any(issue.code == "boundary_contract_failed" for issue in report.issues)
    assert recovery["recommended_action"] == "run_validate"


def test_run_doctor_healthy_analyst_and_manager_runs_allow_resume_on_no_action(tmp_path) -> None:
    analyst_cfg, analyst_store, _analyst_boundary, analyst_service, analyst_session, analyst_run = _prepare_run(
        tmp_path,
        case_id="analyst_healthy_resume",
        mode_id="analyst",
        phase="intent",
        now_value=1_710_999_650.0,
    )
    _ = analyst_cfg
    analyst_key = _save_analyst_context(analyst_session, template_id="default")
    analyst_store.save_state(
        analyst_run,
        {
            "phase": "intent",
            "status": "running",
            "mode_context": {
                "intent_payload": {"template_id": "default"},
                "analyst_context_key": analyst_key,
            },
        },
    )

    analyst_report = analyst_service.diagnose(analyst_run, mode_id="analyst", phase="intent")
    analyst_recovery = read_json_locked(analyst_run.recovery_path, default={})

    assert analyst_report.status == "ok"
    assert analyst_report.recommended_action == "no_action"
    assert analyst_report.can_resume is True
    assert analyst_recovery["recommended_action"] == "no_action"
    assert analyst_recovery["can_resume"] is True

    _cfg, manager_store, _manager_boundary, manager_service, manager_session, manager_run = _prepare_run(
        tmp_path,
        case_id="manager_healthy_resume",
        mode_id="manager",
        phase="plan",
        now_value=1_710_999_651.0,
    )
    legacy_plan = _create_manager_plan(manager_session.workdir, task_count=1, status="active")
    manager_sync = {
        "synced": True,
        "task_count": len(legacy_plan.tasks),
        "legacy_updated_at": legacy_plan.updated_at,
        "legacy_status": legacy_plan.status,
        "current_task_id": legacy_plan.current_task_id,
        "completion_report_present": False,
    }
    manager_store.save_state(
        manager_run,
        {
            "phase": "plan",
            "status": "running",
            "checkpoint_index": 1,
            "mode_context": {
                "decompose_payload_valid": True,
                "dynamic_validation_passed": True,
                "legacy_plan_sync": dict(manager_sync),
            },
        },
    )
    manager_store.save_plan(
        manager_run,
        {
            "units": [{"id": "TASK-1"}],
            "legacy_plan_sync": dict(manager_sync),
        },
    )
    manager_store.append_checkpoint(manager_run, {"phase": "plan", "status": "ok"})

    manager_report = manager_service.diagnose(manager_run, mode_id="manager", phase="plan")
    manager_recovery = read_json_locked(manager_run.recovery_path, default={})

    assert manager_report.status == "ok"
    assert manager_report.recommended_action == "no_action"
    assert manager_report.can_resume is True
    assert manager_recovery["recommended_action"] == "no_action"
    assert manager_recovery["can_resume"] is True


def test_run_doctor_codebase_mapper_healthy_run_has_no_recovery_action(tmp_path) -> None:
    _cfg, store, _boundary, service, _session, run = _prepare_run(
        tmp_path,
        case_id="codebase_mapper_healthy",
        mode_id="codebase_mapper",
        phase="operation",
        now_value=1_710_999_700.0,
    )
    map_dir = Path(run.run_dir) / "artifacts" / "mapper-map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "meta.json").write_text('{"version":1}', encoding="utf-8")
    (map_dir / "state.json").write_text(json.dumps(_mapper_state_payload(nodes_count=2), ensure_ascii=False), encoding="utf-8")
    (map_dir / "graph.json").write_text(
        '{"nodes":[{"id":"n1"},{"id":"n2"}],"edges":[],"tree":[]}',
        encoding="utf-8",
    )
    (map_dir / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    store.save_state(
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

    report = service.diagnose(run, mode_id="codebase_mapper", phase="operation")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "ok"
    assert report.recommended_action == "no_action"
    assert report.can_resume is False
    assert recovery["recommended_action"] == "no_action"


def test_run_doctor_disabled_mode_is_conservative_for_codebase_mapper(tmp_path) -> None:
    _cfg, store, boundary, _service, _session, run = _prepare_run(
        tmp_path,
        case_id="codebase_mapper_disabled_doctor",
        mode_id="codebase_mapper",
        phase="operation",
        now_value=1_711_000_100.0,
    )
    service = RunDoctorService(enabled=False, artifact_store=store, boundary_validator=boundary)

    report = service.diagnose(run, mode_id="codebase_mapper", phase="operation")
    recovery = read_json_locked(run.recovery_path, default={})

    assert report.status == "ok"
    assert report.recommended_action == "no_action"
    assert report.can_resume is False
    assert recovery["recommended_action"] == "no_action"
    assert recovery["can_resume"] is False
