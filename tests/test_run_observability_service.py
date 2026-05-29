from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.services.metrics_service import Metrics
from app.services.run_artifact_store import RunArtifactStore
from app.services.run_observability_service import RunObservabilityService
from app.services.runtime_progress_service import clear_runtime_progress, emit_runtime_progress
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import Session


def _build_config(tmp_path: Path, *, intent: str = "default") -> AppConfig:
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


def _artifact_session(tmp_path: Path, *, name: str, session_uid: str, session_id: str):
    workdir = tmp_path / name
    workdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        id=session_id,
        workdir=str(workdir),
        conversation_scope=SimpleNamespace(session_uid=session_uid),
    )


def _runtime_session(tmp_path: Path, *, intent: str) -> Session:
    cfg = _build_config(tmp_path, intent=intent)
    tool = cfg.tools["dummy"]
    return Session(
        id=f"session-{intent}",
        tool=tool,
        workdir=cfg.defaults.workdir,
        idle_timeout_sec=10,
        config=cfg,
        chat_id=777,
    )


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_run_observability_aggregates_unit_totals_with_null_fallbacks(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="aggregate")
    store = RunArtifactStore(cfg)
    service = RunObservabilityService(enabled=True, artifact_store=store)
    session = _artifact_session(
        tmp_path,
        name="aggregate-session",
        session_uid="thread:-100:aggregate",
        session_id="aggregate-session",
    )
    run = store.start_run(session=session, mode_id="manager", run_id="run_20260312T160000Z_aaaabbbb")

    service.record_unit_start(run, unit_id="unit:1", phase="execute", ts=10.0)
    service.record_retry(run, phase="execute", unit_id="unit:1", reason="timeout", ts=11.0)
    service.record_recovery_attempt(run, phase="execute", unit_id="unit:1", action="resume", ts=12.0)
    service.record_unit_end(
        run,
        unit_id="unit:1",
        phase="execute",
        status="ok",
        duration_sec=3.5,
        tool_calls=2,
        ts=13.5,
    )

    metrics = _load_json(run.metrics_path)
    totals = metrics["totals"]
    unit = metrics["units"][0]

    assert totals["units"] == 1
    assert totals["duration_sec"] == 3.5
    assert totals["retries"] == 1
    assert totals["recovery_attempts"] == 1
    assert totals["tool_calls"] == 2
    assert totals["input_tokens"] is None
    assert totals["output_tokens"] is None
    assert totals["cost_usd"] is None
    assert unit["unit_id"] == "unit:1"
    assert unit["retries"] == 1
    assert unit["recovery_attempts"] == 1
    assert unit["input_tokens"] is None
    assert unit["output_tokens"] is None
    assert unit["cost_usd"] is None


