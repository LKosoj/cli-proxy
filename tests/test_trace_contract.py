"""Tests for app.services.trace_contract."""

from app.services.trace_contract import (
    adapt_runtime_event,
    build_trace_event,
    normalize_trace_event,
)


def test_trace_contract_builds_safe_event_for_minimal_payload():
    result = normalize_trace_event({})
    assert result["event_type"] == "unknown"
    assert result["mode_id"] == ""
    assert result["session_id"] == ""
    assert isinstance(result["timestamp"], float)
    assert result["timestamp"] > 0
    # None values must be absent (compact output).
    for v in result.values():
        assert v is not None


def test_trace_contract_normalizes_optional_fields():
    result = normalize_trace_event({
        "event_type": "step_started",
        "mode_id": "analyst",
        "session_id": "sess_1",
        "step_id": "step_42",
        "corr_id": "corr_99",
        "status": "running",
        "message": "Старт шага",
    })
    assert result["event_type"] == "step_started"
    assert result["mode_id"] == "analyst"
    assert result["session_id"] == "sess_1"
    assert result["step_id"] == "step_42"
    assert result["corr_id"] == "corr_99"
    assert result["status"] == "running"
    assert result["message"] == "Старт шага"
    assert isinstance(result["timestamp"], float)
    assert result["timestamp"] > 0


def test_trace_contract_truncates_long_fields():
    long_event_type = "x" * 200
    long_message = "m" * 1000
    result = normalize_trace_event({
        "event_type": long_event_type,
        "message": long_message,
    })
    assert len(result["event_type"]) <= 64
    assert len(result["message"]) <= 500


def test_build_trace_event_convenience():
    result = build_trace_event(
        "run_started",
        mode_id="manager",
        session_id="s1",
        status="running",
    )
    assert result["event_type"] == "run_started"
    assert result["mode_id"] == "manager"
    assert result["session_id"] == "s1"
    assert result["status"] == "running"
    assert isinstance(result["timestamp"], float)


def test_normalize_handles_none_and_non_string_values():
    result = normalize_trace_event({
        "event_type": "test",
        "corr_id": None,
        "iteration": "3",
        "metadata": {"key": "val"},
    })
    assert result["event_type"] == "test"
    assert "corr_id" not in result  # None → empty → omitted
    assert result["iteration"] == 3
    assert result["metadata"] == {"key": "val"}


def test_normalize_strips_whitespace():
    result = normalize_trace_event({
        "event_type": "  step_finished  ",
        "mode_id": " analyst ",
    })
    assert result["event_type"] == "step_finished"
    assert result["mode_id"] == "analyst"


# --- adapt_runtime_event tests ---


def test_adapt_runtime_event_maps_phase_to_event_type():
    result = adapt_runtime_event({
        "source": "orchestrator",
        "phase": "step_start",
        "status": "running",
        "mode_id": "analyst",
        "message": "Старт шага step_1",
        "ts": 1700000000.0,
    })
    assert result["event_type"] == "step_started"
    assert result["status"] == "running"
    assert result["mode_id"] == "analyst"
    assert result["message"] == "Старт шага step_1"
    meta = result.get("metadata", {})
    assert meta.get("source") == "orchestrator"
    assert meta.get("phase") == "step_start"


def test_adapt_runtime_event_preserves_legacy_fields_in_metadata():
    result = adapt_runtime_event({
        "source": "mode_pipeline",
        "phase": "final",
        "status": "ok",
        "mode_id": "manager",
        "message": "Режим manager завершен",
    })
    assert result["event_type"] == "run_finished"
    meta = result.get("metadata", {})
    assert meta["source"] == "mode_pipeline"
    assert meta["phase"] == "final"


def test_adapt_runtime_event_unknown_phase_passes_through():
    result = adapt_runtime_event({
        "source": "agent_core",
        "phase": "tool_batch",
        "status": "running",
    })
    assert result["event_type"] == "tool_batch"
    meta = result.get("metadata", {})
    assert meta["source"] == "agent_core"


def test_adapt_runtime_event_empty_dict_returns_safe_event():
    result = adapt_runtime_event({})
    assert result["event_type"] == "unknown"
    assert isinstance(result["timestamp"], float)
    assert result["timestamp"] > 0


def test_adapt_runtime_event_uses_ts_field():
    result = adapt_runtime_event({"phase": "start", "ts": 1700000000.0})
    assert result["timestamp"] == 1700000000.0
