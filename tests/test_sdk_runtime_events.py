import json

import pytest

from modes.sdk.runtime import EventSeverity, EventType, OrchestratorEvent


def test_orchestrator_event_to_dict_is_json_serializable() -> None:
    event = OrchestratorEvent(
        event_type=EventType.STEP_STARTED,
        severity=EventSeverity.INFO,
        session_id="s1",
        step_id="step-1",
        message="started",
        payload={"attempt": 1, "meta": {"tool": "codex"}},
    )

    data = event.to_dict()
    rendered = json.dumps(data, ensure_ascii=False)

    assert data["event_type"] == "STEP_STARTED"
    assert data["severity"] == "info"
    assert data["session_id"] == "s1"
    assert data["step_id"] == "step-1"
    assert "STEP_STARTED" in rendered


def test_orchestrator_event_from_dict_roundtrip() -> None:
    raw = {
        "event_type": "STEP_FAILED",
        "severity": "error",
        "session_id": "s2",
        "step_id": "step-9",
        "message": "failed",
        "payload": {"error": "timeout"},
        "ts": 123.5,
    }

    event = OrchestratorEvent.from_dict(raw)
    data = event.to_dict()

    assert event.event_type is EventType.STEP_FAILED
    assert event.severity is EventSeverity.ERROR
    assert data["payload"]["error"] == "timeout"
    assert data["ts"] == 123.5


def test_orchestrator_event_from_dict_rejects_invalid_enum() -> None:
    with pytest.raises(ValueError, match="event_type is invalid"):
        OrchestratorEvent.from_dict(
            {
                "event_type": "UNKNOWN",
                "severity": "info",
                "session_id": "s",
                "step_id": "x",
                "payload": {},
            }
        )
