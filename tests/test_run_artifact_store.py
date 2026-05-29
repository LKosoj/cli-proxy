from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.run_artifact_store import RunArtifactStore, is_terminal_status
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig


def _build_config(tmp_path: Path, *, intent: str = "default", retention_days: int = 30) -> AppConfig:
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
            run_artifacts_retention_days=retention_days,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def _session(tmp_path: Path, *, workdir: str | None = None, session_uid: str = "thread:-100:501", session_id: str = "s1"):
    resolved_workdir = workdir if workdir is not None else str(tmp_path / "session-workdir")
    if resolved_workdir:
        Path(resolved_workdir).mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        id=session_id,
        workdir=resolved_workdir,
        conversation_scope=SimpleNamespace(session_uid=session_uid),
    )


@pytest.mark.parametrize("status", sorted(RunArtifactStore.TERMINAL_STATUSES))
def test_is_terminal_status_accepts_every_terminal_status(status: str) -> None:
    assert is_terminal_status(status) is True
    assert is_terminal_status(f" {status.upper()} ") is True


@pytest.mark.parametrize("status", ["", "active", "paused", "running", "needs_recovery", None])
def test_is_terminal_status_rejects_non_terminal_statuses(status: object) -> None:
    assert is_terminal_status(status) is False


def test_run_artifact_store_start_run_creates_required_documents(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="start")
    store = RunArtifactStore(cfg)
    session = _session(tmp_path, session_uid="thread:-100777:501", session_id="legacy-s1")

    run = store.start_run(session=session, mode_id="manager")

    assert run.run_dir == str(
        Path(session.workdir) / ".cli-proxy" / "runs" / "thread:-100777:501" / "manager" / run.run_id
    )
    assert Path(run.state_path).exists()
    assert Path(run.checkpoints_path).exists()
    assert Path(run.metrics_path).exists()
    assert Path(run.events_path).exists()
    assert Path(run.artifacts_dir).is_dir()
    assert Path(run.scratch_dir).is_dir()

    state = json.loads(Path(run.state_path).read_text(encoding="utf-8"))
    checkpoints = json.loads(Path(run.checkpoints_path).read_text(encoding="utf-8"))
    metrics = json.loads(Path(run.metrics_path).read_text(encoding="utf-8"))
    recovery = json.loads(Path(run.recovery_path).read_text(encoding="utf-8"))
    assert state["run_id"] == run.run_id
    assert state["session_uid"] == "thread:-100777:501"
    assert state["mode_id"] == "manager"
    assert state["status"] == "running"
    assert checkpoints == {"version": 1, "run_id": run.run_id, "items": []}
    assert metrics["version"] == 1
    assert metrics["run_id"] == run.run_id
    assert metrics["units"] == []
    assert metrics["phase_aggregates"] == []
    assert recovery["can_resume"] is False


