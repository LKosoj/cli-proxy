from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from glob import glob
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence

from modes.sdk.json_store import read_json_locked_if_exists
from modes.sdk.planning import load_plan
from modes.sdk.runtime.final_qc import runtime_readiness_allows_finalization
from modes.sdk.runtime.json_normalizer import loads_safe
from sessions.scoped_key import is_session_scoped_key, sanitize_scoped_key_token

from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from app.services.run_boundary_validation_service import RunBoundaryValidationService
from app.services.run_utils import (
    MISSING as _MISSING,
    as_list_of_strings as _as_list_of_strings,
    clean_optional_text as _clean_optional_text,
    clean_text as _clean_text,
    nested_get as _nested_get,
)


DoctorAction = Literal[
    "no_action",
    "resume_same_phase",
    "replay_finalize",
    "rollback_to_checkpoint",
    "restart_from_phase",
    "mark_failed",
    "rerun_same_operation",
    "run_validate",
    "run_repair",
    "manual_review_required",
]

_FINALIZE_PHASES = {"review", "validation", "complete"}
_PLAN_REQUIRED_PHASES = {"plan", "develop", "review", "execute", "validation", "complete", "dev"}
_METRICS_REQUIRED_PHASES = {"execute", "develop", "review", "validation", "complete", "dev"}
_EVENTS_REQUIRED_PHASES = {"execute", "validation", "complete"}
_MODE_RECOVERY_ACTIONS: Dict[str, frozenset[str]] = {
    "admin": frozenset(),
    "agent": frozenset({"rollback_to_checkpoint", "restart_from_phase"}),
    "analyst": frozenset({"rollback_to_checkpoint", "restart_from_phase"}),
    "manager": frozenset({"replay_finalize"}),
    "webmaster": frozenset({"rollback_to_checkpoint", "restart_from_phase", "replay_finalize"}),
}
_ACTIVE_RECOVERY_ACTIONS = frozenset({"rollback_to_checkpoint", "restart_from_phase", "replay_finalize"})


def _manager_scoped_key_from_state(state: Dict[str, Any]) -> Optional[str]:
    execution_context = _nested_get(state, "mode_context.execution_context")
    if not isinstance(execution_context, dict):
        return None
    token = sanitize_scoped_key_token(execution_context.get("session_scoped_key"))
    if token and is_session_scoped_key(token):
        return token
    return None


@dataclass(frozen=True)
class RunDoctorIssue:
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RunDoctorReport:
    mode_id: str
    phase: str
    status: Literal["ok", "needs_recovery"]
    issues: List[RunDoctorIssue]
    recommended_action: DoctorAction
    can_resume: bool
    diagnosed_at: float
    last_consistent_checkpoint: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode_id": self.mode_id,
            "phase": self.phase,
            "status": self.status,
            "issues": [item.to_dict() for item in self.issues],
            "recommended_action": self.recommended_action,
            "can_resume": self.can_resume,
            "diagnosed_at": self.diagnosed_at,
            "last_consistent_checkpoint": self.last_consistent_checkpoint,
        }


@dataclass(frozen=True)
class _LoadedEvents:
    items: List[Dict[str, Any]]
    malformed: bool = False


@dataclass(frozen=True)
class _DoctorDocs:
    state_exists: bool
    plan_exists: bool
    checkpoints_exists: bool
    metrics_exists: bool
    recovery_exists: bool
    events_exists: bool
    state: Dict[str, Any]
    plan: Dict[str, Any]
    checkpoints: Dict[str, Any]
    metrics: Dict[str, Any]
    recovery: Dict[str, Any]
    events: _LoadedEvents


