"""Trace contract: normalized event builder for orchestration observability.

All trace events pass through ``normalize_trace_event`` / ``build_trace_event``
to guarantee safe defaults and consistent field types.  Never raises exceptions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict

_LOG = logging.getLogger(__name__)

# Field length limits (truncation, not validation).
_MAX_EVENT_TYPE = 64
_MAX_MESSAGE = 500
_MAX_ERROR = 1000
_MAX_GENERIC_STR = 256


@dataclass
class TraceEvent:
    event_type: str = ""
    mode_id: str = ""
    session_id: str = ""
    timestamp: float = 0.0
    step_id: str = ""
    task_id: str = ""
    corr_id: str = ""
    status: str = ""
    message: str = ""
    error: str = ""
    iteration: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


def _safe_str(value: Any, max_len: int = _MAX_GENERIC_STR) -> str:
    try:
        s = str(value) if value is not None else ""
        return s.strip()[:max_len]
    except Exception:
        return ""


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        v = float(value)
        return v if v > 0 else 0.0
    except Exception:
        return 0.0


def normalize_trace_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an arbitrary dict into a well-typed trace event dict.

    Never raises.  Returns a minimal safe event on any error.
    """
    try:
        event_type = _safe_str(raw.get("event_type"), _MAX_EVENT_TYPE) or "unknown"
        ts = _safe_float(raw.get("timestamp"))
        if ts <= 0:
            ts = time.time()
        result: Dict[str, Any] = {
            "event_type": event_type,
            "mode_id": _safe_str(raw.get("mode_id")),
            "session_id": _safe_str(raw.get("session_id")),
            "timestamp": ts,
        }
        # Optional fields — only include when non-empty.
        for key, max_len in [
            ("step_id", _MAX_GENERIC_STR),
            ("task_id", _MAX_GENERIC_STR),
            ("corr_id", _MAX_GENERIC_STR),
            ("status", _MAX_GENERIC_STR),
        ]:
            val = _safe_str(raw.get(key))
            if val:
                result[key] = val
        msg = _safe_str(raw.get("message"), _MAX_MESSAGE)
        if msg:
            result["message"] = msg
        err = _safe_str(raw.get("error"), _MAX_ERROR)
        if err:
            result["error"] = err
        iteration = _safe_int(raw.get("iteration"))
        if iteration:
            result["iteration"] = iteration
        meta = raw.get("metadata")
        if isinstance(meta, dict) and meta:
            result["metadata"] = meta
        return result
    except Exception:
        _LOG.exception("trace normalization failed, returning minimal event")
        return {
            "event_type": "unknown",
            "mode_id": "",
            "session_id": "",
            "timestamp": time.time(),
        }


def build_trace_event(
    event_type: str,
    *,
    mode_id: str = "",
    session_id: str = "",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Convenience builder — assembles a raw dict and normalizes it."""
    raw: Dict[str, Any] = {
        "event_type": event_type,
        "mode_id": mode_id,
        "session_id": session_id,
    }
    raw.update(kwargs)
    return normalize_trace_event(raw)


# Mapping from runtime_progress (source, phase) → trace event_type.
# Only explicit mappings; unknown combinations fall through to phase value.
_PHASE_TO_EVENT_TYPE: Dict[str, str] = {
    "start": "run_started",
    "final": "run_finished",
    "error": "run_failed",
    "cancelled": "run_cancelled",
    "step_start": "step_started",
    "step_end": "step_finished",
    "awaiting_input": "awaiting_input",
    "planning_start": "planning_started",
    "plan_ready": "plan_ready",
    "plan_drained": "plan_drained",
    "replan": "replan",
    "clarification_limit": "clarification_limit",
}


def adapt_runtime_event(runtime_event: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a runtime_progress event dict into a normalized trace event dict.

    Bridges the runtime_progress schema (source/phase/status/message) to the
    trace contract schema (event_type/mode_id/status/message).  Preserves all
    original fields in ``metadata`` for backward compatibility.

    Never raises.
    """
    try:
        source = _safe_str(runtime_event.get("source"), _MAX_GENERIC_STR)
        phase = _safe_str(runtime_event.get("phase"), _MAX_GENERIC_STR)
        event_type = _PHASE_TO_EVENT_TYPE.get(phase, phase) or "unknown"

        ts = 0.0
        ts_raw = runtime_event.get("ts")
        if ts_raw is not None:
            ts = _safe_float(ts_raw)
        if ts <= 0:
            ts_raw2 = runtime_event.get("timestamp")
            if ts_raw2 is not None:
                ts = _safe_float(ts_raw2)
        if ts <= 0:
            ts = time.time()

        raw: Dict[str, Any] = {
            "event_type": event_type,
            "mode_id": _safe_str(runtime_event.get("mode_id")),
            "session_id": _safe_str(runtime_event.get("session_id")),
            "timestamp": ts,
            "step_id": _safe_str(runtime_event.get("step_id")),
            "task_id": _safe_str(runtime_event.get("task_id")),
            "corr_id": _safe_str(runtime_event.get("corr_id")),
            "status": _safe_str(runtime_event.get("status")),
            "message": _safe_str(runtime_event.get("message"), _MAX_MESSAGE),
            "iteration": _safe_int(runtime_event.get("iteration")),
            "metadata": {"source": source, "phase": phase},
        }
        return normalize_trace_event(raw)
    except Exception:
        _LOG.exception("adapt_runtime_event failed, returning minimal event")
        return normalize_trace_event({})