def test_run_artifact_store_contract_covers_start_save_load_append_latest_and_finish(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="contract")
    store = RunArtifactStore(cfg)
    session = _session(tmp_path, session_uid="thread:-100777:777", session_id="s-contract")

    first = store.start_run(session=session, mode_id="analyst", run_id="run_20260312T120000Z_aaaa1111")
    second = store.start_run(session=session, mode_id="analyst", run_id="run_20260312T120100Z_bbbb2222")

    saved_state = store.save_state(
        second,
        {
            "phase": "execute",
            "current_unit_id": "task:REQ-1",
            "current_step_id": "step:1",
            "checkpoint_index": 1,
            "mode_context": {"template_id": "default"},
        },
    )
    saved_plan = store.save_plan(second, {"task_family": "deep_research", "units": [{"id": "task:REQ-1"}]})
    checkpoint = store.append_checkpoint(
        second,
        {
            "phase": "plan",
            "unit_id": "plan:root",
            "status": "passed",
            "summary": "validated",
            "artifacts": ["artifacts/plan.json"],
        },
    )
    recovery = store.save_recovery(
        second,
        {
            "status": "needs_recovery",
            "recommended_action": "rollback_to_checkpoint",
            "last_consistent_checkpoint": 1,
            "issues": [{"code": "state_mismatch"}],
        },
    )
    metrics = store.save_metrics(
        second,
        {
            "totals": {
                "units": 1,
                "duration_sec": 3.5,
                "retries": 1,
                "recovery_attempts": 0,
                "tool_calls": 2,
                "input_tokens": 10,
                "output_tokens": 20,
                "cost_usd": 0.01,
            },
            "units": [{"unit_id": "task:REQ-1"}],
            "phase_aggregates": [{"phase": "execute", "duration_sec": 3.5}],
        },
    )
    event = store.append_event(second, {"event_type": "phase_changed", "phase": "execute"})
    finished = store.mark_finished(second, status="completed", phase="complete")
    latest = store.latest_run(session=session, mode_id="analyst")

    assert first.run_id == "run_20260312T120000Z_aaaa1111"
    assert saved_state["phase"] == "execute"
    assert store.load_state(second)["current_unit_id"] == "task:REQ-1"
    assert saved_plan["units"] == [{"id": "task:REQ-1"}]
    assert checkpoint["index"] == 1
    checkpoints_doc = json.loads(Path(second.checkpoints_path).read_text(encoding="utf-8"))
    assert checkpoints_doc["items"] == [checkpoint]
    assert recovery["recommended_action"] == "rollback_to_checkpoint"
    assert metrics["totals"]["tool_calls"] == 2
    assert event["event_type"] == "phase_changed"
    events_lines = Path(second.events_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(events_lines) == 1
    assert json.loads(events_lines[0])["phase"] == "execute"
    assert finished["status"] == "completed"
    assert finished["phase"] == "complete"
    assert finished["finished_at"] is not None
    assert latest == second


def test_run_artifact_store_uses_workdir_and_session_uid_fallbacks_for_fake_session(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="fallback")
    store = RunArtifactStore(cfg)
    session = SimpleNamespace(
        id="legacy-session",
        workdir="",
        conversation_scope=SimpleNamespace(session_uid=""),
    )

    run = store.start_run(session=session, mode_id="webmaster", run_id="run_20260312T130000Z_deadbeef")

    assert run.session_uid == "desktop:legacy-session"
    assert run.run_dir == str(
        Path(cfg.defaults.workdir)
        / ".cli-proxy"
        / "runs"
        / "desktop:legacy-session"
        / "webmaster"
        / "run_20260312T130000Z_deadbeef"
    )
    assert Path(run.state_path).exists()


def test_run_artifact_store_save_recovery_merges_existing_audit_fields(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="recovery_merge")
    store = RunArtifactStore(cfg)
    session = _session(
        tmp_path,
        session_uid="thread:-100:recovery-merge",
        session_id="s-recovery-merge",
    )
    run = store.start_run(session=session, mode_id="analyst", run_id="run_20260314T090000Z_merge000")

    initial = store.save_recovery(
        run,
        {
            "status": "needs_recovery",
            "recommended_action": "rollback_to_checkpoint",
            "last_requested_operation": {
                "operation": "recover",
                "status": "executed",
                "requested_at": 11.0,
                "executed_at": 12.0,
            },
            "requested_operations": [
                {
                    "operation": "recover",
                    "status": "executed",
                    "requested_at": 11.0,
                }
            ],
            "attempts": [
                {
                    "diagnosed_at": 10.0,
                    "mode_id": "analyst",
                    "phase": "plan",
                    "recommended_action": "rollback_to_checkpoint",
                    "issues": [{"code": "missing_plan"}],
                }
            ],
        },
    )

    merged = store.save_recovery(
        run,
        {
            "status": "ok",
            "diagnosed_at": 20.0,
            "recommended_action": "no_action",
            "last_requested_operation": {"status": "executed"},
        },
    )

    assert initial["last_requested_operation"]["requested_at"] == 11.0
    assert merged["status"] == "ok"
    assert merged["diagnosed_at"] == 20.0
    assert merged["recommended_action"] == "no_action"
    assert merged["last_requested_operation"]["operation"] == "recover"
    assert merged["last_requested_operation"]["requested_at"] == 11.0
    assert merged["last_requested_operation"]["executed_at"] == 12.0
    assert merged["requested_operations"][0]["operation"] == "recover"
    assert merged["requested_operations"][0]["requested_at"] == 11.0
    assert merged["attempts"][0]["diagnosed_at"] == 10.0


def test_run_artifact_store_load_state_survives_malformed_json(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="malformed")
    store = RunArtifactStore(cfg)
    session = _session(tmp_path, session_uid="thread:-100777:broken", session_id="broken-state")
    run = store.start_run(session=session, mode_id="manager", run_id="run_20260312T140000Z_feedface")
    Path(run.state_path).write_text("{broken", encoding="utf-8")

    state = store.load_state(run)

    assert state["run_id"] == "run_20260312T140000Z_feedface"
    assert state["session_uid"] == "thread:-100777:broken"
    assert state["mode_id"] == "manager"
    assert state["status"] == "running"


def test_run_artifact_store_isolates_sequential_runs_with_different_intent(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="isolation")
    store = RunArtifactStore(cfg)
    session_a = _session(tmp_path, workdir=str(tmp_path / "intent-a"), session_uid="thread:-100:intent-a", session_id="s-a")
    session_b = _session(tmp_path, workdir=str(tmp_path / "intent-b"), session_uid="thread:-100:intent-b", session_id="s-b")

    run_a = store.start_run(session=session_a, mode_id="manager", run_id="run_20260312T150000Z_aaaabbbb")
    run_b = store.start_run(session=session_b, mode_id="manager", run_id="run_20260312T150100Z_ccccdddd")

    latest_a = store.latest_run(session=session_a, mode_id="manager")
    latest_b = store.latest_run(session=session_b, mode_id="manager")

    assert latest_a == run_a
    assert latest_b == run_b
    assert latest_a is not latest_b
    assert Path(run_a.run_dir).parent.parent.parent != Path(run_b.run_dir).parent.parent.parent
    assert store.load_state(run_a)["session_uid"] == "thread:-100:intent-a"
    assert store.load_state(run_b)["session_uid"] == "thread:-100:intent-b"


def test_run_artifact_store_append_event_fsyncs_events_jsonl(tmp_path, monkeypatch) -> None:
    cfg = _build_config(tmp_path, intent="fsync")
    store = RunArtifactStore(cfg)
    session = _session(tmp_path, session_uid="thread:-100:fsync", session_id="s-fsync")
    run = store.start_run(session=session, mode_id="agent")
    calls: list[int] = []

    def _fsync(fd: int) -> None:
        calls.append(int(fd))

    monkeypatch.setattr("app.services.run_artifact_store.os.fsync", _fsync)

    store.append_event(run, {"event_type": "unit_started"})

    assert len(calls) == 1


def _set_tree_mtime(path: Path, ts: float) -> None:
    for current_root, dir_names, file_names in os.walk(path):
        for name in file_names:
            os.utime(Path(current_root) / name, (ts, ts))
        for name in dir_names:
            os.utime(Path(current_root) / name, (ts, ts))
    os.utime(path, (ts, ts))


def test_run_artifact_store_prune_old_runs_deletes_expired_finished_runs(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="prune", retention_days=7)
    store = RunArtifactStore(cfg)
    session = _session(tmp_path, session_uid="thread:-100:prune", session_id="s-prune")
    now_ts = 1_773_600_000.0

    expired = store.start_run(session=session, mode_id="manager", run_id="run_20260301T000000Z_expired")
    store.mark_finished(expired, status="completed", phase="complete")
    fresh = store.start_run(session=session, mode_id="manager", run_id="run_20260310T000000Z_fresh")
    store.mark_finished(fresh, status="completed", phase="complete")
    running = store.start_run(session=session, mode_id="manager", run_id="run_20260302T000000Z_running")

    _set_tree_mtime(Path(expired.run_dir), now_ts - (9 * 86400.0))
    _set_tree_mtime(Path(fresh.run_dir), now_ts - (2 * 86400.0))
    _set_tree_mtime(Path(running.run_dir), now_ts - (9 * 86400.0))

    report = store.prune_old_runs(session=session, dry_run=False, now_ts=now_ts)

    assert report["retention_days"] == 7
    assert Path(expired.run_dir).exists() is False
    assert Path(fresh.run_dir).exists() is True
    assert Path(running.run_dir).exists() is True
    assert [item["run_id"] for item in report["deleted"]] == ["run_20260301T000000Z_expired"]
    assert [item["run_id"] for item in report["shielded"]] == ["run_20260302T000000Z_running"]


def test_run_artifact_store_prune_old_runs_dry_run_reports_without_deleting(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="prune_dry", retention_days=3)
    store = RunArtifactStore(cfg)
    session = _session(tmp_path, session_uid="thread:-100:prune-dry", session_id="s-prune-dry")
    now_ts = 1_773_700_000.0

    expired = store.start_run(session=session, mode_id="analyst", run_id="run_20260301T000000Z_expired")
    store.mark_finished(expired, status="failed", phase="complete")
    running = store.start_run(session=session, mode_id="analyst", run_id="run_20260301T000500Z_running")

    _set_tree_mtime(Path(expired.run_dir), now_ts - (10 * 86400.0))
    _set_tree_mtime(Path(running.run_dir), now_ts - (10 * 86400.0))

    report = store.prune_old_runs(session=session, dry_run=True, now_ts=now_ts)

    assert Path(expired.run_dir).exists() is True
    assert Path(running.run_dir).exists() is True
    assert [item["run_id"] for item in report["would_delete"]] == ["run_20260301T000000Z_expired"]
    assert [item["run_id"] for item in report["shielded"]] == ["run_20260301T000500Z_running"]
    assert report["deleted"] == []
