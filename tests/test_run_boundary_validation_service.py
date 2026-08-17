from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.sdk.json_store import write_json_locked
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


def _prepare_run(tmp_path: Path, *, case_id: str, mode_id: str, phase: str):
    cfg = _build_config(tmp_path, intent=case_id)
    store = RunArtifactStore(cfg)
    service = RunBoundaryValidationService(enabled=True)
    session = _session(tmp_path, case_id=case_id, session_uid=f"thread:-100:{case_id}", session_id=f"s-{case_id}")
    run = store.start_run(session=session, mode_id=mode_id, run_id=f"run_20260312T170000Z_{case_id[:8]:0<8}")
    store.save_state(run, {"phase": phase, "mode_context": {}})
    return cfg, store, service, session, run


def _create_manager_plan(workdir: str, *, task_count: int = 1, status: str = "active", completion_report: str | None = None) -> ProjectPlan:
    tasks = [
        DevTask(
            id=f"TASK-{idx + 1}",
            title=f"Task {idx + 1}",
            description="desc",
            acceptance_criteria=["a"],
            status="in_review" if idx == 0 else "pending",
            dev_report="dev report" if idx == 0 else None,
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
        completion_report=completion_report,
    )
    save_plan(workdir, plan)
    return plan


def _save_analyst_context(session, *, template_id: str = "default") -> str:
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
        },
    )
    return key


def _save_analyst_quality(
    store: RunArtifactStore,
    run,
    *,
    runtime_verdict: str,
    blocking_reasons: list[str] | None = None,
) -> None:
    store.save_metrics(
        run,
        {
            "analyst_quality": {
                "runtime_verdict": runtime_verdict,
                "blocking_reasons": list(blocking_reasons or []),
            }
        },
    )


def _build_webmaster_user_key(chat_id: int | None, user_id: int | None, session_id: str) -> str:
    c = int(chat_id or 0)
    u = int(user_id or 0)
    if u <= 0 and c > 0:
        u = c
    sid = str(session_id or "").strip()
    if not sid or sid == "0":
        return f"{c}_{u}"
    return f"{c}_{u}_{sid.replace('/', '_').replace('\\\\', '_').strip() or '0'}"


def _save_webmaster_context(session, *, goal: str = "Fix layout", last_cli_report: str = "") -> str:
    key = _build_webmaster_user_key(session.chat_id, session.chat_id, session.id)
    path = Path(session.workdir) / ".cli-proxy" / ".webmaster_data" / "users" / f"{key}.json"
    write_json_locked(
        str(path),
        {
            "task_kind": "new_task",
            "stage": "intent",
            "goal": goal,
            "last_user_text": goal,
            "last_cli_report": last_cli_report,
        },
    )
    return key


