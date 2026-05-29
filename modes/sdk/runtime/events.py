from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class EventType(str, Enum):
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"


class EventSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class OrchestratorEvent:
    event_type: EventType
    severity: EventSeverity
    session_id: str
    step_id: str
    message: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "session_id": str(self.session_id or ""),
            "step_id": str(self.step_id or ""),
            "message": str(self.message or ""),
            "payload": dict(self.payload or {}),
            "ts": float(self.ts),
        }
        # Cross-process delivery requires JSON-serializable payloads.
        json.dumps(data, ensure_ascii=False)
        return data

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "OrchestratorEvent":
        if not isinstance(raw, dict):
            raise ValueError("OrchestratorEvent payload must be dict")
        try:
            event_type = EventType(str(raw.get("event_type") or ""))
        except Exception as exc:
            raise ValueError("OrchestratorEvent.event_type is invalid") from exc
        try:
            severity = EventSeverity(str(raw.get("severity") or ""))
        except Exception as exc:
            raise ValueError("OrchestratorEvent.severity is invalid") from exc

        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("OrchestratorEvent.payload must be dict")

        return cls(
            event_type=event_type,
            severity=severity,
            session_id=str(raw.get("session_id") or ""),
            step_id=str(raw.get("step_id") or ""),
            message=str(raw.get("message") or ""),
            payload=payload,
            ts=float(raw.get("ts") or 0.0),
        )
