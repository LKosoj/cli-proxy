from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from glob import glob
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence

from modes.sdk.json_store import read_json_locked_if_exists
from modes.sdk.runtime.final_qc import runtime_readiness_allows_finalization
from modes.sdk.runtime.json_normalizer import loads_safe
from modes.sdk.planning import load_plan, manager_plan_path
from sessions.scoped_key import is_session_scoped_key, sanitize_scoped_key_token

from app.services.run_artifact_store import RunArtifactHandle
from app.services.run_utils import (
    MISSING as _MISSING,
    as_list_of_strings as _as_list_of_strings,
    clean_optional_text as _clean_optional_text,
    clean_text as _clean_text,
    nested_get as _nested_get,
)

logger = logging.getLogger(__name__)

_CODEBASE_MAPPER_GATEWAY_STATUS = frozenset(
    {
        "completed",
        "validated",
        "full_updated",
        "partial_updated",
        "graph_verified",
        "validation_done",
        "repair_done",
    }
)

_CODEBASE_MAPPER_LEGACY_OPTIONAL_FIELD_TYPES: Dict[str, type] = {
    "review_items": list,
    "needs_review": list,
    "reviewed": dict,
    "validate_queue": list,
    "repair_queue": list,
    "validation_report": dict,
    "nodes_status": dict,
    "relation_graph": dict,
}


def _is_present(value: Any) -> bool:
    if value is _MISSING or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(value)
    return True


def _manager_scoped_key_from_state(state: Dict[str, Any]) -> Optional[str]:
    execution_context = _nested_get(state, "mode_context.execution_context")
    if not isinstance(execution_context, dict):
        return None
    token = sanitize_scoped_key_token(execution_context.get("session_scoped_key"))
    if token and is_session_scoped_key(token):
        return token
    return None


@dataclass(frozen=True)
class RunBoundaryContract:
    mode_id: str
    phase: str
    required_artifacts: tuple[str, ...] = ()
    required_state_fields: tuple[str, ...] = ()
    required_plan_fields: tuple[str, ...] = ()
    required_event_types: tuple[str, ...] = ()
    next_allowed_phases: tuple[str, ...] = ()
    validator: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode_id": self.mode_id,
            "phase": self.phase,
            "required_artifacts": list(self.required_artifacts),
            "required_state_fields": list(self.required_state_fields),
            "required_plan_fields": list(self.required_plan_fields),
            "required_event_types": list(self.required_event_types),
            "next_allowed_phases": list(self.next_allowed_phases),
            "validator": self.validator,
        }


@dataclass(frozen=True)
class RunBoundaryIssue:
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RunBoundaryReport:
    mode_id: str
    phase: str
    status: Literal["ok", "error"]
    issues: List[RunBoundaryIssue]
    next_allowed_phases: List[str]
    contract: Optional[RunBoundaryContract] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode_id": self.mode_id,
            "phase": self.phase,
            "status": self.status,
            "issues": [item.to_dict() for item in self.issues],
            "next_allowed_phases": list(self.next_allowed_phases),
            "contract": self.contract.to_dict() if self.contract is not None else None,
        }


@dataclass(frozen=True)
class _LoadedEvents:
    items: List[Dict[str, Any]]
    malformed: bool = False


@dataclass(frozen=True)
class _RunBoundaryDocs:
    state: Dict[str, Any]
    plan: Dict[str, Any]
    checkpoints: Dict[str, Any]
    metrics: Dict[str, Any]
    events: _LoadedEvents


