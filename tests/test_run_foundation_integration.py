from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.run_artifact_store import RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_doctor_service import RunDoctorService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.sdk.json_store import read_json_locked


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


def _session(tmp_path: Path, *, case_id: str) -> SimpleNamespace:
    workdir = tmp_path / case_id
    workdir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        id=f"s-{case_id}",
        workdir=str(workdir),
        conversation_scope=SimpleNamespace(session_uid=f"thread:-100:{case_id}"),
    )


def test_run_foundation_integration_doctor_reads_artifact_store_state(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="foundation_integration")
    session = _session(tmp_path, case_id="foundation_integration")
    artifact_store = RunArtifactStore(cfg)
    boundary_service = RunBoundaryValidationService(enabled=True)
    doctor_service = RunDoctorService(
        enabled=True,
        artifact_store=artifact_store,
        boundary_validator=boundary_service,
        now_fn=lambda: 1_710_777_000.0,
    )
    run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T190000Z_found000",
    )

    artifact_store.save_state(
        run,
        {
            "phase": "execute",
            "current_unit_id": "unit:browser-review",
            "mode_context": {"required_use_cli_steps": []},
        },
    )
    artifact_store.save_plan(
        run,
        {
            "units": [{"id": "unit:browser-review", "step_type": "use_cli", "title": "Browser review"}],
        },
    )
    artifact_store.save_metrics(
        run,
        {
            "units": [{"unit_id": "unit:browser-review", "duration_sec": 1.2}],
        },
    )
    artifact_store.append_event(
        run,
        {
            "event_type": "unit_end",
            "unit_id": "unit:browser-review",
        },
    )

    boundary_report = boundary_service.validate(run, mode_id="agent", phase="execute")
    doctor_report = doctor_service.diagnose(run, mode_id="agent", phase="execute")
    recovery = read_json_locked(run.recovery_path, default={})

    assert boundary_report.status == "ok"
    assert boundary_report.issues == []
    assert doctor_report.status == "ok"
    assert doctor_report.recommended_action == "no_action"
    assert doctor_report.issues == []
    assert recovery["status"] == "ok"
    assert recovery["recommended_action"] == "no_action"
    assert recovery["attempts"][0]["phase"] == "execute"
