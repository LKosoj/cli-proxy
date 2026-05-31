from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict

from app.services.redaction import redact_value


@dataclass(frozen=True)
class AgentLifecycleEvent:
    event_type: str
    mode_id: str = ""
    session_id: str = ""
    session_uid: str = ""
    run_id: str = ""
    task_id: str = ""
    corr_id: str = ""
    phase: str = ""
    status: str = ""
    iteration: int = 0
    step_id: str = ""
    tool_name: str = ""
    message: str = ""
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=lambda: float(time.time()))
    redacted: bool = False

    def redacted_copy(self) -> "AgentLifecycleEvent":
        redacted_metadata = redact_value(dict(self.metadata or {}))
        return AgentLifecycleEvent(
            event_type=self.event_type,
            mode_id=self.mode_id,
            session_id=self.session_id,
            session_uid=self.session_uid,
            run_id=self.run_id,
            task_id=self.task_id,
            corr_id=self.corr_id,
            phase=self.phase,
            status=self.status,
            iteration=self.iteration,
            step_id=self.step_id,
            tool_name=self.tool_name,
            message=str(redact_value(self.message)),
            error=str(redact_value(self.error)),
            metadata=redacted_metadata if isinstance(redacted_metadata, dict) else {},
            ts=self.ts,
            redacted=True,
        )

    def to_runtime_progress_payload(self) -> Dict[str, Any]:
        return {
            "mode_id": self.mode_id,
            "source": "agent_core",
            "phase": self.phase or self.event_type,
            "status": self.status or "running",
            "message": self.message,
            "corr_id": self.corr_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "iteration": int(self.iteration or 0),
            "ts": float(self.ts),
        }

    def to_artifact_event(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "mode_id": self.mode_id,
            "session_id": self.session_id,
            "session_uid": self.session_uid,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "corr_id": self.corr_id,
            "phase": self.phase,
            "status": self.status,
            "iteration": int(self.iteration or 0),
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "message": self.message,
            "error": self.error,
            "metadata": dict(self.metadata or {}),
            "observed_at": float(self.ts),
            "redacted": bool(self.redacted),
        }


AgentLifecycleHook = Callable[[AgentLifecycleEvent], Awaitable[None]]