def _developer_report_with_checklist() -> str:
    return "\n".join(
        [
            "| Пункт | Статус | Evidence |",
            "| --- | --- | --- |",
            "| html | PASS | checked |",
        ]
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


def _configure_positive_case(case_id: str, store: RunArtifactStore, session, run) -> list[str]:
    if case_id == "analyst_intent":
        key = _save_analyst_context(session)
        store.save_state(
            run,
            {
                "phase": "intent",
                "mode_context": {
                    "intent_payload": {"template_id": "default", "needs_clarification": False},
                    "analyst_context_key": key,
                },
            },
        )
        return ["plan"]
    if case_id == "analyst_plan":
        store.save_plan(run, {"units": [{"id": "unit:plan", "title": "Plan"}]})
        return ["execute"]
    if case_id == "analyst_execute":
        store.append_checkpoint(run, {"phase": "execute", "unit_id": "unit:1", "status": "ok"})
        return ["complete"]
    if case_id == "analyst_complete":
        store.save_state(
            run,
            {
                "phase": "complete",
                "status": "completed",
                "mode_context": {"final_deliverable": "Final analyst report"},
            },
        )
        _save_analyst_quality(store, run, runtime_verdict="Готово к реализации")
        return []
    if case_id == "agent_plan":
        store.save_state(
            run,
            {
                "phase": "plan",
                "mode_context": {"required_use_cli_steps": ["use_cli_repo_final_review"]},
            },
        )
        store.save_plan(
            run,
            {"units": [{"id": "use_cli_repo_final_review", "step_type": "use_cli", "title": "Final review"}]},
        )
        return ["execute"]
    if case_id == "agent_execute":
        store.save_state(
            run,
            {
                "phase": "execute",
                "mode_context": {"required_use_cli_steps": ["use_cli_repo_final_review"]},
            },
        )
        store.save_metrics(run, {"units": [{"unit_id": "use_cli_repo_final_review"}]})
        store.append_event(run, {"event_type": "unit_end", "unit_id": "use_cli_repo_final_review"})
        return ["complete", "review"]
    if case_id == "agent_complete":
        store.save_state(
            run,
            {
                "phase": "complete",
                "mode_context": {
                    "required_use_cli_steps": ["use_cli_repo_final_review"],
                    "blocking_clarification_open": False,
                },
            },
        )
        store.append_event(run, {"event_type": "unit_end", "unit_id": "use_cli_repo_final_review"})
        return []
    if case_id == "manager_plan":
        legacy = _create_manager_plan(session.workdir, task_count=2)
        store.save_state(
            run,
            {
                "phase": "plan",
                "mode_context": {
                    "decompose_payload_valid": True,
                    "dynamic_validation_passed": True,
                },
            },
        )
        store.save_plan(
            run,
            {
                "units": [{"id": "TASK-1"}, {"id": "TASK-2"}],
                "legacy_plan_sync": {"synced": True, "task_count": len(legacy.tasks)},
            },
        )
        return ["develop"]
    if case_id == "manager_develop":
        _create_manager_plan(session.workdir, task_count=1)
        store.save_state(
            run,
            {
                "phase": "develop",
                "mode_context": {"dev_report": "Developer report", "task_status_consistent": True},
            },
        )
        return ["review"]
    if case_id == "manager_review":
        store.save_state(
            run,
            {
                "phase": "review",
                "mode_context": {"review_payload_valid": True, "review_decision_outcome": "approved"},
            },
        )
        return ["develop", "complete"]
    if case_id == "manager_complete":
        legacy = _create_manager_plan(session.workdir, task_count=1, status="completed", completion_report="completed")
        store.save_state(
            run,
            {
                "phase": "complete",
                "mode_context": {
                    "final_report": "Manager final report",
                    "legacy_plan_sync_status": "synced",
                },
            },
        )
        store.save_plan(run, {"legacy_plan_sync": {"synced": True, "task_count": len(legacy.tasks)}})
        return []
    if case_id == "webmaster_intent":
        key = _save_webmaster_context(session)
        store.save_state(
            run,
            {
                "phase": "intent",
                "mode_context": {
                    "intent_payload": {"goal": "Fix layout", "task_kind": "new_task"},
                    "webmaster_user_key": key,
                },
            },
        )
        return ["dev"]
    if case_id == "webmaster_dev":
        store.save_state(run, {"phase": "dev", "mode_context": {"developer_report": "patch report"}})
        return ["validation"]
    if case_id == "webmaster_validation":
        store.save_state(
            run,
            {
                "phase": "validation",
                "mode_context": {
                    "developer_report": _developer_report_with_checklist(),
                    "validation_report": {
                        "checklist_rows": [
                            {"item": "html", "status": "PASS", "evidence": "checked"},
                        ]
                    },
                },
            },
        )
        return ["dev", "complete"]
    if case_id == "webmaster_complete":
        store.save_state(
            run,
            {
                "phase": "complete",
                "mode_context": {"structured_report": {"summary": "done", "status": "PASS"}},
            },
        )
        return []
    raise AssertionError(f"Unhandled positive case: {case_id}")


def _configure_negative_case(case_id: str, store: RunArtifactStore, session, run) -> str:
    if case_id == "analyst_intent":
        store.save_state(
            run,
            {
                "phase": "intent",
                "mode_context": {"intent_payload": {"template_id": "default"}},
            },
        )
        return "analyst_context_missing"
    if case_id == "analyst_plan":
        store.save_plan(run, {"units": []})
        return "missing_plan_field"
    if case_id == "analyst_execute":
        return "analyst_execution_evidence_missing"
    if case_id == "analyst_complete":
        store.save_state(run, {"phase": "complete", "status": "running", "mode_context": {}})
        return "analyst_state_not_completed"
    if case_id == "agent_plan":
        store.save_state(
            run,
            {
                "phase": "plan",
                "mode_context": {"required_use_cli_steps": ["use_cli_repo_final_review"]},
            },
        )
        store.save_plan(run, {"units": [{"id": "plain_step"}]})
        return "agent_required_use_cli_steps_missing"
    if case_id == "agent_execute":
        store.save_state(
            run,
            {
                "phase": "execute",
                "mode_context": {"required_use_cli_steps": ["use_cli_repo_final_review"]},
            },
        )
        return "agent_execution_evidence_missing"
    if case_id == "agent_complete":
        store.save_state(
            run,
            {
                "phase": "complete",
                "mode_context": {
                    "required_use_cli_steps": ["use_cli_repo_final_review"],
                    "blocking_clarification_open": True,
                },
            },
        )
        return "agent_blocking_clarification_open"
    if case_id == "manager_plan":
        _create_manager_plan(session.workdir, task_count=1)
        store.save_state(
            run,
            {
                "phase": "plan",
                "mode_context": {
                    "decompose_payload_valid": True,
                    "dynamic_validation_passed": False,
                },
            },
        )
        store.save_plan(run, {"legacy_plan_sync": {"synced": True, "task_count": 1}})
        return "manager_plan_flag_invalid"
    if case_id == "manager_develop":
        _create_manager_plan(session.workdir, task_count=1)
        store.save_state(run, {"phase": "develop", "mode_context": {}})
        return "missing_state_field"
    if case_id == "manager_review":
        store.save_state(
            run,
            {
                "phase": "review",
                "mode_context": {"review_payload_valid": False, "review_decision_outcome": ""},
            },
        )
        return "manager_review_payload_invalid"
    if case_id == "manager_complete":
        store.save_state(
            run,
            {
                "phase": "complete",
                "mode_context": {"final_report": "report"},
            },
        )
        return "manager_legacy_plan_missing"
    if case_id == "webmaster_intent":
        store.save_state(
            run,
            {
                "phase": "intent",
                "mode_context": {"intent_payload": {"goal": "Fix layout"}},
            },
        )
        return "webmaster_context_missing"
    if case_id == "webmaster_dev":
        store.save_state(run, {"phase": "dev", "mode_context": {}})
        return "missing_state_field"
    if case_id == "webmaster_validation":
        store.save_state(
            run,
            {
                "phase": "validation",
                "mode_context": {
                    "developer_report": "report without checklist table",
                    "validation_report": {
                        "checklist_rows": [
                            {"item": "html", "status": "PARTIAL", "evidence": ""},
                        ]
                    },
                },
            },
        )
        return "webmaster_checklist_table_missing"
    if case_id == "webmaster_complete":
        store.save_state(run, {"phase": "complete", "mode_context": {}})
        return "missing_state_field"
    raise AssertionError(f"Unhandled negative case: {case_id}")


POSITIVE_CASES = [
    ("analyst", "intent", "analyst_intent"),
    ("analyst", "plan", "analyst_plan"),
    ("analyst", "execute", "analyst_execute"),
    ("analyst", "complete", "analyst_complete"),
    ("agent", "plan", "agent_plan"),
    ("agent", "execute", "agent_execute"),
    ("agent", "complete", "agent_complete"),
    ("manager", "plan", "manager_plan"),
    ("manager", "develop", "manager_develop"),
    ("manager", "review", "manager_review"),
    ("manager", "complete", "manager_complete"),
    ("webmaster", "intent", "webmaster_intent"),
    ("webmaster", "dev", "webmaster_dev"),
    ("webmaster", "validation", "webmaster_validation"),
    ("webmaster", "complete", "webmaster_complete"),
]


NEGATIVE_CASES = [
    ("analyst", "intent", "analyst_intent"),
    ("analyst", "plan", "analyst_plan"),
    ("analyst", "execute", "analyst_execute"),
    ("analyst", "complete", "analyst_complete"),
    ("agent", "plan", "agent_plan"),
    ("agent", "execute", "agent_execute"),
    ("agent", "complete", "agent_complete"),
    ("manager", "plan", "manager_plan"),
    ("manager", "develop", "manager_develop"),
    ("manager", "review", "manager_review"),
    ("manager", "complete", "manager_complete"),
    ("webmaster", "intent", "webmaster_intent"),
    ("webmaster", "dev", "webmaster_dev"),
    ("webmaster", "validation", "webmaster_validation"),
    ("webmaster", "complete", "webmaster_complete"),
]


@pytest.mark.parametrize(
    ("mode_id", "phase", "case_id"),
    POSITIVE_CASES,
    ids=[f"{mode_id}:{phase}:ok" for mode_id, phase, _case_id in POSITIVE_CASES],
)
def test_run_boundary_validation_positive_transition_matrix(tmp_path, mode_id: str, phase: str, case_id: str) -> None:
    _cfg, store, service, session, run = _prepare_run(tmp_path, case_id=f"pos_{case_id}", mode_id=mode_id, phase=phase)

    expected_next = _configure_positive_case(case_id, store, session, run)
    report = service.validate(run, mode_id=mode_id, phase=phase)

    assert service.contract_for(mode_id, phase) is not None
    assert report.status == "ok"
    assert report.issues == []
    assert report.next_allowed_phases == expected_next
    assert report.to_dict()["status"] == "ok"


@pytest.mark.parametrize(
    ("mode_id", "phase", "case_id"),
    NEGATIVE_CASES,
    ids=[f"{mode_id}:{phase}:error" for mode_id, phase, _case_id in NEGATIVE_CASES],
)
def test_run_boundary_validation_negative_transition_matrix(tmp_path, mode_id: str, phase: str, case_id: str) -> None:
    _cfg, store, service, session, run = _prepare_run(tmp_path, case_id=f"neg_{case_id}", mode_id=mode_id, phase=phase)

    expected_code = _configure_negative_case(case_id, store, session, run)
    report = service.validate(run, mode_id=mode_id, phase=phase)

    assert report.status == "error"
    assert report.next_allowed_phases == []
    assert any(issue.code == expected_code for issue in report.issues)
    assert report.to_dict()["issues"]


def test_run_boundary_validation_allows_analyst_complete_with_last_draft_only(tmp_path) -> None:
    _cfg, store, service, _session, run = _prepare_run(
        tmp_path,
        case_id="analyst_complete_last_draft",
        mode_id="analyst",
        phase="complete",
    )
    store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "final_deliverable": "",
                "last_draft": "Черновик без финализации",
            },
        },
    )
    _save_analyst_quality(store, run, runtime_verdict="Готово к реализации")

    report = service.validate(run, mode_id="analyst", phase="complete")

    assert report.status == "ok"
    assert report.issues == []