class RunDoctorService:
    def __init__(
        self,
        *,
        enabled: bool,
        artifact_store: RunArtifactStore,
        boundary_validator: RunBoundaryValidationService,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.artifact_store = artifact_store
        self.boundary_validator = boundary_validator
        self._now = now_fn or time.time

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    def diagnose(
        self,
        run: RunArtifactHandle,
        *,
        mode_id: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> RunDoctorReport:
        docs = self._load_docs(run)
        resolved_mode_id = _clean_text(mode_id or run.mode_id, max_len=64) or run.mode_id
        resolved_phase = _clean_text(phase or docs.state.get("phase"), max_len=64) or "unknown"
        diagnosed_at = float(self._now())

        if not self.is_enabled():
            report = RunDoctorReport(
                mode_id=resolved_mode_id,
                phase=resolved_phase,
                status="ok",
                issues=[],
                recommended_action="no_action",
                can_resume=False,
                diagnosed_at=diagnosed_at,
                last_consistent_checkpoint=self._last_consistent_checkpoint(docs),
            )
            self._persist_recovery(run, report)
            return report

        issues: List[RunDoctorIssue] = []
        self._collect_generic_issues(run, docs, resolved_phase, issues)
        self._collect_boundary_issues(run, resolved_mode_id, resolved_phase, issues)
        self._run_mode_comparator(run, docs, resolved_mode_id, resolved_phase, issues)
        deduped = self._dedupe_issues(issues)
        action = self.recommend_action(issues=deduped, phase=resolved_phase, mode_id=resolved_mode_id)
        action = self._constrain_action_by_mode(mode_id=resolved_mode_id, action=action)
        report = RunDoctorReport(
            mode_id=resolved_mode_id,
            phase=resolved_phase,
            status="ok" if not deduped else "needs_recovery",
            issues=deduped,
            recommended_action=action,
            can_resume=self._can_resume(mode_id=resolved_mode_id, action=action),
            diagnosed_at=diagnosed_at,
            last_consistent_checkpoint=self._last_consistent_checkpoint(docs),
        )
        self._persist_recovery(run, report)
        return report

    def recommend_action(self, *, issues: Sequence[RunDoctorIssue], phase: str, mode_id: str = "") -> DoctorAction:
        codes = {issue.code for issue in issues}
        phase_key = _clean_text(phase, max_len=64)
        if mode_id == "codebase_mapper":
            if not codes:
                return "no_action"
            if "manual_review_pending" in codes:
                return "manual_review_required"
            if "validation_failed" in codes:
                return "run_repair"
            if "graph_corrupted" in codes:
                return "run_validate"
            if "missing_state" in codes:
                return "rerun_same_operation"
            return "rerun_same_operation"
        if mode_id == "admin":
            if not codes:
                return "no_action"
            if "admin_destructive_execution_requires_confirmation" in codes:
                return "manual_review_required"
            return "restart_from_phase"

        if not codes:
            return "no_action"
        replay_snapshot_reasons = {
            _clean_text(issue.details.get("reason"), max_len=96)
            for issue in issues
            if issue.code == "legacy_store_mismatch"
        }
        if mode_id == "manager" and any(reason.startswith("replay_snapshot_") for reason in replay_snapshot_reasons):
            return "manual_review_required"
        if "missing_state" in codes:
            return "mark_failed"
        if "missing_plan" in codes or "checkpoint_gap" in codes:
            return "rollback_to_checkpoint"
        if phase_key in _FINALIZE_PHASES and (
            "legacy_store_mismatch" in codes or "boundary_contract_failed" in codes
        ):
            return "replay_finalize"
        if "legacy_store_mismatch" in codes or "orchestrator_invariant_mismatch" in codes:
            return "restart_from_phase"
        if "missing_metrics" in codes or "missing_events" in codes:
            return "resume_same_phase"
        return "mark_failed"

    def _can_resume(self, *, mode_id: str, action: DoctorAction) -> bool:
        if action == "no_action":
            return mode_id != "codebase_mapper"
        return action == "resume_same_phase"

    def _constrain_action_by_mode(self, *, mode_id: str, action: DoctorAction) -> DoctorAction:
        if action not in _ACTIVE_RECOVERY_ACTIONS:
            return action
        supported_actions = _MODE_RECOVERY_ACTIONS.get(mode_id)
        if supported_actions is None or action in supported_actions:
            return action
        return "manual_review_required"

    def _load_docs(self, run: RunArtifactHandle) -> _DoctorDocs:
        state_exists = os.path.exists(run.state_path)
        plan_exists = os.path.exists(run.plan_path)
        checkpoints_exists = os.path.exists(run.checkpoints_path)
        metrics_exists = os.path.exists(run.metrics_path)
        recovery_exists = os.path.exists(run.recovery_path)
        events_exists = os.path.exists(run.events_path)
        state = read_json_locked_if_exists(run.state_path, default={})
        plan = read_json_locked_if_exists(run.plan_path, default={})
        checkpoints = read_json_locked_if_exists(run.checkpoints_path, default={})
        metrics = read_json_locked_if_exists(run.metrics_path, default={})
        recovery = read_json_locked_if_exists(run.recovery_path, default={})
        return _DoctorDocs(
            state_exists=state_exists,
            plan_exists=plan_exists,
            checkpoints_exists=checkpoints_exists,
            metrics_exists=metrics_exists,
            recovery_exists=recovery_exists,
            events_exists=events_exists,
            state=state if isinstance(state, dict) else {},
            plan=plan if isinstance(plan, dict) else {},
            checkpoints=checkpoints if isinstance(checkpoints, dict) else {},
            metrics=metrics if isinstance(metrics, dict) else {},
            recovery=recovery if isinstance(recovery, dict) else {},
            events=self._load_events(run.events_path),
        )

    def _load_events(self, path: str) -> _LoadedEvents:
        if not os.path.exists(path):
            return _LoadedEvents(items=[], malformed=False)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle.readlines() if line.strip()]
        except Exception:
            return _LoadedEvents(items=[], malformed=True)
        events: List[Dict[str, Any]] = []
        malformed = False
        for line in lines:
            try:
                payload = loads_safe(line, strict_first=False)
            except Exception:
                payload = None
                malformed = True
            if isinstance(payload, dict):
                events.append(payload)
                continue
            malformed = True
        return _LoadedEvents(items=events, malformed=malformed)

    def _collect_generic_issues(
        self,
        run: RunArtifactHandle,
        docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if not docs.state or not _clean_optional_text(docs.state.get("phase"), max_len=64):
            issues.append(
                RunDoctorIssue(
                    code="missing_state",
                    message="STATE.json is missing or does not contain a valid phase.",
                )
            )
        if phase in _PLAN_REQUIRED_PHASES and not docs.plan_exists:
            issues.append(
                RunDoctorIssue(
                    code="missing_plan",
                    message=f"PLAN.json is missing while run already entered `{phase}` phase.",
                    details={"path": run.plan_path},
                )
            )
        checkpoint_items = docs.checkpoints.get("items") if isinstance(docs.checkpoints, dict) else []
        checkpoint_count = len(checkpoint_items) if isinstance(checkpoint_items, list) else 0
        checkpoint_index_raw = docs.state.get("checkpoint_index") if isinstance(docs.state, dict) else 0
        try:
            checkpoint_index = int(checkpoint_index_raw or 0)
        except Exception:
            checkpoint_index = 0
        if checkpoint_index > checkpoint_count:
            issues.append(
                RunDoctorIssue(
                    code="checkpoint_gap",
                    message="STATE checkpoint_index points past serialized CHECKPOINTS.json items.",
                    details={"checkpoint_index": checkpoint_index, "serialized_items": checkpoint_count},
                )
            )
        if phase in _METRICS_REQUIRED_PHASES and not docs.metrics_exists:
            issues.append(
                RunDoctorIssue(
                    code="missing_metrics",
                    message=f"METRICS.json is missing while run is in `{phase}` phase.",
                    details={"path": run.metrics_path},
                )
            )
        if phase in _EVENTS_REQUIRED_PHASES and not docs.events_exists:
            issues.append(
                RunDoctorIssue(
                    code="missing_events",
                    message=f"EVENTS.jsonl is missing while run is in `{phase}` phase.",
                    details={"path": run.events_path},
                )
            )
        elif phase in _EVENTS_REQUIRED_PHASES and docs.events.malformed:
            issues.append(
                RunDoctorIssue(
                    code="events_malformed",
                    message=f"EVENTS.jsonl is malformed while run is in `{phase}` phase.",
                    details={"path": run.events_path},
                )
            )

    def _collect_boundary_issues(
        self,
        run: RunArtifactHandle,
        mode_id: str,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        report = self.boundary_validator.validate(run, mode_id=mode_id, phase=phase)
        if report.status == "ok":
            return
        report_items = list(report.issues or [])
        if mode_id == "analyst" and phase == "complete":
            report_items = [
                item
                for item in report_items
                if _clean_text(item.code, max_len=96) != "analyst_quality_gate_not_passed"
            ]
            if not report_items:
                return
        for item in report_items:
            if _clean_text(item.code, max_len=96) != "webmaster_gate_payload_degraded":
                continue
            issues.append(
                RunDoctorIssue(
                    code="boundary_payload_degraded",
                    message=_clean_text(item.message, max_len=512)
                    or "Boundary validation detected degraded payload rows.",
                    severity="warning",
                    details=dict(getattr(item, "details", {}) or {}),
                )
            )
        issues.append(
            RunDoctorIssue(
                code="boundary_contract_failed",
                message="Boundary validation reported inconsistencies for the current phase.",
                details={"boundary_issue_codes": [item.code for item in report_items]},
            )
        )

    def _run_mode_comparator(
        self,
        run: RunArtifactHandle,
        docs: _DoctorDocs,
        mode_id: str,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        comparator = getattr(self, f"_compare_{_clean_text(mode_id, max_len=64)}", None)
        if callable(comparator):
            comparator(run, docs, phase, issues)

    def _compare_manager(
        self,
        run: RunArtifactHandle,
        docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if phase not in {"plan", "develop", "review", "complete"}:
            return
        replay_snapshot = self._manager_replay_finalize_snapshot(docs)
        if replay_snapshot is not None:
            self._compare_manager_replay_snapshot(docs, replay_snapshot, issues)
            return
        sync_status = _clean_optional_text(_nested_get(docs.state, "mode_context.legacy_plan_sync_status"), max_len=32)
        legacy_plan = load_plan(run.root_dir, scoped_key=_manager_scoped_key_from_state(docs.state))
        if legacy_plan is None and phase == "complete" and sync_status == "archived":
            return
        if legacy_plan is None:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Legacy MANAGER_PLAN.json is missing for manager run comparison.",
                    details={"store": "MANAGER_PLAN.json", "reason": "missing"},
                )
            )
            return
        sync_payload = docs.plan.get("legacy_plan_sync") if isinstance(docs.plan, dict) else None
        if not isinstance(sync_payload, dict) or sync_payload.get("synced") is not True:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level PLAN.json is not synchronized with MANAGER_PLAN.json.",
                    details={"store": "MANAGER_PLAN.json", "reason": "sync_flag_missing"},
                )
            )
            return
        state_sync = _nested_get(docs.state, "mode_context.legacy_plan_sync")
        if isinstance(state_sync, dict) and state_sync != sync_payload:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level STATE.json echo does not match PLAN.json legacy sync payload.",
                    details={"store": "STATE.json", "reason": "state_plan_echo_mismatch"},
                )
            )
        expected_task_count = len(legacy_plan.tasks)
        actual_task_count = int(sync_payload.get("task_count") or 0)
        if actual_task_count != expected_task_count:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level PLAN.json conflicts with MANAGER_PLAN.json task count.",
                    details={
                        "store": "MANAGER_PLAN.json",
                        "reason": "task_count_mismatch",
                        "legacy_task_count": expected_task_count,
                        "run_task_count": actual_task_count,
                    },
                )
            )
        legacy_updated_at = _clean_optional_text(getattr(legacy_plan, "updated_at", ""), max_len=64) or ""
        mirrored_updated_at = _clean_optional_text(sync_payload.get("legacy_updated_at"), max_len=64) or ""
        if legacy_updated_at and mirrored_updated_at and legacy_updated_at != mirrored_updated_at:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level PLAN.json lags behind MANAGER_PLAN.json updates.",
                    details={
                        "store": "MANAGER_PLAN.json",
                        "reason": "plan_lagging_behind_legacy",
                        "legacy_updated_at": legacy_updated_at,
                        "run_plan_updated_at": mirrored_updated_at,
                    },
                )
            )
        legacy_status = _clean_optional_text(getattr(legacy_plan, "status", ""), max_len=64) or ""
        mirrored_status = _clean_optional_text(sync_payload.get("legacy_status"), max_len=64) or ""
        if legacy_status and mirrored_status and legacy_status != mirrored_status:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level PLAN.json status does not match MANAGER_PLAN.json.",
                    details={
                        "store": "MANAGER_PLAN.json",
                        "reason": "legacy_status_mismatch",
                        "legacy_status": legacy_status,
                        "run_plan_status": mirrored_status,
                    },
                )
            )
        legacy_current_task = _clean_optional_text(getattr(legacy_plan, "current_task_id", ""), max_len=128) or ""
        mirrored_current_task = _clean_optional_text(sync_payload.get("current_task_id"), max_len=128) or ""
        if legacy_current_task != mirrored_current_task:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level PLAN.json current_task_id does not match MANAGER_PLAN.json.",
                    details={
                        "store": "MANAGER_PLAN.json",
                        "reason": "current_task_id_mismatch",
                        "legacy_current_task_id": legacy_current_task,
                        "run_plan_current_task_id": mirrored_current_task,
                    },
                )
            )
        legacy_completion_report = bool(str(getattr(legacy_plan, "completion_report", "") or "").strip())
        mirrored_completion_report = bool(sync_payload.get("completion_report_present"))
        if legacy_completion_report != mirrored_completion_report:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Run-level PLAN.json completion report marker does not match MANAGER_PLAN.json.",
                    details={
                        "store": "MANAGER_PLAN.json",
                        "reason": "completion_report_mismatch",
                        "legacy_completion_report_present": legacy_completion_report,
                        "run_plan_completion_report_present": mirrored_completion_report,
                    },
                )
            )
        if phase == "complete" and str(getattr(legacy_plan, "status", "") or "").strip().lower() != "completed":
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Manager complete phase expects completed legacy MANAGER_PLAN.json status.",
                    details={
                        "store": "MANAGER_PLAN.json",
                        "reason": "legacy_status_incomplete",
                        "legacy_status": str(getattr(legacy_plan, "status", "") or ""),
                    },
                )
            )

    def _compare_manager_replay_snapshot(
        self,
        docs: _DoctorDocs,
        replay_snapshot: Dict[str, Any],
        issues: List[RunDoctorIssue],
    ) -> None:
        sync_payload = docs.plan.get("legacy_plan_sync") if isinstance(docs.plan, dict) else None
        if not isinstance(sync_payload, dict) or sync_payload.get("synced") is not True:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run lost synchronized legacy plan payload.",
                    details={"reason": "replay_snapshot_sync_flag_missing"},
                )
            )
            return
        state_sync = _nested_get(docs.state, "mode_context.legacy_plan_sync")
        if state_sync is not _MISSING and state_sync != sync_payload:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run STATE echo does not match PLAN legacy sync payload.",
                    details={"reason": "replay_snapshot_state_echo_mismatch"},
                )
            )
        snapshot_status = _clean_optional_text(replay_snapshot.get("status"), max_len=64) or ""
        if snapshot_status != "completed":
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery snapshot must preserve completed manager status.",
                    details={"reason": "replay_snapshot_not_completed", "snapshot_status": snapshot_status},
                )
            )
        if not bool(str(replay_snapshot.get("completion_report") or "").strip()):
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery snapshot must preserve manager completion_report.",
                    details={"reason": "replay_snapshot_completion_report_missing"},
                )
            )
        expected_task_count = len(replay_snapshot.get("tasks") or [])
        actual_task_count = int(sync_payload.get("task_count") or 0)
        if actual_task_count != expected_task_count:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run task_count conflicts with preserved plan snapshot.",
                    details={
                        "reason": "replay_snapshot_task_count_mismatch",
                        "snapshot_task_count": expected_task_count,
                        "run_plan_task_count": actual_task_count,
                    },
                )
            )
        snapshot_legacy_status = snapshot_status
        run_legacy_status = _clean_optional_text(sync_payload.get("legacy_status"), max_len=64) or ""
        if snapshot_legacy_status and run_legacy_status != snapshot_legacy_status:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run legacy_status conflicts with preserved plan snapshot.",
                    details={
                        "reason": "replay_snapshot_status_mismatch",
                        "snapshot_legacy_status": snapshot_legacy_status,
                        "run_plan_status": run_legacy_status,
                    },
                )
            )
        snapshot_current_task_id = _clean_optional_text(replay_snapshot.get("current_task_id"), max_len=128) or ""
        run_current_task_id = _clean_optional_text(sync_payload.get("current_task_id"), max_len=128) or ""
        if run_current_task_id != snapshot_current_task_id:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run current_task_id conflicts with preserved plan snapshot.",
                    details={
                        "reason": "replay_snapshot_current_task_id_mismatch",
                        "snapshot_current_task_id": snapshot_current_task_id,
                        "run_plan_current_task_id": run_current_task_id,
                    },
                )
            )
        snapshot_completion_report_present = bool(str(replay_snapshot.get("completion_report") or "").strip())
        run_completion_report_present = bool(sync_payload.get("completion_report_present"))
        if run_completion_report_present != snapshot_completion_report_present:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run completion report marker conflicts with preserved plan snapshot.",
                    details={
                        "reason": "replay_snapshot_completion_report_mismatch",
                        "snapshot_completion_report_present": snapshot_completion_report_present,
                        "run_plan_completion_report_present": run_completion_report_present,
                    },
                )
            )
        snapshot_updated_at = _clean_optional_text(replay_snapshot.get("updated_at"), max_len=64) or ""
        run_updated_at = _clean_optional_text(sync_payload.get("legacy_updated_at"), max_len=64) or ""
        if run_updated_at != snapshot_updated_at:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="Replay recovery run updated_at conflicts with preserved plan snapshot.",
                    details={
                        "reason": "replay_snapshot_updated_at_mismatch",
                        "snapshot_updated_at": snapshot_updated_at,
                        "run_plan_updated_at": run_updated_at,
                    },
                )
            )

    @staticmethod
    def _manager_replay_finalize_snapshot(docs: _DoctorDocs) -> Optional[Dict[str, Any]]:
        recovery_request = _nested_get(docs.state, "mode_context.recovery_request")
        if not isinstance(recovery_request, dict):
            return None
        if str(recovery_request.get("action") or "").strip() != "replay_finalize":
            return None
        source_run_id = str(recovery_request.get("source_run_id") or "").strip()
        if not source_run_id:
            return None
        recovery_nodes = docs.plan.get("recovery_nodes") if isinstance(docs.plan, dict) else None
        if not isinstance(recovery_nodes, dict):
            return None
        replay_node = recovery_nodes.get("replay_finalize")
        if not isinstance(replay_node, dict):
            return None
        snapshot = replay_node.get("plan_snapshot")
        return snapshot if isinstance(snapshot, dict) else None

    def _compare_codebase_mapper(
        self,
        run: RunArtifactHandle,
        _docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if phase != "operation":
            return
        report = self.boundary_validator.validate(run, mode_id="codebase_mapper", phase=phase)
        if report.status == "ok":
            return
        for item in report.issues:
            issues.append(
                RunDoctorIssue(
                    code=_clean_text(item.code, max_len=96) or "boundary_contract_failed",
                    message=_clean_text(item.message, max_len=512) or "Codebase mapper boundary validation failed.",
                    details=dict(getattr(item, "details", {}) or {}),
                )
            )

    def _compare_analyst(
        self,
        run: RunArtifactHandle,
        docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if phase not in {"intent", "plan", "execute", "complete"}:
            return
        deliverable = self._analyst_deliverable_from_state(docs.state) if phase == "complete" else ""
        not_ready_deliverable = bool(deliverable) and self._analyst_state_deliverable_can_stand_alone(docs)
        context = self._load_analyst_context(run, docs.state)
        if context is None:
            if phase == "complete" and not_ready_deliverable:
                return
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="AnalystStateStore context is missing for current run.",
                    details={"store": "AnalystStateStore", "reason": "missing"},
                )
            )
            return
        if phase == "intent":
            expected_template = _clean_optional_text(
                _nested_get(docs.state, "mode_context.intent_payload.template_id")
                or _nested_get(docs.state, "mode_context.intent_payload.effective_template_id"),
                max_len=128,
            )
            persisted_template = _clean_optional_text(
                context.get("runtime_template_id") or context.get("effective_template_id"),
                max_len=128,
            )
            if not persisted_template or (expected_template and persisted_template != expected_template):
                issues.append(
                    RunDoctorIssue(
                        code="legacy_store_mismatch",
                        message="AnalystStateStore template ids do not match run intent payload.",
                        details={
                            "store": "AnalystStateStore",
                            "reason": "template_mismatch",
                            "expected_template": expected_template,
                            "persisted_template": persisted_template,
                        },
                    )
                )
        if phase == "complete":
            persisted_draft = _clean_optional_text(context.get("last_draft"), max_len=256)
            if deliverable and not persisted_draft and not not_ready_deliverable:
                issues.append(
                    RunDoctorIssue(
                        code="legacy_store_mismatch",
                        message="AnalystStateStore did not persist the final draft/deliverable.",
                        details={"store": "AnalystStateStore", "reason": "deliverable_missing"},
                    )
                )

    @staticmethod
    def _analyst_deliverable_from_state(state: Dict[str, Any]) -> str:
        raw_deliverable = _nested_get(state, "mode_context.final_deliverable")
        if raw_deliverable is _MISSING:
            raw_deliverable = _nested_get(state, "mode_context.last_draft")
        return _clean_optional_text(raw_deliverable, max_len=256)

    @staticmethod
    def _analyst_state_deliverable_can_stand_alone(docs: _DoctorDocs) -> bool:
        quality = docs.metrics.get("analyst_quality") if isinstance(docs.metrics, dict) else {}
        if not isinstance(quality, dict) or not quality:
            return False
        return not runtime_readiness_allows_finalization(quality)

    def _compare_webmaster(
        self,
        run: RunArtifactHandle,
        docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if phase not in {"intent", "dev", "validation", "complete"}:
            return
        context = self._load_webmaster_context(run, docs.state)
        if context is None:
            issues.append(
                RunDoctorIssue(
                    code="legacy_store_mismatch",
                    message="WebmasterStateStore context is missing for current run.",
                    details={"store": "WebmasterStateStore", "reason": "missing"},
                )
            )
            return
        if phase == "intent":
            goal = _clean_optional_text(_nested_get(docs.state, "mode_context.intent_payload.goal"), max_len=256)
            persisted_goal = _clean_optional_text(context.get("goal"), max_len=256)
            if goal and persisted_goal != goal:
                issues.append(
                    RunDoctorIssue(
                        code="legacy_store_mismatch",
                        message="WebmasterStateStore goal does not match intent payload.",
                        details={
                            "store": "WebmasterStateStore",
                            "reason": "goal_mismatch",
                            "expected_goal": goal,
                            "persisted_goal": persisted_goal,
                        },
                    )
                )
        if phase == "validation":
            validation_payload = _nested_get(docs.state, "mode_context.validation_report")
            if isinstance(validation_payload, dict) and not isinstance(context.get("last_validation_json"), dict):
                issues.append(
                    RunDoctorIssue(
                        code="legacy_store_mismatch",
                        message="WebmasterStateStore did not persist validation JSON for validation phase.",
                        details={"store": "WebmasterStateStore", "reason": "validation_json_missing"},
                    )
                )

    def _compare_agent(
        self,
        _run: RunArtifactHandle,
        docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if phase not in {"plan", "execute", "complete"}:
            return
        required_steps = _as_list_of_strings(_nested_get(docs.state, "mode_context.required_use_cli_steps"))
        missing_steps = self._missing_required_event_steps(docs, required_steps)
        if missing_steps:
            issues.append(
                RunDoctorIssue(
                    code="orchestrator_invariant_mismatch",
                    message="Agent orchestrator invariants are broken: required use_cli steps have no serialized evidence.",
                    details={"missing_step_ids": missing_steps},
                )
            )
        if _nested_get(docs.state, "mode_context.blocking_clarification_open") is True:
            issues.append(
                RunDoctorIssue(
                    code="orchestrator_invariant_mismatch",
                    message="Blocking clarification is still open for agent run.",
                    details={"reason": "blocking_clarification_open"},
                )
            )

    def _compare_admin(
        self,
        _run: RunArtifactHandle,
        docs: _DoctorDocs,
        phase: str,
        issues: List[RunDoctorIssue],
    ) -> None:
        if phase not in {"analyze", "complete"}:
            return
        mode_context = docs.state.get("mode_context") if isinstance(docs.state, dict) else {}
        if not isinstance(mode_context, dict):
            return
        execution_context = mode_context.get("execution_context")
        if not isinstance(execution_context, dict):
            return
        native_transport_execution = execution_context.get("native_transport_execution") is True
        destructive_execution = execution_context.get("destructive_execution") is True
        state_status = _clean_optional_text(docs.state.get("status"), max_len=32).lower()
        if native_transport_execution and destructive_execution and state_status not in {
            "aborted",
            "canceled",
            "cancelled",
            "completed",
            "failed",
            "terminated",
        }:
            issues.append(
                RunDoctorIssue(
                    code="admin_destructive_execution_requires_confirmation",
                    message="Admin destructive native execution is still in-flight and cannot be resumed automatically.",
                    details={
                        "target_transport": _clean_optional_text(
                            mode_context.get("target_transport"),
                            max_len=32,
                        ),
                        "action_id": _clean_optional_text(
                            execution_context.get("action_id"),
                            max_len=128,
                        ),
                    },
                )
            )
        if native_transport_execution and execution_context.get("skill_selector_bypassed") is not True:
            issues.append(
                RunDoctorIssue(
                    code="admin_native_transport_not_isolated",
                    message="Admin native transport execution must remain isolated from skill selector wrapping.",
                )
            )
        has_preexisting_issues = bool(issues)
        if native_transport_execution and destructive_execution and has_preexisting_issues:
            issues.append(
                RunDoctorIssue(
                    code="admin_destructive_execution_requires_confirmation",
                    message="Admin destructive native execution cannot be auto-replayed without manual confirmation.",
                    details={
                        "target_transport": _clean_optional_text(
                            mode_context.get("target_transport"),
                            max_len=32,
                        ),
                        "action_id": _clean_optional_text(
                            execution_context.get("action_id"),
                            max_len=128,
                        ),
                    },
                )
            )

    def _persist_recovery(self, run: RunArtifactHandle, report: RunDoctorReport) -> None:
        current = self.artifact_store.load_recovery(run)
        attempts = list(current.get("attempts") or []) if isinstance(current, dict) else []
        attempts.append(
            {
                "diagnosed_at": report.diagnosed_at,
                "mode_id": report.mode_id,
                "phase": report.phase,
                "recommended_action": report.recommended_action,
                "issues": [item.to_dict() for item in report.issues],
            }
        )
        payload = dict(current if isinstance(current, dict) else {})
        payload.update(
            {
                "status": "ok" if report.status == "ok" else "needs_recovery",
                "mode_id": report.mode_id,
                "phase": report.phase,
                "diagnosed_at": report.diagnosed_at,
                "recommended_action": report.recommended_action,
                "last_consistent_checkpoint": report.last_consistent_checkpoint,
                "issues": [item.to_dict() for item in report.issues],
                "attempts": attempts,
                "can_resume": report.can_resume,
            }
        )
        self.artifact_store.save_recovery(run, payload)

    @staticmethod
    def _last_consistent_checkpoint(docs: _DoctorDocs) -> int:
        items = docs.checkpoints.get("items") if isinstance(docs.checkpoints, dict) else []
        if not isinstance(items, list) or not items:
            return 0
        last = items[-1]
        if not isinstance(last, dict):
            return len(items)
        try:
            return int(last.get("index") or len(items))
        except Exception:
            return len(items)

    @staticmethod
    def _dedupe_issues(items: Sequence[RunDoctorIssue]) -> List[RunDoctorIssue]:
        deduped: list[RunDoctorIssue] = []
        seen: set[tuple[str, str, str, tuple[tuple[str, Any], ...]]] = set()
        ordered = sorted(
            items,
            key=lambda issue: (
                issue.code,
                issue.message,
                issue.severity,
                tuple(
                    (detail_key, RunDoctorService._freeze_detail_value(detail_value))
                    for detail_key, detail_value in sorted(issue.details.items())
                ),
            ),
        )
        for issue in ordered:
            key = (
                issue.code,
                issue.message,
                issue.severity,
                tuple(
                    (detail_key, RunDoctorService._freeze_detail_value(detail_value))
                    for detail_key, detail_value in sorted(issue.details.items())
                ),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped

    @staticmethod
    def _freeze_detail_value(value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(
                (str(detail_key), RunDoctorService._freeze_detail_value(detail_value))
                for detail_key, detail_value in sorted(value.items())
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(RunDoctorService._freeze_detail_value(item) for item in value)
        return value

    @staticmethod
    def _missing_required_event_steps(docs: _DoctorDocs, required_steps: Iterable[str]) -> list[str]:
        if not required_steps:
            return []
        if docs.events.malformed:
            return []
        present: set[str] = set()
        if isinstance(docs.metrics, dict):
            metric_units = docs.metrics.get("units")
            if isinstance(metric_units, list):
                for item in metric_units:
                    if not isinstance(item, dict):
                        continue
                    for key in ("unit_id", "step_id", "task_id"):
                        token = _clean_optional_text(item.get(key), max_len=128)
                        if token:
                            present.add(token)
        for event in docs.events.items:
            if not isinstance(event, dict):
                continue
            for key in ("unit_id", "step_id", "task_id"):
                token = _clean_optional_text(event.get(key), max_len=128)
                if token:
                    present.add(token)
        return [step_id for step_id in required_steps if step_id not in present]

    def _load_analyst_context(self, run: RunArtifactHandle, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = _clean_optional_text(_nested_get(state, "mode_context.analyst_context_key"), max_len=128)
        contexts_dir = os.path.join(run.root_dir, ".cli-proxy", ".analyst_data", "contexts")
        if key:
            safe_key = key.replace("/", "_").replace("\\", "_").strip() or "default"
            path = os.path.join(contexts_dir, f"{safe_key}.json")
            data = read_json_locked_if_exists(path, default={})
            return data if isinstance(data, dict) and data else None
        matches = glob(os.path.join(contexts_dir, "*.json"))
        if len(matches) != 1:
            return None
        data = read_json_locked_if_exists(matches[0], default={})
        return data if isinstance(data, dict) and data else None

    def _load_webmaster_context(self, run: RunArtifactHandle, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = _clean_optional_text(_nested_get(state, "mode_context.webmaster_user_key"), max_len=128)
        users_dir = os.path.join(run.root_dir, ".cli-proxy", ".webmaster_data", "users")
        if key:
            safe_key = key.replace("/", "_").replace("\\", "_").strip() or "0_0"
            path = os.path.join(users_dir, f"{safe_key}.json")
            data = read_json_locked_if_exists(path, default={})
            return data if isinstance(data, dict) and data else None
        matches = glob(os.path.join(users_dir, "*.json"))
        if len(matches) != 1:
            return None
        data = read_json_locked_if_exists(matches[0], default={})
        return data if isinstance(data, dict) and data else None
