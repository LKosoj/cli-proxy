from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from modes.sdk.file_lock import lock_file, unlock_file
from modes.sdk.json_store import read_json_locked, read_json_locked_if_exists, update_json_locked, write_json_locked
from modes.sdk.runtime.json_normalizer import loads_safe
from session import session_runtime_uid
from utils.paths import cli_proxy_artifact_path


def _safe_path_token(value: Any, *, fallback: str) -> str:
    token = str(value or "").strip()
    token = token.replace("/", "_").replace("\\", "_")
    return token or fallback


def _now_ts() -> float:
    return float(time.time())


def _run_started_at(run_dir: str) -> float:
    """Читает started_at из STATE.json прогона; при ошибке — mtime каталога; при OSError — 0.0."""
    state_path = os.path.join(run_dir, "STATE.json")
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = data.get("started_at")
        if value is not None:
            return float(value)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        return float(os.path.getmtime(run_dir))
    except OSError:
        return 0.0


def _sortable_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{secrets.token_hex(4)}"


@dataclass(frozen=True)
class RunArtifactHandle:
    root_dir: str
    session_uid: str
    mode_id: str
    run_id: str
    run_dir: str
    state_path: str
    plan_path: str
    checkpoints_path: str
    recovery_path: str
    metrics_path: str
    events_path: str
    artifacts_dir: str
    scratch_dir: str


def is_terminal_status(status: Any) -> bool:
    token = str(status or "").strip().lower()
    return token in RunArtifactStore.TERMINAL_STATUSES