def test_run_observability_contract_is_independent_from_process_metrics_service(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="contract")
    store = RunArtifactStore(cfg)
    service = RunObservabilityService(enabled=True, artifact_store=store)
    process_metrics = Metrics()
    process_metrics.inc("messages", 2)
    session = _artifact_session(
        tmp_path,
        name="contract-session",
        session_uid="thread:-100:contract",
        session_id="contract-session",
    )
    run = store.start_run(session=session, mode_id="analyst", run_id="run_20260312T161000Z_bbbbcccc")

    service.record_phase_start(run, phase="plan", corr_id="corr:1", message="phase start", ts=1.0)
    service.record_unit_start(run, unit_id="unit:plan", phase="plan", corr_id="corr:1", ts=2.0)
    service.record_retry(run, phase="plan", unit_id="unit:plan", reason="timeout", ts=3.0)
    service.record_recovery_attempt(run, phase="plan", unit_id="unit:plan", action="resume", ts=4.0)
    service.record_skill_selection(
        run,
        phase="plan",
        unit_id="unit:plan",
        selected_skills=["playwright-cli", "xlsx"],
        reason="browser and spreadsheet",
        ts=5.0,
    )
    service.record_skill_discovery(
        run,
        phase="plan",
        unit_id="unit:plan",
        source="registry:npx-skills",
        discovered_skills=["playwright-cli"],
        query="browser automation",
        ts=6.0,
    )
    service.record_skill_install(
        run,
        phase="plan",
        unit_id="unit:plan",
        skill_id="playwright-cli",
        source="registry:npx-skills",
        target="project-local",
        ts=7.0,
    )
    service.record_unit_end(
        run,
        unit_id="unit:plan",
        phase="plan",
        status="ok",
        duration_sec=4.5,
        tool_calls=4,
        input_tokens=11,
        output_tokens=7,
        cost_usd=0.02,
        ts=8.0,
    )
    service.record_phase_end(
        run,
        phase="plan",
        status="ok",
        duration_sec=5.0,
        tool_calls=4,
        input_tokens=11,
        output_tokens=7,
        cost_usd=0.02,
        ts=9.0,
    )

    metrics = _load_json(run.metrics_path)
    totals = metrics["totals"]
    phase = metrics["phase_aggregates"][0]
    events = [
        json.loads(line)
        for line in Path(run.events_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert [event["event_type"] for event in events] == [
        "phase_start",
        "unit_start",
        "retry",
        "recovery_attempt",
        "skill_selection",
        "skill_discovery",
        "skill_install",
        "unit_end",
        "phase_end",
    ]
    assert totals["units"] == 1
    assert totals["retries"] == 1
    assert totals["recovery_attempts"] == 1
    assert totals["skill_selections"] == 1
    assert totals["skill_discoveries"] == 1
    assert totals["skill_installs"] == 1
    assert totals["tool_calls"] == 4
    assert totals["input_tokens"] == 11
    assert totals["output_tokens"] == 7
    assert totals["cost_usd"] == 0.02
    assert phase["phase"] == "plan"
    assert phase["starts"] == 1
    assert phase["ends"] == 1
    assert phase["duration_sec"] == 5.0
    assert phase["tool_calls"] == 4
    assert phase["input_tokens"] == 11
    assert phase["output_tokens"] == 7
    assert phase["cost_usd"] == 0.02
    assert process_metrics.counters == {"messages": 2}


def test_run_observability_bridge_duplicates_runtime_progress_events(tmp_path) -> None:
    session = _runtime_session(tmp_path, intent="bridge")
    store = RunArtifactStore(session.config)
    service = RunObservabilityService(enabled=True, artifact_store=store)
    run = store.start_run(session=session, mode_id="agent", run_id="run_20260312T162000Z_ccccdddd")

    clear_runtime_progress(session)
    service.bind_session(session, run)
    emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "iteration",
            "status": "running",
            "corr_id": "corr:agent",
            "task_id": "task:1",
            "step_id": "step:1",
            "iteration": 1,
            "message": "Итерация 1",
        },
    )
    service.unbind_session(session, run=run)
    emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "after_unbind",
            "status": "running",
            "message": "Не должно попасть в run events",
        },
    )

    metrics = _load_json(run.metrics_path)
    events = [
        json.loads(line)
        for line in Path(run.events_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert metrics["runtime_progress"]["events"] == 1
    assert metrics["runtime_progress"]["last_event"]["phase"] == "iteration"
    assert [event["event_type"] for event in events] == ["runtime_progress"]
    assert events[0]["phase"] == "iteration"
    assert events[0]["source"] == "agent_core"


def test_run_observability_isolates_sequential_runs_with_different_intent(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="isolation")
    store = RunArtifactStore(cfg)
    service = RunObservabilityService(enabled=True, artifact_store=store)
    session_a = _artifact_session(tmp_path, name="intent-a", session_uid="thread:-100:intent-a", session_id="s-a")
    session_b = _artifact_session(tmp_path, name="intent-b", session_uid="thread:-100:intent-b", session_id="s-b")
    run_a = store.start_run(session=session_a, mode_id="manager", run_id="run_20260312T163000Z_aaaabbbb")
    run_b = store.start_run(session=session_b, mode_id="manager", run_id="run_20260312T163100Z_ccccdddd")

    service.record_unit_start(run_a, unit_id="unit:a", phase="plan", ts=20.0)
    service.record_unit_end(run_a, unit_id="unit:a", phase="plan", duration_sec=1.0, input_tokens=5, ts=21.0)
    service.record_unit_start(run_b, unit_id="unit:b", phase="execute", ts=30.0)
    service.record_unit_end(run_b, unit_id="unit:b", phase="execute", duration_sec=2.0, input_tokens=9, ts=32.0)

    metrics_a = _load_json(run_a.metrics_path)
    metrics_b = _load_json(run_b.metrics_path)

    assert metrics_a["units"][0]["unit_id"] == "unit:a"
    assert metrics_b["units"][0]["unit_id"] == "unit:b"
    assert metrics_a["totals"]["input_tokens"] == 5
    assert metrics_b["totals"]["input_tokens"] == 9
    assert metrics_a["run_id"] != metrics_b["run_id"]
