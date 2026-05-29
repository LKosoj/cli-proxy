from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from app.services.session_tick_history_store import append_session_tick

logger = logging.getLogger(__name__)
agent_runtime_logger = logging.getLogger("agent.runtime")

MAX_RUNTIME_EVENTS_PER_SESSION = 50
DEFAULT_RECENT_LIMIT = 10
DEDUP_WINDOW_SEC = 1.0


def _clean_text(value: Any, *, max_len: int = 280) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _clean_id(value: Any, *, max_len: int = 128) -> str:
    return _clean_text(value, max_len=max_len)


def _to_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _normalize_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    ts_raw = raw.get("ts")
    try:
        ts = float(ts_raw) if ts_raw is not None else float(now)
    except Exception:
        ts = float(now)

    event = {
        "ts": ts,
        "mode_id": _clean_id(raw.get("mode_id"), max_len=64),
        "source": _clean_id(raw.get("source"), max_len=64) or "runtime",
        "phase": _clean_id(raw.get("phase"), max_len=64) or "event",
        "status": _clean_id(raw.get("status"), max_len=32) or "running",
        "corr_id": _clean_id(raw.get("corr_id"), max_len=128),
        "task_id": _clean_id(raw.get("task_id"), max_len=128),
        "step_id": _clean_id(raw.get("step_id"), max_len=128),
        "iteration": _to_int(raw.get("iteration"), default=0),
        "message": _clean_text(raw.get("message"), max_len=280),
    }
    return event


def _event_signature(event: Dict[str, Any]) -> str:
    return "|".join(
        [
            str(event.get("mode_id") or ""),
            str(event.get("source") or ""),
            str(event.get("phase") or ""),
            str(event.get("status") or ""),
            str(event.get("corr_id") or ""),
            str(event.get("task_id") or ""),
            str(event.get("step_id") or ""),
            str(event.get("iteration") or 0),
            str(event.get("message") or ""),
        ]
    )


def format_tick_value(event: Dict[str, Any]) -> str:
    source = str(event.get("source") or "runtime")
    phase = str(event.get("phase") or "event")
    status = str(event.get("status") or "running")
    message = _clean_text(event.get("message"), max_len=220)
    if message:
        return f"[{source}][{phase}][{status}] {message}"
    return f"[{source}][{phase}][{status}] progress"


def clear_runtime_progress(session: Any) -> None:
    try:
        setattr(session, "runtime_progress_last_event", None)
        setattr(session, "runtime_progress_events", [])
        setattr(session, "_runtime_progress_last_sig", "")
        setattr(session, "_runtime_progress_last_ts", 0.0)
    except Exception:
        logger.exception("runtime progress clear failed for session=%s", getattr(session, "id", None))


def _emit_observability_bridge(session: Any, event: Dict[str, Any]) -> None:
    callback = getattr(session, "_run_observability_bridge", None)
    if not callable(callback):
        return
    try:
        callback(dict(event))
    except Exception:
        logger.exception("runtime progress observability bridge failed for session=%s", getattr(session, "id", None))


def emit_runtime_progress(session: Any, raw_event: Dict[str, Any]) -> Dict[str, Any]:
    event = _normalize_event(dict(raw_event or {}))
    ts = float(event.get("ts") or time.time())
    sig = _event_signature(event)

    prev_sig = str(getattr(session, "_runtime_progress_last_sig", "") or "")
    try:
        prev_ts = float(getattr(session, "_runtime_progress_last_ts", 0.0) or 0.0)
    except Exception:
        prev_ts = 0.0
    is_duplicate = bool(sig and prev_sig and sig == prev_sig and (ts - prev_ts) <= DEDUP_WINDOW_SEC)

    setattr(session, "last_output_ts", ts)
    setattr(session, "last_tick_ts", ts)

    tick_value = format_tick_value(event)
    if not is_duplicate:
        current_tick = getattr(session, "last_tick_value", None)
        if current_tick:
            try:
                setattr(session, "tick_seen", int(getattr(session, "tick_seen", 0) or 0) + 1)
            except Exception:
                setattr(session, "tick_seen", 1)
        else:
            try:
                setattr(session, "tick_seen", max(1, int(getattr(session, "tick_seen", 0) or 0)))
            except Exception:
                setattr(session, "tick_seen", 1)
        setattr(session, "last_tick_value", tick_value)
        try:
            append_session_tick(session, value=tick_value, ts=ts)
        except Exception:
            logger.exception("runtime progress append tick failed session=%s", getattr(session, "id", None))
    else:
        # Keep heartbeat fresh even when event is deduplicated.
        if not getattr(session, "last_tick_value", None):
            setattr(session, "last_tick_value", tick_value)

    setattr(session, "_runtime_progress_last_sig", sig)
    setattr(session, "_runtime_progress_last_ts", ts)

    previous = getattr(session, "runtime_progress_events", None)
    events: List[Dict[str, Any]] = list(previous) if isinstance(previous, list) else []
    events.append(event)
    if len(events) > MAX_RUNTIME_EVENTS_PER_SESSION:
        events = events[-MAX_RUNTIME_EVENTS_PER_SESSION:]
    setattr(session, "runtime_progress_events", events)
    setattr(session, "runtime_progress_last_event", event)

    agent_runtime_logger.info(
        "runtime_progress mode=%s source=%s phase=%s status=%s corr_id=%s task_id=%s step_id=%s iteration=%s msg=%s",
        event.get("mode_id") or "-",
        event.get("source") or "-",
        event.get("phase") or "-",
        event.get("status") or "-",
        event.get("corr_id") or "-",
        event.get("task_id") or "-",
        event.get("step_id") or "-",
        event.get("iteration") or 0,
        event.get("message") or "-",
    )
    _emit_observability_bridge(session, event)
    return event


def build_runtime_progress_payload(session: Any, *, recent_limit: int = DEFAULT_RECENT_LIMIT) -> Dict[str, Any]:
    from app.services.trace_contract import adapt_runtime_event

    last = getattr(session, "runtime_progress_last_event", None)
    events = getattr(session, "runtime_progress_events", None)
    items: List[Dict[str, Any]] = list(events) if isinstance(events, list) else []
    limit = max(1, int(recent_limit or DEFAULT_RECENT_LIMIT))
    recent = items[-limit:]

    out: Dict[str, Any] = {
        "last_source": None,
        "last_phase": None,
        "last_status": None,
        "last_message": None,
        "last_corr_id": None,
        "last_task_id": None,
        "last_step_id": None,
        "last_iteration": None,
        "last_ts": None,
        "last_event_type": None,
        "recent_events": recent,
    }
    if isinstance(last, dict):
        trace = adapt_runtime_event(last)
        meta = trace.get("metadata") or {}
        out.update(
            {
                "last_source": meta.get("source") or last.get("source"),
                "last_phase": meta.get("phase") or last.get("phase"),
                "last_status": trace.get("status") or last.get("status"),
                "last_message": trace.get("message") or last.get("message"),
                "last_corr_id": trace.get("corr_id") or last.get("corr_id"),
                "last_task_id": trace.get("task_id") or last.get("task_id"),
                "last_step_id": trace.get("step_id") or last.get("step_id"),
                "last_iteration": trace.get("iteration") or last.get("iteration"),
                "last_ts": trace.get("timestamp") or last.get("ts"),
                "last_event_type": trace.get("event_type"),
            }
        )
    return out