class RunArtifactStore:
    STATE_VERSION = 1
    TERMINAL_STATUSES = frozenset({"aborted", "canceled", "cancelled", "completed", "failed", "superseded", "terminated"})

    def __init__(self, config: Any):
        self.config = config

    def start_run(
        self,
        *,
        session: Any,
        mode_id: str,
        run_id: Optional[str] = None,
        phase: Optional[str] = None,
        source_prompt_hash: Optional[str] = None,
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> RunArtifactHandle:
        handle = self._build_handle(session=session, mode_id=mode_id, run_id=run_id)
        os.makedirs(handle.run_dir, exist_ok=True)
        os.makedirs(handle.artifacts_dir, exist_ok=True)
        os.makedirs(handle.scratch_dir, exist_ok=True)

        started_at = _now_ts()
        write_json_locked(
            handle.state_path,
            self._state_default(
                handle,
                started_at=started_at,
                phase=phase,
                source_prompt_hash=source_prompt_hash,
                mode_context=mode_context,
            ),
        )
        write_json_locked(handle.plan_path, self._plan_default(handle))
        write_json_locked(handle.checkpoints_path, self._checkpoints_default(handle))
        write_json_locked(handle.recovery_path, self._recovery_default(handle))
        write_json_locked(handle.metrics_path, self._metrics_default(handle))
        self._touch_events_file(handle.events_path)
        return handle

    def latest_run(self, *, session: Any, mode_id: str) -> Optional[RunArtifactHandle]:
        runs = self.list_mode_runs(session=session, mode_id=mode_id)
        return runs[0] if runs else None

    def get_run(self, *, session: Any, mode_id: str, run_id: str) -> Optional[RunArtifactHandle]:
        handle = self._build_handle(session=session, mode_id=mode_id, run_id=run_id)
        if os.path.exists(handle.state_path):
            return handle
        return None

    def list_runs(
        self,
        *,
        session: Any,
        mode_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[RunArtifactHandle]:
        root_dir = self._resolve_root_dir(session)
        session_uid = self._resolve_session_uid(session)
        session_root = cli_proxy_artifact_path(root_dir, f"runs/{session_uid}")
        if not os.path.isdir(session_root):
            return []
        resolved_limit = max(1, int(limit or 20))
        mode_tokens: list[str] = []
        if str(mode_id or "").strip():
            mode_tokens.append(_safe_path_token(mode_id, fallback="unknown-mode"))
        else:
            mode_tokens.extend(
                sorted(
                    name
                    for name in os.listdir(session_root)
                    if os.path.isdir(os.path.join(session_root, name))
                )
            )
        handles: list[RunArtifactHandle] = []
        for mode_token in mode_tokens:
            handles.extend(self._list_mode_run_handles(session=session, mode_token=mode_token))
        handles.sort(key=lambda item: (_run_started_at(item.run_dir), item.run_id), reverse=True)
        return handles[:resolved_limit]

    def list_mode_runs(self, *, session: Any, mode_id: str) -> List[RunArtifactHandle]:
        """Возвращает все прогоны mode от новых к старым по STATE.json.started_at."""
        mode_token = _safe_path_token(mode_id, fallback="unknown-mode")
        return self._list_mode_run_handles(session=session, mode_token=mode_token)

    def load_state(self, run: RunArtifactHandle) -> Dict[str, Any]:
        payload = read_json_locked(run.state_path, default=self._state_default(run))
        return payload if isinstance(payload, dict) else self._state_default(run)

    def load_plan(self, run: RunArtifactHandle) -> Dict[str, Any]:
        payload = read_json_locked_if_exists(run.plan_path, default=self._plan_default(run))
        return payload if isinstance(payload, dict) else self._plan_default(run)

    def load_checkpoints(self, run: RunArtifactHandle) -> Dict[str, Any]:
        payload = read_json_locked_if_exists(run.checkpoints_path, default=self._checkpoints_default(run))
        return payload if isinstance(payload, dict) else self._checkpoints_default(run)

    def load_recovery(self, run: RunArtifactHandle) -> Dict[str, Any]:
        payload = read_json_locked_if_exists(run.recovery_path, default=self._recovery_default(run))
        return payload if isinstance(payload, dict) else self._recovery_default(run)

    def load_metrics(self, run: RunArtifactHandle) -> Dict[str, Any]:
        payload = read_json_locked_if_exists(run.metrics_path, default=self._metrics_default(run))
        return payload if isinstance(payload, dict) else self._metrics_default(run)

    def load_events_tail(self, run: RunArtifactHandle, *, limit: int = 20) -> List[Dict[str, Any]]:
        if not os.path.exists(run.events_path):
            return []
        resolved_limit = max(1, int(limit or 20))
        try:
            with open(run.events_path, "r", encoding="utf-8") as handle:
                lines = deque((line.strip() for line in handle if line.strip()), maxlen=resolved_limit)
        except Exception:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                payload = loads_safe(line, strict_first=False)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def save_state(self, run: RunArtifactHandle, state: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._state_default(run)
        existing = self.load_state(run)
        if isinstance(existing, dict):
            payload.update(existing)
        payload.update(dict(state or {}))
        payload["version"] = self.STATE_VERSION
        payload["run_id"] = run.run_id
        payload["session_uid"] = run.session_uid
        payload["mode_id"] = run.mode_id
        payload["updated_at"] = _now_ts()
        write_json_locked(run.state_path, payload)
        return payload

    def save_plan(self, run: RunArtifactHandle, plan: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._plan_default(run)
        payload.update(dict(plan or {}))
        payload["version"] = self.STATE_VERSION
        payload["mode_id"] = run.mode_id
        write_json_locked(run.plan_path, payload)
        return payload

    def append_checkpoint(self, run: RunArtifactHandle, checkpoint: Dict[str, Any]) -> Dict[str, Any]:
        appended: Dict[str, Any] = {}

        def _updater(current: Dict[str, Any]) -> Dict[str, Any]:
            items = list(current.get("items") or [])
            record = dict(checkpoint or {})
            record.setdefault("index", len(items) + 1)
            items.append(record)
            current["version"] = self.STATE_VERSION
            current["run_id"] = run.run_id
            current["items"] = items
            appended.clear()
            appended.update(record)
            return current

        update_json_locked(run.checkpoints_path, _updater, default=self._checkpoints_default(run))
        return dict(appended)

    def save_recovery(self, run: RunArtifactHandle, recovery: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._merge_recovery_payload(self._recovery_default(run), self.load_recovery(run))
        payload = self._merge_recovery_payload(payload, dict(recovery or {}))
        payload["version"] = self.STATE_VERSION
        write_json_locked(run.recovery_path, payload)
        return payload

    def save_metrics(self, run: RunArtifactHandle, metrics: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._metrics_default(run)
        payload.update(dict(metrics or {}))
        payload["version"] = self.STATE_VERSION
        payload["run_id"] = run.run_id
        write_json_locked(run.metrics_path, payload)
        return payload

    def append_event(self, run: RunArtifactHandle, event: Dict[str, Any]) -> Dict[str, Any]:
        record = {"version": self.STATE_VERSION, "ts": _now_ts()}
        record.update(dict(event or {}))
        parent = os.path.dirname(run.events_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(run.events_path, "a+", encoding="utf-8") as handle:
            lock_file(handle, shared=False)
            try:
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                unlock_file(handle)
        return record

    def mark_finished(
        self,
        run: RunArtifactHandle,
        *,
        status: str = "completed",
        phase: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = self.load_state(run)
        finished_at = _now_ts()
        state["status"] = str(status or "completed")
        state["finished_at"] = finished_at
        state["updated_at"] = finished_at
        if phase is not None:
            state["phase"] = phase
        write_json_locked(run.state_path, state)
        return state

    def prune_old_runs(
        self,
        *,
        session: Any | None = None,
        root_dir: Optional[str] = None,
        dry_run: bool = False,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        resolved_root_dir = self._resolve_prune_root_dir(session=session, root_dir=root_dir)
        retention_days = self._retention_days()
        cutoff_ts = float(now_ts if now_ts is not None else _now_ts()) - (float(retention_days) * 86400.0)
        runs_root = cli_proxy_artifact_path(resolved_root_dir, "runs")
        report: Dict[str, Any] = {
            "root_dir": resolved_root_dir,
            "runs_root": runs_root,
            "retention_days": retention_days,
            "cutoff_ts": cutoff_ts,
            "dry_run": bool(dry_run),
            "checked_runs": 0,
            "deleted": [],
            "would_delete": [],
            "shielded": [],
            "kept": [],
            "errors": [],
        }
        if not os.path.isdir(runs_root):
            return report

        for session_uid in sorted(os.listdir(runs_root)):
            session_root = os.path.join(runs_root, session_uid)
            if not os.path.isdir(session_root):
                continue
            for mode_token in sorted(os.listdir(session_root)):
                mode_root = os.path.join(session_root, mode_token)
                if not os.path.isdir(mode_root):
                    continue
                for run_id in sorted(os.listdir(mode_root)):
                    run_dir = os.path.join(mode_root, run_id)
                    if not os.path.isdir(run_dir):
                        continue
                    if not os.path.exists(os.path.join(run_dir, "STATE.json")):
                        continue
                    run = self._build_handle_from_tokens(
                        root_dir=resolved_root_dir,
                        session_uid=session_uid,
                        mode_id=mode_token,
                        run_id=run_id,
                    )
                    state = self.load_state(run)
                    status = str(state.get("status") or "").strip().lower() or "running"
                    last_modified_ts = self._run_modified_ts(run.run_dir)
                    record = {
                        "session_uid": run.session_uid,
                        "mode_id": run.mode_id,
                        "run_id": run.run_id,
                        "run_dir": run.run_dir,
                        "status": status,
                        "last_modified_ts": last_modified_ts,
                    }
                    report["checked_runs"] = int(report["checked_runs"]) + 1

                    if last_modified_ts >= cutoff_ts:
                        report["kept"].append({**record, "reason": "retention_window"})
                        continue
                    if status == "running":
                        report["shielded"].append({**record, "reason": "running_state"})
                        continue
                    if not is_terminal_status(status):
                        report["kept"].append({**record, "reason": "non_terminal_status"})
                        continue
                    if dry_run:
                        report["would_delete"].append({**record, "reason": "expired_terminal_run"})
                        continue
                    try:
                        shutil.rmtree(run.run_dir)
                        self._cleanup_empty_parents(run)
                        report["deleted"].append({**record, "reason": "expired_terminal_run"})
                    except Exception as exc:
                        report["errors"].append({**record, "error": str(exc)})
        return report

    def _build_handle(self, *, session: Any, mode_id: str, run_id: Optional[str] = None) -> RunArtifactHandle:
        root_dir = self._resolve_root_dir(session)
        session_uid = self._resolve_session_uid(session)
        return self._build_handle_from_tokens(
            root_dir=root_dir,
            session_uid=session_uid,
            mode_id=mode_id,
            run_id=run_id,
        )

    def _list_mode_run_handles(self, *, session: Any, mode_token: str) -> List[RunArtifactHandle]:
        root_dir = self._resolve_root_dir(session)
        session_uid = self._resolve_session_uid(session)
        mode_root = cli_proxy_artifact_path(root_dir, f"runs/{session_uid}/{mode_token}")
        if not os.path.isdir(mode_root):
            return []
        handles: list[RunArtifactHandle] = []
        for run_id in os.listdir(mode_root):
            run_dir = os.path.join(mode_root, run_id)
            if not os.path.isdir(run_dir):
                continue
            if not os.path.exists(os.path.join(run_dir, "STATE.json")):
                continue
            handles.append(
                self._build_handle_from_tokens(
                    root_dir=root_dir,
                    session_uid=session_uid,
                    mode_id=mode_token,
                    run_id=run_id,
                )
            )
        handles.sort(key=lambda item: (_run_started_at(item.run_dir), item.run_id), reverse=True)
        return handles

    def _build_handle_from_tokens(
        self,
        *,
        root_dir: str,
        session_uid: str,
        mode_id: str,
        run_id: Optional[str] = None,
    ) -> RunArtifactHandle:
        mode_token = _safe_path_token(mode_id, fallback="unknown-mode")
        resolved_run_id = _safe_path_token(run_id, fallback=_sortable_run_id())
        run_dir = cli_proxy_artifact_path(root_dir, f"runs/{session_uid}/{mode_token}/{resolved_run_id}")
        return RunArtifactHandle(
            root_dir=root_dir,
            session_uid=session_uid,
            mode_id=str(mode_id or "").strip() or "unknown-mode",
            run_id=resolved_run_id,
            run_dir=run_dir,
            state_path=os.path.join(run_dir, "STATE.json"),
            plan_path=os.path.join(run_dir, "PLAN.json"),
            checkpoints_path=os.path.join(run_dir, "CHECKPOINTS.json"),
            recovery_path=os.path.join(run_dir, "RECOVERY.json"),
            metrics_path=os.path.join(run_dir, "METRICS.json"),
            events_path=os.path.join(run_dir, "EVENTS.jsonl"),
            artifacts_dir=os.path.join(run_dir, "artifacts"),
            scratch_dir=os.path.join(run_dir, "scratch"),
        )

    def _resolve_prune_root_dir(self, *, session: Any | None = None, root_dir: Optional[str] = None) -> str:
        explicit_root = str(root_dir or "").strip()
        if explicit_root:
            return os.path.abspath(explicit_root)
        if session is not None:
            return self._resolve_root_dir(session)
        defaults = getattr(self.config, "defaults", None)
        config_workdir = str(getattr(defaults, "workdir", "") or "").strip()
        if config_workdir:
            return os.path.abspath(config_workdir)
        return os.path.abspath(os.getcwd())

    def _retention_days(self) -> int:
        defaults = getattr(self.config, "defaults", None)
        raw_value = int(getattr(defaults, "run_artifacts_retention_days", 30) or 30)
        return max(0, raw_value)

    @staticmethod
    def _run_modified_ts(run_dir: str) -> float:
        latest_ts = 0.0
        for current_root, dir_names, file_names in os.walk(run_dir):
            try:
                latest_ts = max(latest_ts, float(os.path.getmtime(current_root)))
            except OSError:
                continue
            for name in list(dir_names) + list(file_names):
                path = os.path.join(current_root, name)
                try:
                    latest_ts = max(latest_ts, float(os.path.getmtime(path)))
                except OSError:
                    continue
        return latest_ts

    @staticmethod
    def _cleanup_empty_parents(run: RunArtifactHandle) -> None:
        mode_root = os.path.dirname(run.run_dir)
        session_root = os.path.dirname(mode_root)
        for candidate in (mode_root, session_root):
            if not os.path.isdir(candidate):
                continue
            try:
                if not os.listdir(candidate):
                    os.rmdir(candidate)
            except OSError:
                continue

    def _resolve_root_dir(self, session: Any) -> str:
        session_workdir = str(getattr(session, "workdir", "") or "").strip()
        if session_workdir:
            return os.path.abspath(session_workdir)
        defaults = getattr(self.config, "defaults", None)
        config_workdir = str(getattr(defaults, "workdir", "") or "").strip()
        if config_workdir:
            return os.path.abspath(config_workdir)
        return os.path.abspath(os.getcwd())

    @staticmethod
    def _resolve_session_uid(session: Any) -> str:
        return _safe_path_token(session_runtime_uid(session), fallback="unknown-session")

    @staticmethod
    def _touch_events_file(path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as handle:
            lock_file(handle, shared=False)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                unlock_file(handle)

    def _state_default(
        self,
        run: RunArtifactHandle,
        *,
        started_at: Optional[float] = None,
        phase: Optional[str] = None,
        source_prompt_hash: Optional[str] = None,
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ts = float(started_at if started_at is not None else _now_ts())
        return {
            "version": self.STATE_VERSION,
            "run_id": run.run_id,
            "session_uid": run.session_uid,
            "mode_id": run.mode_id,
            "status": "running",
            "phase": phase,
            "started_at": ts,
            "updated_at": ts,
            "finished_at": None,
            "current_unit_id": None,
            "current_step_id": None,
            "checkpoint_index": 0,
            "last_successful_phase": None,
            "resume_token": None,
            "source_prompt_hash": source_prompt_hash,
            "mode_context": dict(mode_context or {}),
        }

    def _plan_default(self, run: RunArtifactHandle) -> Dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "mode_id": run.mode_id,
            "plan_kind": "mode_run",
            "task_family": "",
            "units": [],
            "boundary_map": [],
            "validation_contracts": [],
        }

    def _checkpoints_default(self, run: RunArtifactHandle) -> Dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "run_id": run.run_id,
            "items": [],
        }

    def _recovery_default(self, _run: RunArtifactHandle) -> Dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "status": "ok",
            "diagnosed_at": None,
            "recommended_action": "",
            "last_consistent_checkpoint": 0,
            "issues": [],
            "attempts": [],
            "can_resume": False,
        }

    def _merge_recovery_payload(self, base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base if isinstance(base, dict) else {})
        if not isinstance(updates, dict):
            return merged
        for key, value in updates.items():
            existing = merged.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                merged[key] = self._merge_recovery_payload(existing, value)
                continue
            if key in {"last_requested_operation"} and existing is not None and not isinstance(value, dict):
                continue
            if key in {"requested_operations", "attempts"} and existing is not None and not isinstance(value, list):
                continue
            merged[key] = value
        return merged

    def _metrics_default(self, run: RunArtifactHandle) -> Dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
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
            },
            "units": [],
            "phase_aggregates": [],
        }