def test_run_boundary_validation_allows_analyst_complete_with_not_ready_final_deliverable(tmp_path) -> None:
    _cfg, store, service, _session, run = _prepare_run(
        tmp_path,
        case_id="analyst_complete_not_ready_deliverable",
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
    _save_analyst_quality(
        store,
        run,
        runtime_verdict="Не готово к реализации",
        blocking_reasons=["Незакрытые blocking obligations: 1"],
    )

    report = service.validate(run, mode_id="analyst", phase="complete")

    assert report.status == "error"
    assert any(issue.code == "analyst_quality_gate_not_passed" for issue in report.issues)


def test_run_boundary_validation_agent_execute_distinguishes_missing_event_evidence_from_malformed_events(tmp_path) -> None:
    _cfg, store, service, _session, run = _prepare_run(
        tmp_path,
        case_id="agent_execute_events_distinction",
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
    store.save_metrics(run, {"units": [{"unit_id": "other-step"}]})
    store.append_event(run, {"event_type": "unit_end", "unit_id": "other-step"})

    healthy_report = service.validate(run, mode_id="agent", phase="execute")

    assert healthy_report.status == "error"
    assert any(issue.code == "agent_use_cli_events_missing" for issue in healthy_report.issues)
    assert all(issue.code != "events_malformed" for issue in healthy_report.issues)

    Path(run.events_path).write_text("{not-json}\n", encoding="utf-8")

    malformed_report = service.validate(run, mode_id="agent", phase="execute")

    assert malformed_report.status == "error"
    assert any(issue.code == "events_malformed" for issue in malformed_report.issues)
    assert all(issue.code != "agent_use_cli_events_missing" for issue in malformed_report.issues)


def test_run_boundary_validation_manager_complete_requires_synced_manager_plan_json(tmp_path) -> None:
    _cfg, store, service, session, run = _prepare_run(
        tmp_path,
        case_id="manager_complete_no_sync",
        mode_id="manager",
        phase="complete",
    )
    store.save_state(
        run,
        {
            "phase": "complete",
            "mode_context": {"final_report": "Final report without legacy sync"},
        },
    )
    store.save_plan(run, {"legacy_plan_sync": {"synced": False, "task_count": 0}})

    report = service.validate(run, mode_id="manager", phase="complete")

    assert report.status == "error"
    assert any(issue.code in {"manager_legacy_plan_missing", "manager_legacy_plan_unsynced"} for issue in report.issues)


def test_run_boundary_validation_manager_recovery_complete_uses_snapshot_not_live_manager_plan(tmp_path) -> None:
    _cfg, store, service, session, run = _prepare_run(
        tmp_path,
        case_id="manager_recovery_snapshot_ok",
        mode_id="manager",
        phase="complete",
    )
    live_plan = _create_manager_plan(session.workdir, task_count=1, status="failed")
    live_plan.current_task_id = "TASK-LIVE"
    live_plan.completion_report = ""
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
                    "source_run_id": "run_20260312T165500Z_source0001",
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
                    "source_run_id": "run_20260312T165500Z_source0001",
                    "plan_snapshot": dict(replay_snapshot),
                }
            },
        },
    )

    report = service.validate(run, mode_id="manager", phase="complete")

    assert report.status == "ok"
    assert report.issues == []


