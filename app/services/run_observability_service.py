from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from modes.sdk.json_store import update_json_locked

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore

logger = logging.getLogger(__name__)


def _now_ts() -> float:
    return float(time.time())


def _clean_text(value: Any, *, max_len: int = 256) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _clean_optional_text(value: Any, *, max_len: int = 256) -> Optional[str]:
    text = _clean_text(value, max_len=max_len)
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _clean_skill_ids(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        skill_id = _clean_text(item, max_len=128)
        if not skill_id or skill_id in seen:
            continue
        result.append(skill_id)
        seen.add(skill_id)
    return result


def _merge_optional_number(current: Any, new_value: Optional[float], *, as_int: bool) -> Optional[float] | Optional[int]:
    current_value = _optional_float(current)
    if new_value is None:
        if current_value is None:
            return None
        return int(current_value) if as_int else float(current_value)
    total = (current_value or 0.0) + float(new_value)
    return int(total) if as_int else float(total)


class RunObservabilityService:
    BRIDGE_ATTR = "_run_observability_bridge"
    BRIDGE_RUN_ATTR = "_run_observability_bound_run_id"

    def __init__(self, *, enabled: bool, artifact_store: RunArtifactStore):
        self.enabled = bool(enabled)
        self.artifact_store = artifact_store

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    def bind_session(self, session: Any, run: RunArtifactHandle) -> None:
        if not self.is_enabled():
            return

        def _bridge(event: Dict[str, Any]) -> None:
            self.record_runtime_progress(run, event=event)

        setattr(session, self.BRIDGE_ATTR, _bridge)
        setattr(session, self.BRIDGE_RUN_ATTR, run.run_id)

    def unbind_session(self, session: Any, *, run: Optional[RunArtifactHandle] = None) -> None:
        current_run_id = str(getattr(session, self.BRIDGE_RUN_ATTR, "") or "").strip()
        if run is not None and current_run_id and current_run_id != run.run_id:
            return
        try:
            delattr(session, self.BRIDGE_ATTR)
        except Exception:
            pass
        try:
            delattr(session, self.BRIDGE_RUN_ATTR)
        except Exception:
            pass

    def record_phase_start(
        self,
        run: RunArtifactHandle,
        *,
        phase: str,
        corr_id: Any = None,
        message: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_text(phase, max_len=64) or "phase"
        self._update_metrics(
            run,
            lambda doc: self._apply_phase_start(doc, phase=phase_name, recorded_at=recorded_at),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "phase_start",
                "phase": phase_name,
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "message": _clean_optional_text(message, max_len=280),
                "observed_at": recorded_at,
            },
        )

    def record_phase_end(
        self,
        run: RunArtifactHandle,
        *,
        phase: str,
        status: str = "ok",
        duration_sec: Any = None,
        tool_calls: Any = None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        cost_usd: Any = None,
        corr_id: Any = None,
        message: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_text(phase, max_len=64) or "phase"
        duration_value = _optional_float(duration_sec)
        input_value = _optional_int(input_tokens)
        output_value = _optional_int(output_tokens)
        cost_value = _optional_float(cost_usd)
        tool_calls_value = _optional_int(tool_calls)
        self._update_metrics(
            run,
            lambda doc: self._apply_phase_end(
                doc,
                phase=phase_name,
                recorded_at=recorded_at,
                status=_clean_text(status, max_len=32) or "ok",
                duration_sec=duration_value,
                tool_calls=tool_calls_value,
                input_tokens=input_value,
                output_tokens=output_value,
                cost_usd=cost_value,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "phase_end",
                "phase": phase_name,
                "status": _clean_text(status, max_len=32) or "ok",
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "message": _clean_optional_text(message, max_len=280),
                "duration_sec": duration_value,
                "tool_calls": tool_calls_value,
                "input_tokens": input_value,
                "output_tokens": output_value,
                "cost_usd": cost_value,
                "observed_at": recorded_at,
            },
        )

    def record_unit_start(
        self,
        run: RunArtifactHandle,
        *,
        unit_id: str,
        phase: Any = None,
        corr_id: Any = None,
        message: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        unit_key = _clean_text(unit_id, max_len=128) or "unit"
        phase_name = _clean_optional_text(phase, max_len=64)
        self._update_metrics(
            run,
            lambda doc: self._apply_unit_start(doc, unit_id=unit_key, phase=phase_name, recorded_at=recorded_at),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "unit_start",
                "unit_id": unit_key,
                "phase": phase_name,
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "message": _clean_optional_text(message, max_len=280),
                "observed_at": recorded_at,
            },
        )

    def record_unit_end(
        self,
        run: RunArtifactHandle,
        *,
        unit_id: str,
        phase: Any = None,
        status: str = "ok",
        duration_sec: Any = None,
        tool_calls: Any = None,
        input_tokens: Any = None,
        output_tokens: Any = None,
        cost_usd: Any = None,
        corr_id: Any = None,
        message: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        unit_key = _clean_text(unit_id, max_len=128) or "unit"
        phase_name = _clean_optional_text(phase, max_len=64)
        duration_value = _optional_float(duration_sec)
        tool_calls_value = _optional_int(tool_calls)
        input_value = _optional_int(input_tokens)
        output_value = _optional_int(output_tokens)
        cost_value = _optional_float(cost_usd)
        self._update_metrics(
            run,
            lambda doc: self._apply_unit_end(
                doc,
                unit_id=unit_key,
                phase=phase_name,
                recorded_at=recorded_at,
                status=_clean_text(status, max_len=32) or "ok",
                duration_sec=duration_value,
                tool_calls=tool_calls_value,
                input_tokens=input_value,
                output_tokens=output_value,
                cost_usd=cost_value,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "unit_end",
                "unit_id": unit_key,
                "phase": phase_name,
                "status": _clean_text(status, max_len=32) or "ok",
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "message": _clean_optional_text(message, max_len=280),
                "duration_sec": duration_value,
                "tool_calls": tool_calls_value,
                "input_tokens": input_value,
                "output_tokens": output_value,
                "cost_usd": cost_value,
                "observed_at": recorded_at,
            },
        )

    def record_retry(
        self,
        run: RunArtifactHandle,
        *,
        phase: Any = None,
        unit_id: Any = None,
        reason: Any = None,
        corr_id: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_optional_text(phase, max_len=64)
        unit_key = _clean_optional_text(unit_id, max_len=128)
        self._update_metrics(
            run,
            lambda doc: self._increment_counter(
                doc,
                total_key="retries",
                unit_key=unit_key,
                unit_field="retries",
                phase=phase_name,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "retry",
                "phase": phase_name,
                "unit_id": unit_key,
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "reason": _clean_optional_text(reason, max_len=280),
                "observed_at": recorded_at,
            },
        )

    def record_recovery_attempt(
        self,
        run: RunArtifactHandle,
        *,
        phase: Any = None,
        unit_id: Any = None,
        action: Any = None,
        status: str = "started",
        corr_id: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_optional_text(phase, max_len=64)
        unit_key = _clean_optional_text(unit_id, max_len=128)
        self._update_metrics(
            run,
            lambda doc: self._increment_counter(
                doc,
                total_key="recovery_attempts",
                unit_key=unit_key,
                unit_field="recovery_attempts",
                phase=phase_name,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "recovery_attempt",
                "phase": phase_name,
                "unit_id": unit_key,
                "action": _clean_optional_text(action, max_len=128),
                "status": _clean_text(status, max_len=32) or "started",
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "observed_at": recorded_at,
            },
        )

    def record_skill_selection(
        self,
        run: RunArtifactHandle,
        *,
        phase: Any = None,
        unit_id: Any = None,
        selected_skills: Any = None,
        reason: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_optional_text(phase, max_len=64)
        unit_key = _clean_optional_text(unit_id, max_len=128)
        skills = _clean_skill_ids(selected_skills)
        self._update_metrics(
            run,
            lambda doc: self._increment_counter(
                doc,
                total_key="skill_selections",
                unit_key=unit_key,
                unit_field="skill_selection_count",
                phase=phase_name,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "skill_selection",
                "phase": phase_name,
                "unit_id": unit_key,
                "selected_skills": skills,
                "reason": _clean_optional_text(reason, max_len=280),
                "observed_at": recorded_at,
            },
        )

    def record_skill_discovery(
        self,
        run: RunArtifactHandle,
        *,
        phase: Any = None,
        unit_id: Any = None,
        source: Any = None,
        discovered_skills: Any = None,
        query: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_optional_text(phase, max_len=64)
        unit_key = _clean_optional_text(unit_id, max_len=128)
        skills = _clean_skill_ids(discovered_skills)
        self._update_metrics(
            run,
            lambda doc: self._increment_counter(
                doc,
                total_key="skill_discoveries",
                unit_key=unit_key,
                unit_field="skill_discovery_count",
                phase=phase_name,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "skill_discovery",
                "phase": phase_name,
                "unit_id": unit_key,
                "source": _clean_optional_text(source, max_len=128),
                "query": _clean_optional_text(query, max_len=160),
                "discovered_skills": skills,
                "observed_at": recorded_at,
            },
        )

    def record_skill_install(
        self,
        run: RunArtifactHandle,
        *,
        phase: Any = None,
        unit_id: Any = None,
        skill_id: Any,
        source: Any = None,
        target: Any = None,
        status: str = "ok",
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        phase_name = _clean_optional_text(phase, max_len=64)
        unit_key = _clean_optional_text(unit_id, max_len=128)
        cleaned_skill_id = _clean_text(skill_id, max_len=128) or "unknown-skill"
        self._update_metrics(
            run,
            lambda doc: self._increment_counter(
                doc,
                total_key="skill_installs",
                unit_key=unit_key,
                unit_field="skill_install_count",
                phase=phase_name,
            ),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "skill_install",
                "phase": phase_name,
                "unit_id": unit_key,
                "skill_id": cleaned_skill_id,
                "source": _clean_optional_text(source, max_len=128),
                "target": _clean_optional_text(target, max_len=128),
                "status": _clean_text(status, max_len=32) or "ok",
                "observed_at": recorded_at,
            },
        )

    def record_llm_request(
        self,
        run: RunArtifactHandle,
        *,
        model: Any = None,
        messages_count: Any = None,
        tools_count: Any = None,
        estimated_tokens: Any = None,
        corr_id: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "llm_request",
                "model": _clean_optional_text(model, max_len=64),
                "messages_count": _optional_int(messages_count),
                "tools_count": _optional_int(tools_count),
                "estimated_tokens": _optional_int(estimated_tokens),
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "observed_at": recorded_at,
            },
        )

    def record_llm_response(
        self,
        run: RunArtifactHandle,
        *,
        model: Any = None,
        content_preview: Any = None,
        tool_calls_summary: Any = None,
        usage_tokens: Any = None,
        duration_ms: Any = None,
        corr_id: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "llm_response",
                "model": _clean_optional_text(model, max_len=64),
                "content_preview": _clean_optional_text(content_preview, max_len=500),
                "tool_calls_summary": _clean_optional_text(tool_calls_summary, max_len=500),
                "usage_tokens": _optional_int(usage_tokens),
                "duration_ms": _optional_int(duration_ms),
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "observed_at": recorded_at,
            },
        )

    def record_tool_execution(
        self,
        run: RunArtifactHandle,
        *,
        tool_name: Any = None,
        args_preview: Any = None,
        result_preview: Any = None,
        success: bool = True,
        duration_ms: Any = None,
        error: Any = None,
        corr_id: Any = None,
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        recorded_at = float(ts if ts is not None else _now_ts())
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "tool_execution",
                "tool_name": _clean_optional_text(tool_name, max_len=128),
                "args_preview": _clean_optional_text(args_preview, max_len=500),
                "result_preview": _clean_optional_text(result_preview, max_len=1000),
                "success": bool(success),
                "duration_ms": _optional_int(duration_ms),
                "error": _clean_optional_text(error, max_len=500),
                "corr_id": _clean_optional_text(corr_id, max_len=128),
                "observed_at": recorded_at,
            },
        )

    def record_runtime_progress(self, run: RunArtifactHandle, *, event: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_enabled():
            return {}
        payload = dict(event or {})
        self._update_metrics(
            run,
            lambda doc: self._apply_runtime_progress(doc, payload),
        )
        return self.artifact_store.append_event(
            run,
            {
                "event_type": "runtime_progress",
                "mode_id": _clean_optional_text(payload.get("mode_id"), max_len=64),
                "source": _clean_optional_text(payload.get("source"), max_len=64),
                "phase": _clean_optional_text(payload.get("phase"), max_len=64),
                "status": _clean_optional_text(payload.get("status"), max_len=32),
                "corr_id": _clean_optional_text(payload.get("corr_id"), max_len=128),
                "task_id": _clean_optional_text(payload.get("task_id"), max_len=128),
                "step_id": _clean_optional_text(payload.get("step_id"), max_len=128),
                "iteration": _optional_int(payload.get("iteration")),
                "message": _clean_optional_text(payload.get("message"), max_len=280),
                "observed_at": _optional_float(payload.get("ts")) or _now_ts(),
            },
        )

    def _update_metrics(self, run: RunArtifactHandle, updater) -> Dict[str, Any]:
        return update_json_locked(
            run.metrics_path,
            lambda current: updater(self._normalize_metrics_doc(run, current)),
            default=self._metrics_default(run),
        )

    def _normalize_metrics_doc(self, run: RunArtifactHandle, current: Dict[str, Any]) -> Dict[str, Any]:
        doc = self._metrics_default(run)
        if not isinstance(current, dict):
            return doc
        doc["version"] = int(current.get("version") or doc["version"])
        doc["run_id"] = str(current.get("run_id") or run.run_id)
        current_totals = current.get("totals")
        if isinstance(current_totals, dict):
            doc["totals"].update(current_totals)
        current_units = current.get("units")
        if isinstance(current_units, list):
            doc["units"] = [dict(item) for item in current_units if isinstance(item, dict)]
        current_phases = current.get("phase_aggregates")
        if isinstance(current_phases, list):
            doc["phase_aggregates"] = [dict(item) for item in current_phases if isinstance(item, dict)]
        runtime_progress = current.get("runtime_progress")
        if isinstance(runtime_progress, dict):
            doc["runtime_progress"].update(runtime_progress)
        return doc

    @staticmethod
    def _metrics_default(run: RunArtifactHandle) -> Dict[str, Any]:
        return {
            "version": 1,
            "run_id": run.run_id,
            "totals": {
                "units": 0,
                "duration_sec": 0.0,
                "retries": 0,
                "recovery_attempts": 0,
                "tool_calls": 0,
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "skill_selections": 0,
                "skill_discoveries": 0,
                "skill_installs": 0,
            },
            "units": [],
            "phase_aggregates": [],
            "runtime_progress": {
                "events": 0,
                "last_event": None,
            },
        }

    def _apply_phase_start(self, doc: Dict[str, Any], *, phase: str, recorded_at: float) -> Dict[str, Any]:
        aggregate = self._ensure_phase_aggregate(doc, phase)
        aggregate["starts"] = int(aggregate.get("starts") or 0) + 1
        aggregate["last_started_at"] = recorded_at
        aggregate["last_status"] = "running"
        return doc

    def _apply_phase_end(
        self,
        doc: Dict[str, Any],
        *,
        phase: str,
        recorded_at: float,
        status: str,
        duration_sec: Optional[float],
        tool_calls: Optional[int],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        cost_usd: Optional[float],
    ) -> Dict[str, Any]:
        aggregate = self._ensure_phase_aggregate(doc, phase)
        aggregate["ends"] = int(aggregate.get("ends") or 0) + 1
        aggregate["last_status"] = status
        aggregate["last_ended_at"] = recorded_at
        aggregate["duration_sec"] = float(aggregate.get("duration_sec") or 0.0) + float(duration_sec or 0.0)
        aggregate["tool_calls"] = int(aggregate.get("tool_calls") or 0) + int(tool_calls or 0)
        aggregate["input_tokens"] = _merge_optional_number(aggregate.get("input_tokens"), input_tokens, as_int=True)
        aggregate["output_tokens"] = _merge_optional_number(aggregate.get("output_tokens"), output_tokens, as_int=True)
        aggregate["cost_usd"] = _merge_optional_number(aggregate.get("cost_usd"), cost_usd, as_int=False)
        return doc

    def _apply_unit_start(self, doc: Dict[str, Any], *, unit_id: str, phase: Optional[str], recorded_at: float) -> Dict[str, Any]:
        unit = self._ensure_unit(doc, unit_id, phase=phase)
        if unit.get("started_at") is None:
            unit["started_at"] = recorded_at
        unit["phase"] = phase
        unit["status"] = "running"
        return doc

    def _apply_unit_end(
        self,
        doc: Dict[str, Any],
        *,
        unit_id: str,
        phase: Optional[str],
        recorded_at: float,
        status: str,
        duration_sec: Optional[float],
        tool_calls: Optional[int],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        cost_usd: Optional[float],
    ) -> Dict[str, Any]:
        unit = self._ensure_unit(doc, unit_id, phase=phase)
        started_at = _optional_float(unit.get("started_at"))
        resolved_duration = duration_sec
        if resolved_duration is None:
            if started_at is not None:
                resolved_duration = max(0.0, recorded_at - started_at)
            else:
                resolved_duration = 0.0
        unit["phase"] = phase
        unit["status"] = status
        unit["ended_at"] = recorded_at
        unit["duration_sec"] = float(resolved_duration)
        unit["tool_calls"] = int(tool_calls or 0)
        unit["input_tokens"] = input_tokens
        unit["output_tokens"] = output_tokens
        unit["cost_usd"] = cost_usd
        if not bool(unit.get("totals_counted")):
            totals = doc["totals"]
            totals["units"] = int(totals.get("units") or 0) + 1
            totals["duration_sec"] = float(totals.get("duration_sec") or 0.0) + float(resolved_duration)
            totals["tool_calls"] = int(totals.get("tool_calls") or 0) + int(tool_calls or 0)
            totals["input_tokens"] = _merge_optional_number(totals.get("input_tokens"), input_tokens, as_int=True)
            totals["output_tokens"] = _merge_optional_number(totals.get("output_tokens"), output_tokens, as_int=True)
            totals["cost_usd"] = _merge_optional_number(totals.get("cost_usd"), cost_usd, as_int=False)
            unit["totals_counted"] = True
        return doc

    def _increment_counter(
        self,
        doc: Dict[str, Any],
        *,
        total_key: str,
        unit_key: Optional[str],
        unit_field: str,
        phase: Optional[str],
    ) -> Dict[str, Any]:
        totals = doc["totals"]
        totals[total_key] = int(totals.get(total_key) or 0) + 1
        if unit_key:
            unit = self._ensure_unit(doc, unit_key, phase=phase)
            unit[unit_field] = int(unit.get(unit_field) or 0) + 1
        return doc

    def _apply_runtime_progress(self, doc: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "mode_id": _clean_optional_text(event.get("mode_id"), max_len=64),
            "source": _clean_optional_text(event.get("source"), max_len=64),
            "phase": _clean_optional_text(event.get("phase"), max_len=64),
            "status": _clean_optional_text(event.get("status"), max_len=32),
            "message": _clean_optional_text(event.get("message"), max_len=280),
            "ts": _optional_float(event.get("ts")) or _now_ts(),
        }
        runtime_progress = doc.setdefault("runtime_progress", {"events": 0, "last_event": None})
        runtime_progress["events"] = int(runtime_progress.get("events") or 0) + 1
        runtime_progress["last_event"] = payload
        return doc

    @staticmethod
    def _ensure_phase_aggregate(doc: Dict[str, Any], phase: str) -> Dict[str, Any]:
        items = doc.setdefault("phase_aggregates", [])
        for item in items:
            if isinstance(item, dict) and str(item.get("phase") or "") == phase:
                return item
        record = {
            "phase": phase,
            "starts": 0,
            "ends": 0,
            "duration_sec": 0.0,
            "tool_calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "last_status": None,
            "last_started_at": None,
            "last_ended_at": None,
        }
        items.append(record)
        return record

    @staticmethod
    def _ensure_unit(doc: Dict[str, Any], unit_id: str, *, phase: Optional[str]) -> Dict[str, Any]:
        items = doc.setdefault("units", [])
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("unit_id") or "") != unit_id:
                continue
            item_phase = _clean_optional_text(item.get("phase"), max_len=64)
            if phase is None or item_phase in (None, phase):
                return item
        record = {
            "unit_id": unit_id,
            "phase": phase,
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "duration_sec": 0.0,
            "tool_calls": 0,
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "retries": 0,
            "recovery_attempts": 0,
            "skill_selection_count": 0,
            "skill_discovery_count": 0,
            "skill_install_count": 0,
            "totals_counted": False,
        }
        items.append(record)
        return record
