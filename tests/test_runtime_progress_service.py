import time

from app.services.runtime_progress_service import (
    build_runtime_progress_payload,
    clear_runtime_progress,
    emit_runtime_progress,
)
from app.services.session_tick_history_store import load_session_ticks
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


def _build_session(tmp_path) -> Session:
    tool = ToolConfig(
        name="dummy",
        mode="headless",
        cmd=["bash", "-lc", "cat"],
        headless_cmd=["bash", "-lc", "cat"],
    )
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"dummy": tool},
        defaults=DefaultsConfig(workdir=str(tmp_path), state_path=str(tmp_path / "state.json")),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return Session(
        id="s1",
        tool=tool,
        workdir=str(tmp_path),
        idle_timeout_sec=10,
        config=cfg,
        chat_id=777,
    )


def test_runtime_progress_updates_tick_history_and_deduplicates(tmp_path) -> None:
    session = _build_session(tmp_path)
    clear_runtime_progress(session)

    e1 = emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "iteration",
            "status": "running",
            "corr_id": "s1:step1",
            "task_id": "step1",
            "iteration": 1,
            "message": "Итерация 1: запрос к модели",
        },
    )
    assert e1["source"] == "agent_core"
    assert session.tick_seen == 1
    first_tick_count = len(load_session_ticks(session))
    assert first_tick_count == 1

    emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "iteration",
            "status": "running",
            "corr_id": "s1:step1",
            "task_id": "step1",
            "iteration": 1,
            "message": "Итерация 1: запрос к модели",
            "ts": time.time(),
        },
    )
    assert session.tick_seen == 1
    assert len(load_session_ticks(session)) == first_tick_count

    emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "tool_batch",
            "status": "running",
            "corr_id": "s1:step1",
            "task_id": "step1",
            "iteration": 1,
            "message": "Итерация 1: вызовы инструментов",
        },
    )
    assert session.tick_seen == 2
    assert len(load_session_ticks(session)) == 2


def test_runtime_progress_payload_contains_last_and_recent(tmp_path) -> None:
    session = _build_session(tmp_path)
    clear_runtime_progress(session)
    emit_runtime_progress(
        session,
        {
            "mode_id": "analyst",
            "source": "orchestrator",
            "phase": "planning_start",
            "status": "running",
            "message": "Планирование: попытка 1",
        },
    )
    emit_runtime_progress(
        session,
        {
            "mode_id": "analyst",
            "source": "orchestrator",
            "phase": "plan_ready",
            "status": "running",
            "message": "План готов: 3 шага",
        },
    )

    payload = build_runtime_progress_payload(session, recent_limit=5)
    assert payload["last_source"] == "orchestrator"
    assert payload["last_phase"] == "plan_ready"
    assert payload["last_status"] == "running"
    assert isinstance(payload["recent_events"], list)
    assert len(payload["recent_events"]) == 2


def test_runtime_progress_payload_includes_event_type(tmp_path) -> None:
    session = _build_session(tmp_path)
    clear_runtime_progress(session)
    emit_runtime_progress(
        session,
        {
            "mode_id": "analyst",
            "source": "orchestrator",
            "phase": "step_start",
            "status": "running",
            "message": "Старт шага step_1",
        },
    )
    payload = build_runtime_progress_payload(session, recent_limit=5)
    assert payload["last_event_type"] == "step_started"
    # Backward compat: legacy fields still present.
    assert payload["last_source"] == "orchestrator"
    assert payload["last_phase"] == "step_start"
    assert payload["last_status"] == "running"
    assert payload["last_message"] == "Старт шага step_1"


def test_runtime_progress_payload_event_type_none_when_empty(tmp_path) -> None:
    session = _build_session(tmp_path)
    clear_runtime_progress(session)
    payload = build_runtime_progress_payload(session)
    assert payload["last_event_type"] is None
    assert payload["last_source"] is None


def test_runtime_progress_invalid_iteration_is_tolerated(tmp_path) -> None:
    session = _build_session(tmp_path)
    clear_runtime_progress(session)
    event = emit_runtime_progress(
        session,
        {
            "mode_id": "agent",
            "source": "agent_core",
            "phase": "iteration",
            "status": "running",
            "iteration": "not-a-number",
            "message": "bad iteration payload",
        },
    )
    assert event["iteration"] == 0