@pytest.mark.parametrize(
    ("case_suffix", "sync_overrides", "expected_field"),
    [
        ("task_count", {"task_count": 2}, "task_count"),
        ("legacy_status", {"legacy_status": "active"}, "legacy_status"),
        ("current_task_id", {"current_task_id": "TASK-2"}, "current_task_id"),
        ("completion_report", {"completion_report_present": False}, "completion_report_present"),
        ("updated_at", {"legacy_updated_at": "2030-01-01 00:05:00"}, "legacy_updated_at"),
        ("updated_at_missing", {"legacy_updated_at": ""}, "legacy_updated_at"),
    ],
    ids=[
        "task_count_mismatch",
        "legacy_status_mismatch",
        "current_task_id_mismatch",
        "completion_report_presence_mismatch",
        "legacy_updated_at_mismatch",
        "legacy_updated_at_missing",
    ],
)
def test_run_boundary_validation_manager_recovery_complete_snapshot_parity_checks_all_required_fields(
    tmp_path,
    case_suffix: str,
    sync_overrides: dict,
    expected_field: str,
) -> None:
    _cfg, store, boundary, session, run = _prepare_run(
        tmp_path,
        case_id=f"manager_recovery_snapshot_{case_suffix}",
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
                    "source_run_id": f"run_20260312T165600Z_{case_suffix}",
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
                    "source_run_id": f"run_20260312T165600Z_{case_suffix}",
                    "plan_snapshot": dict(replay_snapshot),
                }
            },
        },
    )

    boundary_report = boundary.validate(run, mode_id="manager", phase="complete")
    conflict_fields = {
        issue.details.get("field")
        for issue in boundary_report.issues
        if issue.code == "manager_legacy_plan_conflict"
    }

    assert boundary_report.status == "error"
    assert conflict_fields == {expected_field}