class RunBoundaryValidationService:
    CONTRACTS: Dict[str, Dict[str, RunBoundaryContract]] = {
        "agent": {
            "plan": RunBoundaryContract(
                mode_id="agent",
                phase="plan",
                required_artifacts=("PLAN.json",),
                required_plan_fields=("units",),
                next_allowed_phases=("execute",),
                validator="agent_plan",
            ),
            "execute": RunBoundaryContract(
                mode_id="agent",
                phase="execute",
                required_artifacts=("METRICS.json", "EVENTS.jsonl"),
                next_allowed_phases=("complete", "review"),
                validator="agent_execute",
            ),
            "complete": RunBoundaryContract(
                mode_id="agent",
                phase="complete",
                required_artifacts=("PLAN.json", "METRICS.json", "EVENTS.jsonl"),
                next_allowed_phases=(),
                validator="agent_complete",
            ),
        },
        "analyst": {
            "intent": RunBoundaryContract(
                mode_id="analyst",
                phase="intent",
                required_state_fields=("mode_context.intent_payload",),
                next_allowed_phases=("plan",),
                validator="analyst_intent",
            ),
            "plan": RunBoundaryContract(
                mode_id="analyst",
                phase="plan",
                required_artifacts=("PLAN.json",),
                required_plan_fields=("units",),
                next_allowed_phases=("execute",),
                validator="analyst_plan",
            ),
            "execute": RunBoundaryContract(
                mode_id="analyst",
                phase="execute",
                required_artifacts=("CHECKPOINTS.json", "METRICS.json"),
                next_allowed_phases=("complete",),
                validator="analyst_execute",
            ),
            "complete": RunBoundaryContract(
                mode_id="analyst",
                phase="complete",
                required_state_fields=("status",),
                next_allowed_phases=(),
                validator="analyst_complete",
            ),
        },
        "manager": {
            "plan": RunBoundaryContract(
                mode_id="manager",
                phase="plan",
                required_artifacts=("PLAN.json", "MANAGER_PLAN.json"),
                required_state_fields=("mode_context.decompose_payload_valid", "mode_context.dynamic_validation_passed"),
                next_allowed_phases=("develop",),
                validator="manager_plan",
            ),
            "develop": RunBoundaryContract(
                mode_id="manager",
                phase="develop",
                required_state_fields=("mode_context.dev_report",),
                next_allowed_phases=("review",),
                validator="manager_develop",
            ),
            "review": RunBoundaryContract(
                mode_id="manager",
                phase="review",
                required_state_fields=("mode_context.review_payload_valid", "mode_context.review_decision_outcome"),
                next_allowed_phases=("develop", "complete"),
                validator="manager_review",
            ),
            "complete": RunBoundaryContract(
                mode_id="manager",
                phase="complete",
                required_state_fields=("mode_context.final_report",),
                next_allowed_phases=(),
                validator="manager_complete",
            ),
        },
        "webmaster": {
            "intent": RunBoundaryContract(
                mode_id="webmaster",
                phase="intent",
                required_state_fields=("mode_context.intent_payload",),
                next_allowed_phases=("dev",),
                validator="webmaster_intent",
            ),
            "dev": RunBoundaryContract(
                mode_id="webmaster",
                phase="dev",
                required_state_fields=("mode_context.developer_report",),
                next_allowed_phases=("validation",),
                validator="webmaster_dev",
            ),
            "validation": RunBoundaryContract(
                mode_id="webmaster",
                phase="validation",
                required_state_fields=("mode_context.validation_report",),
                next_allowed_phases=("dev", "complete"),
                validator="webmaster_validation",
            ),
            "complete": RunBoundaryContract(
                mode_id="webmaster",
                phase="complete",
                required_state_fields=("mode_context.structured_report",),
                next_allowed_phases=(),
                validator="webmaster_complete",
            ),
        },
        "codebase_mapper": {
            "operation": RunBoundaryContract(
                mode_id="codebase_mapper",
                phase="operation",
                required_state_fields=("mode_context.operation", "mode_context.map_dir"),
                next_allowed_phases=(),
                validator="codebase_mapper_operation",
            ),
        },
        "admin": {
            "analyze": RunBoundaryContract(
                mode_id="admin",
                phase="analyze",
                required_artifacts=("PLAN.json", "CHECKPOINTS.json"),
                required_state_fields=(
                    "mode_context.operation_payload",
                    "mode_context.target_transport",
                    "mode_context.snapshot_id",
                    "mode_context.snapshot_ids",
                    "mode_context.snapshot_fidelity",
                    "mode_context.last_monitor_snapshot",
                    "mode_context.last_analyzer_decision",
                ),
                next_allowed_phases=("complete",),
                validator="admin_analyze",
            ),
            "complete": RunBoundaryContract(
                mode_id="admin",
                phase="complete",
                required_artifacts=("PLAN.json", "CHECKPOINTS.json"),
                required_state_fields=(
                    "mode_context.operation_payload",
                    "mode_context.target_transport",
                    "mode_context.execution_context",
                ),
                next_allowed_phases=(),
                validator="admin_complete",
            ),
        },
    }

    def __init__(self, *, enabled: bool):
        self.enabled = bool(enabled)

    def is_enabled(self) -> bool:
        return bool(self.enabled)

    def contract_for(self, mode_id: str, phase: str) -> Optional[RunBoundaryContract]:
        mode_key = _clean_text(mode_id, max_len=64)
        phase_key = _clean_text(phase, max_len=64)
        return self.CONTRACTS.get(mode_key, {}).get(phase_key)

    def validate(
        self,
        run: RunArtifactHandle,
        *,
        mode_id: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> RunBoundaryReport:
        resolved_mode_id = _clean_text(mode_id or run.mode_id, max_len=64) or run.mode_id
        docs = self._load_docs(run)
        resolved_phase = _clean_text(phase or docs.state.get("phase"), max_len=64) or str(phase or "")
        contract = self.contract_for(resolved_mode_id, resolved_phase)
        if not self.is_enabled():
            return RunBoundaryReport(
                mode_id=resolved_mode_id,
                phase=resolved_phase,
                status="ok",
                issues=[],
                next_allowed_phases=[],
                contract=contract,
            )
        if contract is None:
            return RunBoundaryReport(
                mode_id=resolved_mode_id,
                phase=resolved_phase,
                status="error",
                issues=[
                    RunBoundaryIssue(
                        code="unsupported_boundary_contract",
                        message=f"Boundary contract is not defined for {resolved_mode_id}:{resolved_phase}",
                    )
                ],
                next_allowed_phases=[],
                contract=None,
            )

        issues: List[RunBoundaryIssue] = []
        self._check_required_artifacts(run, docs.state, contract, issues)
        self._check_required_fields(docs.state, contract.required_state_fields, issues, source="state")
        self._check_required_fields(docs.plan, contract.required_plan_fields, issues, source="plan")
        self._check_events_payload(run, contract, docs.events, issues)
        self._check_required_event_types(docs.events, contract.required_event_types, issues)
        self._run_custom_validator(run, docs, contract, issues)
        return RunBoundaryReport(
            mode_id=resolved_mode_id,
            phase=resolved_phase,
            status="ok" if not issues else "error",
            issues=issues,
            next_allowed_phases=list(contract.next_allowed_phases) if not issues else [],
            contract=contract,
        )

    def validate_phase(
        self,
        run: RunArtifactHandle,
        phase: str,
        *,
        mode_id: Optional[str] = None,
    ) -> RunBoundaryReport:
        return self.validate(run, mode_id=mode_id, phase=phase)

    def _load_docs(self, run: RunArtifactHandle) -> _RunBoundaryDocs:
        state = read_json_locked_if_exists(run.state_path, default={})
        plan = read_json_locked_if_exists(run.plan_path, default={})
        checkpoints = read_json_locked_if_exists(run.checkpoints_path, default={})
        metrics = read_json_locked_if_exists(run.metrics_path, default={})
        return _RunBoundaryDocs(
            state=state if isinstance(state, dict) else {},
            plan=plan if isinstance(plan, dict) else {},
            checkpoints=checkpoints if isinstance(checkpoints, dict) else {},
            metrics=metrics if isinstance(metrics, dict) else {},
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

    def _check_required_artifacts(
        self,
        run: RunArtifactHandle,
        state: Dict[str, Any],
        contract: RunBoundaryContract,
        issues: List[RunBoundaryIssue],
    ) -> None:
        for artifact in contract.required_artifacts:
            path = self._resolve_artifact_path(run, artifact, state)
            if os.path.exists(path):
                continue
            issues.append(
                RunBoundaryIssue(
                    code="missing_artifact",
                    message=f"Required artifact is missing: {artifact}",
                    details={"artifact": artifact, "path": path},
                )
            )

    def _check_events_payload(
        self,
        run: RunArtifactHandle,
        contract: RunBoundaryContract,
        events: _LoadedEvents,
        issues: List[RunBoundaryIssue],
    ) -> None:
        if not events.malformed or "EVENTS.jsonl" not in contract.required_artifacts:
            return
        issues.append(
            RunBoundaryIssue(
                code="events_malformed",
                message="EVENTS.jsonl exists but could not be parsed as observability events.",
                details={"path": run.events_path},
            )
        )

    def _check_required_fields(
        self,
        payload: Dict[str, Any],
        field_paths: Sequence[str],
        issues: List[RunBoundaryIssue],
        *,
        source: str,
    ) -> None:
        for field_path in field_paths:
            value = _nested_get(payload, field_path)
            if _is_present(value):
                continue
            issues.append(
                RunBoundaryIssue(
                    code=f"missing_{source}_field",
                    message=f"Required {source} field is missing or empty: {field_path}",
                    details={"field": field_path, "source": source},
                )
            )

    def _check_required_event_types(
        self,
        events: _LoadedEvents,
        expected_event_types: Sequence[str],
        issues: List[RunBoundaryIssue],
    ) -> None:
        if events.malformed or not expected_event_types:
            return
        present = {
            str(item.get("event_type") or "").strip()
            for item in events.items
            if isinstance(item, dict)
        }
        missing = [event_type for event_type in expected_event_types if event_type not in present]
        if not missing:
            return
        issues.append(
            RunBoundaryIssue(
                code="missing_event_types",
                message="Required event types are missing from EVENTS.jsonl",
                details={"missing_event_types": missing},
            )
        )

    def _run_custom_validator(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        contract: RunBoundaryContract,
        issues: List[RunBoundaryIssue],
    ) -> None:
        name = _clean_text(contract.validator, max_len=64)
        if not name:
            return
        validator = getattr(self, f"_validate_{name}", None)
        if callable(validator):
            try:
                validator(run, docs, issues)
            except Exception as exc:
                logger.exception(
                    "boundary custom validator failed mode=%s phase=%s validator=%s run_id=%s",
                    contract.mode_id,
                    contract.phase,
                    name,
                    getattr(run, "run_id", None),
                )
                issues.append(
                    RunBoundaryIssue(
                        code="boundary_validator_exception",
                        message=f"Boundary validator `{name}` failed while validating persisted state.",
                        details={
                            "validator": name,
                            "error_type": type(exc).__name__,
                        },
                    )
                )

    @staticmethod
    def _resolve_artifact_path(run: RunArtifactHandle, artifact: str, state: Dict[str, Any]) -> str:
        if os.path.isabs(str(artifact or "")):
            return os.path.abspath(str(artifact))
        token = str(artifact or "").strip()
        if token == "MANAGER_PLAN.json":
            return manager_plan_path(run.root_dir, scoped_key=_manager_scoped_key_from_state(state))
        return os.path.join(run.run_dir, token)

    def _validate_analyst_intent(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        payload = _nested_get(docs.state, "mode_context.intent_payload")
        if not isinstance(payload, dict):
            issues.append(
                RunBoundaryIssue(
                    code="analyst_intent_payload_invalid",
                    message="Analyst intent payload must be a JSON object.",
                )
            )
        elif not _is_present(payload.get("template_id") or payload.get("effective_template_id")):
            issues.append(
                RunBoundaryIssue(
                    code="analyst_template_missing",
                    message="Analyst intent payload must contain template information.",
                )
            )
        context_payload = self._load_analyst_context(run, docs.state)
        if not isinstance(context_payload, dict):
            issues.append(
                RunBoundaryIssue(
                    code="analyst_context_missing",
                    message="AnalystStateStore context was not found for intent boundary validation.",
                )
            )
            return
        if not _is_present(context_payload.get("runtime_template_id") or context_payload.get("effective_template_id")):
            issues.append(
                RunBoundaryIssue(
                    code="analyst_context_template_missing",
                    message="AnalystStateStore context must persist runtime/effective template ids.",
                )
            )

    def _validate_analyst_plan(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        units = docs.plan.get("units")
        if isinstance(units, list) and units:
            return
        issues.append(
            RunBoundaryIssue(
                code="analyst_plan_units_missing",
                message="Analyst plan boundary requires a non-empty units list in PLAN.json.",
            )
        )

    def _validate_analyst_execute(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        checkpoints = docs.checkpoints.get("items")
        metric_units = docs.metrics.get("units")
        if isinstance(checkpoints, list) and checkpoints:
            return
        if isinstance(metric_units, list) and metric_units:
            return
        issues.append(
            RunBoundaryIssue(
                code="analyst_execution_evidence_missing",
                message="Analyst execute boundary requires at least one checkpoint or unit metrics entry.",
            )
        )

    def _validate_analyst_complete(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        if str(docs.state.get("status") or "").strip() != "completed":
            issues.append(
                RunBoundaryIssue(
                    code="analyst_state_not_completed",
                    message="Analyst complete boundary requires STATE.status=completed.",
                )
            )
        deliverable = _nested_get(docs.state, "mode_context.final_deliverable")
        if not _is_present(deliverable):
            deliverable = _nested_get(docs.state, "mode_context.last_draft")
        if _is_present(deliverable):
            quality = docs.metrics.get("analyst_quality")
            quality_payload = quality if isinstance(quality, dict) else {}
            quality_verdict = str(
                quality_payload.get("runtime_verdict") or quality_payload.get("verdict") or ""
            ).strip()
            if quality_verdict and runtime_readiness_allows_finalization(quality_payload):
                return
            issues.append(
                RunBoundaryIssue(
                    code="analyst_quality_gate_not_passed",
                    message="Analyst complete boundary requires analyst_quality to pass finalization readiness checks.",
                    details={
                        "runtime_verdict": str(quality_payload.get("runtime_verdict") or quality_payload.get("verdict") or ""),
                        "blocking_reasons": list(quality_payload.get("blocking_reasons") or []),
                    },
                )
            )
            return
        issues.append(
            RunBoundaryIssue(
                code="analyst_deliverable_missing",
                message="Analyst complete boundary requires final_deliverable or last_draft in mode_context.",
            )
        )

    def _validate_agent_plan(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        units = docs.plan.get("units")
        if not isinstance(units, list) or not units:
            issues.append(
                RunBoundaryIssue(
                    code="agent_plan_units_missing",
                    message="Agent plan boundary requires a non-empty orchestrator plan.",
                )
            )
            return
        required_steps = self._required_use_cli_steps(docs.state)
        if not required_steps:
            return
        present_ids = {
            str(item.get("id") or "").strip()
            for item in units
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        missing = [step_id for step_id in required_steps if step_id not in present_ids]
        if missing:
            issues.append(
                RunBoundaryIssue(
                    code="agent_required_use_cli_steps_missing",
                    message="Agent plan boundary is missing required repo use_cli steps.",
                    details={"missing_step_ids": missing},
                )
            )

    def _validate_agent_execute(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        checkpoints = docs.checkpoints.get("items")
        metric_units = docs.metrics.get("units")
        if not ((isinstance(checkpoints, list) and checkpoints) or (isinstance(metric_units, list) and metric_units)):
            issues.append(
                RunBoundaryIssue(
                    code="agent_execution_evidence_missing",
                    message="Agent execute boundary requires at least one checkpoint or unit metrics entry.",
                )
            )
        required_steps = self._required_use_cli_steps(docs.state)
        if required_steps and not docs.events.malformed:
            missing = self._missing_event_step_ids(docs.events.items, required_steps)
            if missing:
                issues.append(
                    RunBoundaryIssue(
                        code="agent_use_cli_events_missing",
                        message="Agent execute boundary requires serialized use_cli/tool events in observability log.",
                        details={"missing_step_ids": missing},
                    )
                )

    def _validate_agent_complete(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        blocking = _nested_get(docs.state, "mode_context.blocking_clarification_open")
        if blocking is True:
            clarification_meta = _nested_get(docs.state, "mode_context.blocking_clarifications")
            details = {}
            if isinstance(clarification_meta, dict):
                count = clarification_meta.get("count")
                active_question_id = _clean_optional_text(clarification_meta.get("active_question_id"), max_len=128)
                try:
                    details["count"] = int(count or 0)
                except Exception:
                    details["count"] = 0
                if active_question_id:
                    details["active_question_id"] = active_question_id
            issues.append(
                RunBoundaryIssue(
                    code="agent_blocking_clarification_open",
                    message="Agent complete boundary cannot pass while blocking clarification is unresolved.",
                    details=details,
                )
            )
        required_steps = self._required_use_cli_steps(docs.state)
        if not required_steps:
            return
        if docs.events.malformed:
            return
        missing = self._missing_event_step_ids(docs.events.items, required_steps)
        if missing:
            issues.append(
                RunBoundaryIssue(
                    code="agent_final_repo_evidence_missing",
                    message="Agent complete boundary requires final repo evidence for all required use_cli steps.",
                    details={"missing_step_ids": missing},
                )
            )

    def _validate_manager_plan(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        for field_name in ("mode_context.decompose_payload_valid", "mode_context.dynamic_validation_passed"):
            value = _nested_get(docs.state, field_name)
            if value is True:
                continue
            issues.append(
                RunBoundaryIssue(
                    code="manager_plan_flag_invalid",
                    message=f"Manager plan boundary requires `{field_name}` to be true.",
                    details={"field": field_name, "value": value},
                )
            )
        self._validate_manager_legacy_plan_sync(run, docs, issues, allow_archived=False)

    def _validate_manager_develop(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        if not _is_present(_nested_get(docs.state, "mode_context.dev_report")):
            issues.append(
                RunBoundaryIssue(
                    code="manager_dev_report_missing",
                    message="Manager develop boundary requires a developer report in mode_context.dev_report.",
                )
            )
        consistency_flag = _nested_get(docs.state, "mode_context.task_status_consistent")
        if consistency_flag is True:
            return
        plan = load_plan(run.root_dir, scoped_key=_manager_scoped_key_from_state(docs.state))
        if plan is not None and any(_is_present(task.dev_report) and str(task.status or "").strip() != "pending" for task in plan.tasks):
            return
        issues.append(
            RunBoundaryIssue(
                code="manager_task_status_inconsistent",
                message="Manager develop boundary requires consistent task status update in legacy plan or state flag.",
            )
        )

    def _validate_manager_review(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        if _nested_get(docs.state, "mode_context.review_payload_valid") is not True:
            issues.append(
                RunBoundaryIssue(
                    code="manager_review_payload_invalid",
                    message="Manager review boundary requires review_payload_valid=true.",
                )
            )
        if _is_present(_nested_get(docs.state, "mode_context.review_decision_outcome")):
            return
        issues.append(
            RunBoundaryIssue(
                code="manager_review_outcome_missing",
                message="Manager review boundary requires a recorded decision outcome.",
            )
        )

    def _validate_manager_complete(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        if not _is_present(_nested_get(docs.state, "mode_context.final_report")):
            issues.append(
                RunBoundaryIssue(
                    code="manager_final_report_missing",
                    message="Manager complete boundary requires a final report.",
                )
            )
        self._validate_manager_legacy_plan_sync(run, docs, issues, allow_archived=True)
        replay_snapshot = self._manager_replay_finalize_snapshot(docs)
        if replay_snapshot is not None:
            if str(replay_snapshot.get("status") or "").strip() != "completed":
                issues.append(
                    RunBoundaryIssue(
                        code="manager_plan_not_completed",
                        message="Manager complete boundary requires recovery snapshot status=completed.",
                    )
                )
            if not _is_present(replay_snapshot.get("completion_report")):
                issues.append(
                    RunBoundaryIssue(
                        code="manager_completion_report_missing",
                        message="Manager complete boundary requires completion_report in recovery snapshot.",
                    )
                )
            return
        plan = load_plan(run.root_dir, scoped_key=_manager_scoped_key_from_state(docs.state))
        sync_status = _clean_optional_text(_nested_get(docs.state, "mode_context.legacy_plan_sync_status"), max_len=32)
        if plan is None and sync_status == "archived":
            return
        if plan is None:
            issues.append(
                RunBoundaryIssue(
                    code="manager_legacy_plan_missing",
                    message="Manager complete boundary requires MANAGER_PLAN.json sync or archived marker.",
                )
            )
            return
        if str(plan.status or "").strip() != "completed":
            issues.append(
                RunBoundaryIssue(
                    code="manager_plan_not_completed",
                    message="Manager complete boundary requires legacy MANAGER_PLAN.json to be marked completed.",
                )
            )
        if not _is_present(plan.completion_report):
            issues.append(
                RunBoundaryIssue(
                    code="manager_completion_report_missing",
                    message="Manager complete boundary requires completion_report in legacy MANAGER_PLAN.json.",
                )
            )

    def _validate_manager_legacy_plan_sync(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
        *,
        allow_archived: bool,
    ) -> None:
        sync_payload = docs.plan.get("legacy_plan_sync")
        sync_status = _clean_optional_text(_nested_get(docs.state, "mode_context.legacy_plan_sync_status"), max_len=32)
        replay_snapshot = self._manager_replay_finalize_snapshot(docs)
        if replay_snapshot is not None:
            if not isinstance(sync_payload, dict) or sync_payload.get("synced") is not True:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_unsynced",
                        message="Run-level PLAN.json is not synchronized with recovery snapshot.",
                    )
                )
                return
            state_sync = _nested_get(docs.state, "mode_context.legacy_plan_sync")
            if state_sync is not _MISSING and state_sync != sync_payload:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_conflict",
                        message="Run-level STATE.json echo conflicts with PLAN.json legacy sync payload.",
                        details={
                            "field": "state_plan_echo",
                            "reason": "state_plan_echo_mismatch",
                        },
                    )
                )
            expected_task_count = len(replay_snapshot.get("tasks") or [])
            actual_task_count = int(sync_payload.get("task_count") or 0)
            if actual_task_count != expected_task_count:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_conflict",
                        message="Run-level PLAN.json conflicts with recovery snapshot task count.",
                        details={
                            "field": "task_count",
                            "reason": "task_count_mismatch",
                            "legacy_task_count": expected_task_count,
                            "run_task_count": actual_task_count,
                        },
                    )
                )
            expected_status = _clean_optional_text(replay_snapshot.get("status"), max_len=64) or ""
            actual_status = _clean_optional_text(sync_payload.get("legacy_status"), max_len=64) or ""
            if expected_status and actual_status != expected_status:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_conflict",
                        message="Run-level PLAN.json status conflicts with recovery snapshot.",
                        details={
                            "field": "legacy_status",
                            "reason": "legacy_status_mismatch",
                            "legacy_status": expected_status,
                            "run_plan_status": actual_status,
                        },
                    )
                )
            expected_current_task = _clean_optional_text(replay_snapshot.get("current_task_id"), max_len=128) or ""
            actual_current_task = _clean_optional_text(sync_payload.get("current_task_id"), max_len=128) or ""
            if actual_current_task != expected_current_task:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_conflict",
                        message="Run-level PLAN.json current_task_id conflicts with recovery snapshot.",
                        details={
                            "field": "current_task_id",
                            "reason": "current_task_id_mismatch",
                            "legacy_current_task_id": expected_current_task,
                            "run_plan_current_task_id": actual_current_task,
                        },
                    )
                )
            expected_completion_report = bool(str(replay_snapshot.get("completion_report") or "").strip())
            actual_completion_report = bool(sync_payload.get("completion_report_present"))
            if actual_completion_report != expected_completion_report:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_conflict",
                        message="Run-level PLAN.json completion report flag conflicts with recovery snapshot.",
                        details={
                            "field": "completion_report_present",
                            "reason": "completion_report_presence_mismatch",
                            "legacy_completion_report_present": expected_completion_report,
                            "run_plan_completion_report_present": actual_completion_report,
                        },
                    )
                )
            expected_updated_at = _clean_optional_text(replay_snapshot.get("updated_at"), max_len=64) or ""
            actual_updated_at = _clean_optional_text(sync_payload.get("legacy_updated_at"), max_len=64) or ""
            if actual_updated_at != expected_updated_at:
                issues.append(
                    RunBoundaryIssue(
                        code="manager_legacy_plan_conflict",
                        message="Run-level PLAN.json update timestamp conflicts with recovery snapshot.",
                        details={
                            "field": "legacy_updated_at",
                            "reason": "legacy_updated_at_mismatch",
                            "legacy_updated_at": expected_updated_at,
                            "run_plan_updated_at": actual_updated_at,
                        },
                    )
                )
            return
        legacy_plan = load_plan(run.root_dir, scoped_key=_manager_scoped_key_from_state(docs.state))
        if legacy_plan is None and allow_archived and sync_status == "archived":
            return
        if legacy_plan is None:
            issues.append(
                RunBoundaryIssue(
                    code="manager_legacy_plan_missing",
                    message="Legacy MANAGER_PLAN.json is required for manager boundary validation.",
                )
            )
            return
        if not isinstance(sync_payload, dict) or sync_payload.get("synced") is not True:
            issues.append(
                RunBoundaryIssue(
                    code="manager_legacy_plan_unsynced",
                    message="Run-level PLAN.json is not synchronized with legacy MANAGER_PLAN.json.",
                )
            )
            return
        expected_task_count = int(len(legacy_plan.tasks))
        actual_task_count = int(sync_payload.get("task_count") or 0)
        if actual_task_count != expected_task_count:
            issues.append(
                RunBoundaryIssue(
                    code="manager_legacy_plan_conflict",
                    message="Run-level PLAN.json conflicts with legacy MANAGER_PLAN.json task count.",
                    details={"legacy_task_count": expected_task_count, "run_task_count": actual_task_count},
                )
            )

    @staticmethod
    def _manager_replay_finalize_snapshot(docs: _RunBoundaryDocs) -> Optional[Dict[str, Any]]:
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

    def _validate_webmaster_intent(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        payload = _nested_get(docs.state, "mode_context.intent_payload")
        if not isinstance(payload, dict):
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_intent_payload_invalid",
                    message="Webmaster intent payload must be a JSON object.",
                )
            )
        elif not _is_present(payload.get("goal") or payload.get("task_kind")):
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_intent_payload_incomplete",
                    message="Webmaster intent payload must include goal or task_kind.",
                )
            )
        context_payload = self._load_webmaster_context(run, docs.state)
        if isinstance(context_payload, dict) and _is_present(context_payload.get("goal") or context_payload.get("last_user_text")):
            return
        issues.append(
            RunBoundaryIssue(
                code="webmaster_context_missing",
                message="WebmasterStateStore must persist context for intent boundary validation.",
            )
        )

    def _validate_webmaster_dev(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        report = _nested_get(docs.state, "mode_context.developer_report")
        if _is_present(report):
            return
        context_payload = self._load_webmaster_context(run, docs.state)
        if isinstance(context_payload, dict) and _is_present(context_payload.get("last_cli_report")):
            return
        issues.append(
            RunBoundaryIssue(
                code="webmaster_dev_report_missing",
                message="Webmaster dev boundary requires developer or patch report evidence.",
            )
        )

    def _validate_webmaster_validation(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        report = _nested_get(docs.state, "mode_context.validation_report")
        if not isinstance(report, dict):
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_validation_report_invalid",
                    message="Webmaster validation boundary requires structured validation_report payload.",
                )
            )
            return
        checklist_rows = report.get("checklist_rows") or report.get("checklist_results") or []
        if not isinstance(checklist_rows, list) or not checklist_rows:
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_checklist_missing",
                    message="Webmaster validation boundary requires serialized checklist rows.",
                )
            )
            return
        gate = report.get("gate")
        if isinstance(gate, dict):
            if gate.get("checklist_table_present") is False:
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_checklist_table_missing",
                        message="Existing webmaster validation gate requires checklist table in developer report.",
                    )
                )
            for row in self._gate_rows_as_dicts(gate, "invalid_rows", issues):
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_checklist_row_invalid",
                        message="Webmaster validation checklist row must be a JSON object.",
                        details={"item": row.get("item"), "status": row.get("status")},
                    )
                )
            for row in self._gate_rows_as_dicts(gate, "non_pass_rows", issues):
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_gate_failed",
                        message="Existing webmaster validation gate rejects non-PASS checklist rows.",
                        details={"item": row.get("item"), "status": row.get("status")},
                    )
                )
            for row in self._gate_rows_as_dicts(gate, "missing_evidence_rows", issues):
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_checklist_evidence_missing",
                        message="Existing webmaster validation gate requires evidence for every checklist row.",
                        details={"item": row.get("item")},
                    )
                )
            blocking_issue_count = int(gate.get("blocking_issue_count") or 0)
            if (
                gate.get("passed") is False
                and not any(issue.code == "webmaster_gate_failed" for issue in issues)
                and (blocking_issue_count > 0 or str(report.get("status") or "").strip().upper() != "PASS")
            ):
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_gate_failed",
                        message="Existing webmaster validation gate rejected validation status or blocking issues.",
                        details={
                            "status": str(report.get("status") or "").strip().upper(),
                            "blocking_issue_count": blocking_issue_count,
                        },
                    )
                )
            return
        developer_report = str(_nested_get(docs.state, "mode_context.developer_report") or "")
        if not self._has_checklist_table(developer_report):
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_checklist_table_missing",
                    message="Webmaster validation PASS cannot bypass checklist table in developer report.",
                )
            )
        status_token = str(report.get("status") or "").strip().upper()
        blocking_issues = _as_list_of_strings(report.get("blocking_issues"))
        if (status_token and status_token != "PASS") or blocking_issues:
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_gate_failed",
                    message="Existing webmaster validation gate rejected validation status or blocking issues.",
                    details={
                        "status": status_token,
                        "blocking_issue_count": len(blocking_issues),
                    },
                )
            )
        for row in checklist_rows:
            if not isinstance(row, dict):
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_checklist_row_invalid",
                        message="Webmaster validation checklist row must be a JSON object.",
                    )
                )
                continue
            status = str(row.get("status") or "").strip().upper()
            evidence = str(row.get("evidence") or "").strip()
            if status != "PASS":
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_gate_failed",
                        message="Existing webmaster validation gate rejects non-PASS checklist rows.",
                        details={"item": row.get("item"), "status": status},
                    )
                )
            if not evidence:
                issues.append(
                    RunBoundaryIssue(
                        code="webmaster_checklist_evidence_missing",
                        message="Existing webmaster validation gate requires evidence for every checklist row.",
                        details={"item": row.get("item")},
                    )
                )

    @staticmethod
    def _gate_rows_as_dicts(
        gate: Dict[str, Any],
        field_name: str,
        issues: List[RunBoundaryIssue],
    ) -> List[Dict[str, Any]]:
        raw_rows = gate.get(field_name)
        if raw_rows is None:
            return []
        if not isinstance(raw_rows, list):
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_gate_payload_degraded",
                    message=f"Webmaster validation gate содержит повреждённое поле `{field_name}`: ожидался список JSON-объектов.",
                    details={"field": field_name, "actual_type": type(raw_rows).__name__},
                )
            )
            return []
        rows: List[Dict[str, Any]] = []
        for index, row in enumerate(raw_rows):
            if isinstance(row, dict):
                rows.append(row)
                continue
            issues.append(
                RunBoundaryIssue(
                    code="webmaster_gate_payload_degraded",
                    message=f"Webmaster validation gate содержит повреждённую строку `{field_name}`: ожидался JSON-объект.",
                    details={
                        "field": field_name,
                        "row_index": index,
                        "actual_type": type(row).__name__,
                    },
                )
            )
        return rows

    def _validate_webmaster_complete(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        if _is_present(_nested_get(docs.state, "mode_context.structured_report")):
            return
        issues.append(
            RunBoundaryIssue(
                code="webmaster_structured_report_missing",
                message="Webmaster complete boundary requires final structured report.",
            )
        )

    def _validate_codebase_mapper_operation(
        self,
        run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        map_dir = _nested_get(docs.state, "mode_context.map_dir")
        if not map_dir or not os.path.exists(map_dir):
            issues.append(
                RunBoundaryIssue(
                    code="codebase_mapper_map_dir_missing",
                    message="Codebase mapper operation boundary requires a valid map_dir.",
                )
            )
            return

        status = _nested_get(docs.state, "mode_context.status")
        if status in _CODEBASE_MAPPER_GATEWAY_STATUS:
            if not os.path.exists(os.path.join(map_dir, "meta.json")):
                issues.append(RunBoundaryIssue(code="graph_corrupted", message="meta.json is missing."))
            state_path = os.path.join(map_dir, "state.json")
            if not os.path.exists(state_path):
                issues.append(RunBoundaryIssue(code="graph_corrupted", message="state.json is missing."))
            elif not self._codebase_mapper_state_valid(state_path):
                issues.append(
                    RunBoundaryIssue(
                        code="graph_corrupted",
                        message="state.json is malformed or missing required graph state fields.",
                    )
                )
            graph_path = os.path.join(map_dir, "graph.json")
            if not os.path.exists(graph_path):
                issues.append(RunBoundaryIssue(code="graph_corrupted", message="graph.json is missing."))
            elif not self._codebase_mapper_graph_valid(
                graph_path,
                state_path=state_path,
            ):
                issues.append(
                    RunBoundaryIssue(
                        code="graph_corrupted",
                        message="graph.json is malformed or missing required topology fields.",
                    )
                )
            if not os.path.exists(os.path.join(map_dir, "INDEX.md")):
                issues.append(RunBoundaryIssue(code="graph_corrupted", message="INDEX.md is missing."))

        operation = _nested_get(docs.state, "mode_context.operation")
        if operation == "repair" and status not in {"failed", "skipped"}:
            validate_queue = _nested_get(docs.state, "mode_context.validate_queue")
            if isinstance(validate_queue, list) and len(validate_queue) > 0:
                issues.append(RunBoundaryIssue(code="validation_failed", message="Repair operation failed to clear validate_queue."))

        needs_review = _nested_get(docs.state, "mode_context.needs_review")
        if isinstance(needs_review, list) and len(needs_review) > 0:
            issues.append(RunBoundaryIssue(code="manual_review_pending", message="Manual review items are pending."))

    @staticmethod
    def _codebase_mapper_graph_valid(path: str, *, state_path: str | None = None) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        nodes = payload.get("nodes")
        edges = payload.get("edges")
        tree = payload.get("tree")
        if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(tree, list):
            return False
        if any(not isinstance(item, dict) for item in nodes):
            return False
        if any(not isinstance(item, dict) for item in edges):
            return False
        if state_path:
            try:
                with open(state_path, "r", encoding="utf-8") as handle:
                    state_payload = json.load(handle)
            except Exception:
                return False
            if not isinstance(state_payload, dict):
                return False
            expected_nodes = state_payload.get("nodes_count")
            if not isinstance(expected_nodes, int) or expected_nodes < 0:
                return False
            if len(nodes) != expected_nodes:
                return False
            expected_tree = state_payload.get("tree")
            if not isinstance(expected_tree, list):
                return False
            if expected_tree != tree:
                return False
        return True

    @staticmethod
    def _codebase_mapper_state_valid(path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        state_token = payload.get("state")
        operation = payload.get("operation")
        nodes_count = payload.get("nodes_count")
        tree = payload.get("tree")
        if not isinstance(state_token, str) or not state_token.strip():
            return False
        if not isinstance(operation, str) or not operation.strip():
            return False
        if not isinstance(nodes_count, int) or nodes_count < 0:
            return False
        if not isinstance(tree, list):
            return False
        for field_name, expected_type in _CODEBASE_MAPPER_LEGACY_OPTIONAL_FIELD_TYPES.items():
            if field_name in payload and not isinstance(payload.get(field_name), expected_type):
                return False
        return True

    def _validate_admin_analyze(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        operation_kind = _clean_optional_text(_nested_get(docs.state, "mode_context.operation_payload.kind"), max_len=64)
        if operation_kind != "watch_loop":
            issues.append(
                RunBoundaryIssue(
                    code="admin_operation_payload_invalid",
                    message="Admin analyze boundary supports only watch_loop operation payloads.",
                )
            )
            return
        self._validate_admin_snapshot_fidelity(docs.state, issues)
        decision = _nested_get(docs.state, "mode_context.last_analyzer_decision")
        if not isinstance(decision, dict) or not decision:
            issues.append(
                RunBoundaryIssue(
                    code="admin_analyzer_decision_missing",
                    message="Admin analyze boundary requires serialized analyzer decision.",
                )
            )

    def _validate_admin_complete(
        self,
        _run: RunArtifactHandle,
        docs: _RunBoundaryDocs,
        issues: List[RunBoundaryIssue],
    ) -> None:
        operation_payload = _nested_get(docs.state, "mode_context.operation_payload")
        if not isinstance(operation_payload, dict):
            issues.append(
                RunBoundaryIssue(
                    code="admin_operation_payload_invalid",
                    message="Admin complete boundary requires operation_payload JSON object.",
                )
            )
            return
        operation_kind = _clean_optional_text(operation_payload.get("kind"), max_len=64)
        if operation_kind == "watch_loop":
            self._validate_admin_snapshot_fidelity(docs.state, issues)
            if not isinstance(_nested_get(docs.state, "mode_context.last_action_result"), dict):
                issues.append(
                    RunBoundaryIssue(
                        code="admin_action_result_missing",
                        message="Admin watch loop complete boundary requires last_action_result payload.",
                    )
                )
        execution_context = _nested_get(docs.state, "mode_context.execution_context")
        if not isinstance(execution_context, dict):
            issues.append(
                RunBoundaryIssue(
                    code="admin_execution_context_invalid",
                    message="Admin complete boundary requires execution_context JSON object.",
                )
            )
            return
        native_transport_execution = execution_context.get("native_transport_execution") is True
        if native_transport_execution:
            if execution_context.get("skill_selector_bypassed") is not True:
                issues.append(
                    RunBoundaryIssue(
                        code="admin_skill_selector_bypass_missing",
                        message="Admin native transport execution must explicitly bypass skill selectors.",
                    )
                )
            if str(execution_context.get("skill_selector_bypass_reason") or "").strip() != "native_admin_transport":
                issues.append(
                    RunBoundaryIssue(
                        code="admin_skill_selector_bypass_missing",
                        message="Admin native transport execution must declare native_admin_transport bypass reason.",
                    )
                )
        target_transport = _clean_optional_text(_nested_get(docs.state, "mode_context.target_transport"), max_len=32)
        if operation_kind in {"manual_check", "manual_run"} and target_transport not in {"local", "ssh"}:
            issues.append(
                RunBoundaryIssue(
                    code="admin_target_transport_invalid",
                    message="Admin manual operation must persist local or ssh target_transport.",
                )
            )
        if (
            operation_kind == "manual_run"
            and execution_context.get("dry_run") is not True
            and execution_context.get("check_only") is not True
            and execution_context.get("destructive_execution") is not True
        ):
            issues.append(
                RunBoundaryIssue(
                    code="admin_destructive_flag_missing",
                    message="Admin manual run must explicitly mark destructive_execution for live native commands.",
                )
            )

    def _validate_admin_snapshot_fidelity(
        self,
        state: Dict[str, Any],
        issues: List[RunBoundaryIssue],
    ) -> None:
        snapshot_id = _clean_optional_text(_nested_get(state, "mode_context.snapshot_id"), max_len=128)
        snapshot_ids = _as_list_of_strings(_nested_get(state, "mode_context.snapshot_ids"))
        fidelity = _nested_get(state, "mode_context.snapshot_fidelity")
        summary = _nested_get(state, "mode_context.last_monitor_snapshot")
        if not isinstance(fidelity, dict):
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_missing",
                    message="Admin analyze boundary requires snapshot_fidelity JSON object.",
                )
            )
            return
        if not snapshot_id or not snapshot_ids:
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_missing",
                    message="Admin analyze boundary requires snapshot_id and snapshot_ids.",
                )
            )
            return
        if str(fidelity.get("snapshot_id") or "").strip() != snapshot_id:
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_mismatch",
                    message="Admin snapshot_fidelity.snapshot_id must match snapshot_id.",
                )
            )
        fidelity_ids = _as_list_of_strings(fidelity.get("snapshot_ids"))
        if fidelity_ids != snapshot_ids:
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_mismatch",
                    message="Admin snapshot_fidelity.snapshot_ids must match snapshot_ids.",
                )
            )
        summary_server_count = int((summary or {}).get("server_count") or 0) if isinstance(summary, dict) else 0
        fidelity_server_count = int(fidelity.get("server_count") or fidelity.get("total_servers") or 0)
        if summary_server_count > 0 and len(snapshot_ids) != summary_server_count:
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_mismatch",
                    message="Admin snapshot_ids length must match summarized server_count.",
                )
            )
        if fidelity_server_count > 0 and len(snapshot_ids) != fidelity_server_count:
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_mismatch",
                    message="Admin snapshot_fidelity server_count must match snapshot_ids length.",
                )
            )
        if fidelity.get("verified_post_analyze") is not True:
            issues.append(
                RunBoundaryIssue(
                    code="admin_snapshot_fidelity_mismatch",
                    message="Admin snapshot_fidelity must mark verified_post_analyze=true.",
                )
            )

    @staticmethod
    def _required_use_cli_steps(state: Dict[str, Any]) -> list[str]:
        return _as_list_of_strings(
            _nested_get(state, "mode_context.required_use_cli_steps")
            if _nested_get(state, "mode_context.required_use_cli_steps") is not _MISSING
            else []
        )

    @staticmethod
    def _missing_event_step_ids(events: Sequence[Dict[str, Any]], required_steps: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        for item in events:
            if not isinstance(item, dict):
                continue
            for key in ("unit_id", "step_id", "task_id"):
                value = _clean_optional_text(item.get(key), max_len=128)
                if value:
                    seen.add(value)
        return [step_id for step_id in required_steps if step_id not in seen]

    @staticmethod
    def _has_checklist_table(text: str) -> bool:
        raw = str(text or "")
        if "|" not in raw:
            return False
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return False
        header_idx = -1
        for idx, line in enumerate(lines):
            lowered = line.lower()
            if "пункт" in lowered and "статус" in lowered and "|" in line:
                header_idx = idx
                break
        if header_idx < 0:
            return False
        for row in lines[header_idx + 1:]:
            if "|" not in row:
                continue
            upper = row.upper()
            if "PASS" in upper or "PARTIAL" in upper or "FAIL" in upper:
                return True
        return False

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