def test_run_boundary_validation_manager_recovery_complete_flags_state_echo_mismatch(tmp_path) -> None:
    _cfg, store, boundary, session, run = _prepare_run(
        tmp_path,
        case_id="manager_recovery_snapshot_state_echo",
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
                    "source_run_id": "run_20260312T165650Z_stateecho",
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
                    "source_run_id": "run_20260312T165650Z_stateecho",
                    "plan_snapshot": dict(replay_snapshot),
                }
            },
        },
    )

    boundary_report = boundary.validate(run, mode_id="manager", phase="complete")

    assert boundary_report.status == "error"
    assert any(
        issue.code == "manager_legacy_plan_conflict" and issue.details.get("field") == "state_plan_echo"
        for issue in boundary_report.issues
    )


def test_run_boundary_validation_admin_analyze_enforces_snapshot_fidelity(tmp_path) -> None:
    _cfg, store, service, _session, run = _prepare_run(
        tmp_path,
        case_id="admin_analyze_fidelity",
        mode_id="admin",
        phase="analyze",
    )
    snapshot_id = "snapshot:admin-analyze"
    snapshot_ids = ["srv-1:local:scan_local:1710000000250"]
    base_state = {
        "phase": "analyze",
        "status": "running",
        "mode_context": {
            "operation_payload": {"kind": "watch_loop", "chat_id": "1"},
            "target_transport": "local",
            "snapshot_id": snapshot_id,
            "snapshot_ids": list(snapshot_ids),
            "snapshot_fidelity": {
                "snapshot_id": snapshot_id,
                "snapshot_ids": list(snapshot_ids),
                "server_count": 1,
                "total_servers": 1,
                "ok_servers": 1,
                "failed_servers": 0,
                "verified_post_analyze": True,
            },
            "last_monitor_snapshot": {
                "server_count": 1,
                "total_servers": 1,
                "ok_servers": 1,
                "failed_servers": 0,
            },
            "last_analyzer_decision": {"action": "notify_admin", "confidence": "high"},
        },
    }
    store.save_plan(run, {"units": [{"id": "admin:watch_loop"}]})
    store.append_checkpoint(run, {"phase": "analyze", "status": "ok"})
    store.save_state(run, base_state)

    ok_report = service.validate(run, mode_id="admin", phase="analyze")

    broken_state = dict(base_state)
    broken_mode_context = dict(base_state["mode_context"])
    broken_fidelity = dict(broken_mode_context["snapshot_fidelity"])
    broken_fidelity["snapshot_id"] = "snapshot:wrong"
    broken_mode_context["snapshot_fidelity"] = broken_fidelity
    broken_state["mode_context"] = broken_mode_context
    store.save_state(run, broken_state)

    error_report = service.validate(run, mode_id="admin", phase="analyze")

    assert ok_report.status == "ok"
    assert error_report.status == "error"
    assert any(issue.code == "admin_snapshot_fidelity_mismatch" for issue in error_report.issues)


def test_run_boundary_validation_admin_analyze_compares_long_snapshot_ids_symmetrically(tmp_path) -> None:
    _cfg, store, service, _session, run = _prepare_run(
        tmp_path,
        case_id="admin_analyze_long_fidelity",
        mode_id="admin",
        phase="analyze",
    )
    snapshot_id = "snapshot:admin-analyze-long"
    long_snapshot_id = "scan:docker_container:" + ("very-long-container-name-" * 8)
    store.save_plan(run, {"units": [{"id": "admin:watch_loop"}]})
    store.append_checkpoint(run, {"phase": "analyze", "status": "ok"})
    store.save_state(
        run,
        {
            "phase": "analyze",
            "status": "running",
            "mode_context": {
                "operation_payload": {"kind": "watch_loop", "chat_id": "1"},
                "target_transport": "ssh",
                "snapshot_id": snapshot_id,
                "snapshot_ids": [long_snapshot_id],
                "snapshot_fidelity": {
                    "snapshot_id": snapshot_id,
                    "snapshot_ids": [long_snapshot_id],
                    "server_count": 1,
                    "total_servers": 1,
                    "ok_servers": 1,
                    "failed_servers": 0,
                    "verified_post_analyze": True,
                },
                "last_monitor_snapshot": {
                    "server_count": 1,
                    "total_servers": 1,
                    "ok_servers": 1,
                    "failed_servers": 0,
                },
                "last_analyzer_decision": {"action": "notify_admin", "confidence": "medium"},
            },
        },
    )

    report = service.validate(run, mode_id="admin", phase="analyze")

    assert report.status == "ok"


def test_run_boundary_validation_admin_complete_requires_native_bypass_for_manual_run(tmp_path) -> None:
    _cfg, store, service, _session, run = _prepare_run(
        tmp_path,
        case_id="admin_complete_manual_run",
        mode_id="admin",
        phase="complete",
    )
    store.save_plan(run, {"units": [{"id": "admin:manual_run"}]})
    store.append_checkpoint(run, {"phase": "complete", "status": "started"})
    store.save_state(
        run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "operation_payload": {
                    "kind": "manual_run",
                    "action_id": "safe_action",
                    "server_id": "srv-1",
                    "target_transport": "ssh",
                },
                "target_transport": "ssh",
                "execution_context": {
                    "native_transport_execution": True,
                    "destructive_execution": True,
                    "dry_run": False,
                    "check_only": False,
                    "action_id": "safe_action",
                    "server_id": "srv-1",
                },
            },
        },
    )

    report = service.validate(run, mode_id="admin", phase="complete")

    assert report.status == "error"
    assert any(issue.code == "admin_skill_selector_bypass_missing" for issue in report.issues)


def test_run_boundary_validation_webmaster_gate_payload_degradation_is_reported_without_traceback(tmp_path) -> None:
    _cfg, store, service, session, run = _prepare_run(
        tmp_path,
        case_id="webmaster_gate_payload_degraded",
        mode_id="webmaster",
        phase="validation",
    )
    key = _save_webmaster_context(session, goal="Fix layout", last_cli_report="checked")
    store.save_state(
        run,
        {
            "phase": "validation",
            "mode_context": {
                "webmaster_user_key": key,
                "developer_report": _developer_report_with_checklist(),
                "validation_report": {
                    "status": "FAIL",
                    "checklist_rows": [{"item": "html", "status": "PASS", "evidence": "checked"}],
                    "gate": {
                        "passed": False,
                        "checklist_table_present": True,
                        "invalid_rows": ["broken-row", {"item": "markup", "status": "FAIL"}],
                        "non_pass_rows": ["bad-status", {"item": "styles", "status": "FAIL"}],
                        "missing_evidence_rows": [123, {"item": "scripts"}],
                        "blocking_issue_count": 2,
                    },
                },
            },
        },
    )

    report = service.validate(run, mode_id="webmaster", phase="validation")

    issue_codes = [item.code for item in report.issues]

    assert report.status == "error"
    assert "webmaster_gate_payload_degraded" in issue_codes
    assert "webmaster_checklist_row_invalid" in issue_codes
    assert "webmaster_gate_failed" in issue_codes
    assert "webmaster_checklist_evidence_missing" in issue_codes
    degraded_rows = [item.details for item in report.issues if item.code == "webmaster_gate_payload_degraded"]
    assert {"field": "invalid_rows", "row_index": 0, "actual_type": "str"} in degraded_rows
    assert {"field": "non_pass_rows", "row_index": 0, "actual_type": "str"} in degraded_rows
    assert {"field": "missing_evidence_rows", "row_index": 0, "actual_type": "int"} in degraded_rows


def test_run_boundary_validation_converts_custom_validator_exception_to_issue(tmp_path, monkeypatch) -> None:
    _cfg, store, service, session, run = _prepare_run(
        tmp_path,
        case_id="webmaster_validator_exception",
        mode_id="webmaster",
        phase="intent",
    )
    key = _save_webmaster_context(session, goal="Fix layout")
    store.save_state(
        run,
        {
            "phase": "intent",
            "mode_context": {
                "intent_payload": {"goal": "Fix layout", "task_kind": "new_task"},
                "webmaster_user_key": key,
            },
        },
    )

    def _boom(_run, _docs, _issues) -> None:
        raise RuntimeError("validator exploded")

    monkeypatch.setattr(service, "_validate_webmaster_intent", _boom)

    report = service.validate(run, mode_id="webmaster", phase="intent")

    assert report.status == "error"
    assert any(issue.code == "boundary_validator_exception" for issue in report.issues)
    assert any(issue.details == {"validator": "webmaster_intent", "error_type": "RuntimeError"} for issue in report.issues)


def test_run_boundary_validation_isolates_sequential_runs_with_different_intent(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="boundary_isolation")
    store = RunArtifactStore(cfg)
    service = RunBoundaryValidationService(enabled=True)
    session_a = _session(tmp_path, case_id="intent-a", session_uid="thread:-100:intent-a", session_id="s-a")
    session_b = _session(tmp_path, case_id="intent-b", session_uid="thread:-100:intent-b", session_id="s-b")
    run_a = store.start_run(session=session_a, mode_id="analyst", run_id="run_20260312T171000Z_aaaabbbb")
    run_b = store.start_run(session=session_b, mode_id="analyst", run_id="run_20260312T171100Z_ccccdddd")

    store.save_plan(run_a, {"units": [{"id": "unit:a"}]})
    store.save_plan(run_b, {"units": []})

    report_a = service.validate(run_a, mode_id="analyst", phase="plan")
    report_b = service.validate(run_b, mode_id="analyst", phase="plan")

    assert report_a.status == "ok"
    assert report_b.status == "error"
    assert report_a.issues == []
    assert any(issue.code == "missing_plan_field" for issue in report_b.issues)


