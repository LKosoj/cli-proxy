from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import re
import time
from dataclasses import asdict
from typing import Any, List, Optional, Tuple, Dict

from modes.sdk.runtime.tooling.change_filter import (
    filter_git_name_status_lines,
    filter_git_porcelain_lines,
    filter_git_stat_text,
    format_git_log_name_status,
)
from config import AppConfig
from session import Session, session_scoped_key
from utils.paths import cli_proxy_artifact_path
from utils.text import strip_ansi
from app.services.project_prompts_service import (
    ensure_project_prompts,
    load_mode_learning,
    load_mode_prompt_texts,
    save_mode_learning,
)

from modes.sdk.runtime.contracts import DevTask, ProjectPlan, ReviewResult
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from modes.sdk.runtime.executor import Executor
from modes.sdk.planning import (
    MANAGER_CONTINUE_TOKEN,
    ManagerDecomposeNormalizationError,
    archive_plan,
    delete_plan,
    load_plan,
    save_plan,
)
from modes.manager.schemas import (
    FINAL_SPEC_AUDIT_SCHEMA,
    PLAN_PAYLOAD_SCHEMA,
    PLAN_VALIDATION_RESPONSE_SCHEMA,
    validate_payload,
)
from modes.manager.services import (
    ExecutionTrackingService,
    GitReconcileService,
    ManagerUIService,
    PlanManagementService,
    ReviewAndMergeService,
)
from sessions.session_state_access import get_active_mode
from modes.sdk.runtime.json_normalizer import (
    JSONSchemaValidationError,
    loads_safe,
    normalize_payload,
    parse_normalize_validate,
)
from modes.sdk.runtime.openai_client import chat_completion
from modes.sdk.runtime.profiles import build_reviewer_profile
from modes.sdk.runtime.tooling.registry import get_tool_registry
from agent.cli_routing import RoutedCallError, run_prompt_routed_meta
from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore

_log = logging.getLogger(__name__)
MANAGER_ARTIFACT_ROOT_DIR = ".manager"
MANAGER_RESPONSE_ARCHIVE_SUBDIR = "response"

# Decomposition heuristics are internal manager defaults (not config-driven).
MIN_TASKS_FLOOR = 6
MIN_TASKS_PER_REQ = 1
MIN_TASKS_PER_REMAINING = 1
ATOMICITY_MAX_REQS_PER_TASK = 2

_GIT_COMMAND_TIMEOUT_SEC = 120.0
_GIT_COMMAND_ATTEMPTS = 2
_GIT_COMMAND_RETRY_DELAY_SEC = 0.25

_PLAN_FIX_RELAX_CODES = ("TASK_COUNT_BELOW_MIN", "TASK_TOO_BROAD_REQ_COVERAGE")
_GOAL_ALIGNMENT_ISSUE_TAG = "GOAL_ALIGNMENT_MISMATCH"
_GOAL_ALIGNMENT_ISSUE_PATTERNS = (
    re.compile(r"несоответств(?:ие|ия).+между\s+планом\s+и\s+projectgoal", re.IGNORECASE),
    re.compile(r"несоответств(?:ие|ия).+между\s+projectgoal\s+и\s+tasks", re.IGNORECASE),
    re.compile(r"лишн(?:яя|ие)\s+задач[аеи].+projectgoal", re.IGNORECASE),
    re.compile(r"отсутствует\s+в\s+projectgoal", re.IGNORECASE),
    re.compile(r"не\s+упомянута\s+в\s+projectgoal", re.IGNORECASE),
    re.compile(r"mismatch.+projectgoal", re.IGNORECASE),
    re.compile(r"not\s+present\s+in\s+projectgoal", re.IGNORECASE),
)


def _min_tasks_dynamic(analysis: Any) -> int:
    service = ExecutionTrackingService(
        min_tasks_floor=int(MIN_TASKS_FLOOR),
        min_tasks_per_req=int(MIN_TASKS_PER_REQ),
        min_tasks_per_remaining=int(MIN_TASKS_PER_REMAINING),
    )
    return service.min_tasks_dynamic(analysis)


def _normalize_requirement_ref(value: object) -> str:
    """Normalize requirement reference to canonical REQ-* id when possible."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(REQ-[A-Za-z0-9_-]+)", text, flags=re.IGNORECASE)
    if not match:
        return text
    return str(match.group(1)).upper()


# Statuses eligible for retry (normalization after crash/restart).
RETRIABLE_STATUSES = ("pending", "rejected", "in_progress", "in_review")
STATUS_ALIASES = {
    "inreview": "in_review",
    "in-review": "in_review",
    "in progress": "in_progress",
    "inprogress": "in_progress",
}


# ---------------------------------------------------------------------------
# Work-type classification for Manager dev tasks
# ---------------------------------------------------------------------------

WORK_TYPES = (
    "analytics",
    "planning",
    "development",
    "backend_dev",
    "frontend_dev",
    "administration",
    "website_administration",
    "default",
)

DEV_TASK_WORK_TYPES = (
    "development",
    "backend_dev",
    "frontend_dev",
    "administration",
    "website_administration",
)


def _parse_work_type_json(raw: str) -> tuple[Optional[str], float, str]:
    return ExecutionTrackingService().parse_work_type_json(
        raw,
        allowed_work_types=WORK_TYPES,
        logger=_log,
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _archive_ts() -> str:
    """Compact timestamp for response archive filenames: 20260207_143012."""
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _archive_response_write(workdir: str, prefix: str, title: str, body: str) -> None:
    """Write a response archive markdown file to .cli-proxy/.manager/response inside the workdir."""
    try:
        archive_dir = _manager_response_dir(workdir)
        os.makedirs(archive_dir, exist_ok=True)
        ts = _archive_ts()
        fname = f"{ts}_{prefix}.md"
        path = os.path.join(archive_dir, fname)
        content = f"# {title}\n\n**Timestamp:** {_now_iso()}\n\n---\n\n{body}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        _log.info("response archive write failed: %s", e)


def _manager_artifact_dir(workdir: str) -> str:
    return cli_proxy_artifact_path(str(workdir or ""), MANAGER_ARTIFACT_ROOT_DIR)


def _manager_response_dir(workdir: str) -> str:
    return os.path.join(_manager_artifact_dir(workdir), MANAGER_RESPONSE_ARCHIVE_SUBDIR)


def _line_items(value: object) -> List[str]:
    raw = str(value or "").replace("\r", "\n")
    items: List[str] = []
    for part in raw.split("\n"):
        item = part.strip().lstrip("-").strip()
        if item:
            items.append(item)
    return items


def _rule_items(value: object) -> List[str]:
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(_line_items(item))
        return out
    return _line_items(value)


def _dedupe_items(values: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in values:
        key = str(item or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(str(item).strip())
    return out


_SPECIFIC_PROMPT_LEARNING_PATTERNS = (
    re.compile(r"\brq-\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\btask[_\-\s]?\d+\b", re.IGNORECASE),
)

_MANAGER_RUN_HANDLE_SESSION_ATTR = "_manager_mode_active_run_handle"
_MANAGER_RUN_RESUME_GUARD_SESSION_ATTR = "_manager_mode_resume_guard"


def _normalize_learning_text(value: object) -> str:
    raw = str(value or "").replace("\r", "\n")
    return " ".join(part.strip() for part in raw.split("\n") if part.strip()).strip()


def _looks_task_specific_learning_text(value: object) -> bool:
    text = _normalize_learning_text(value)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SPECIFIC_PROMPT_LEARNING_PATTERNS)


def _normalize_general_learning_rules(value: object) -> List[str]:
    out: List[str] = []
    for item in _dedupe_items(_rule_items(value)):
        normalized = _normalize_learning_text(item)
        if not normalized:
            continue
        if _looks_task_specific_learning_text(normalized):
            continue
        out.append(normalized)
    return _dedupe_items(out)


def _normalize_general_learning_patch(patch: object) -> Optional[Dict[str, Any]]:
    if not isinstance(patch, dict):
        return None
    normalized = {
        "added_rules": _normalize_general_learning_rules(patch.get("added_rules")),
        "changed_rules": _normalize_general_learning_rules(patch.get("changed_rules")),
        "removed_rules": _normalize_general_learning_rules(patch.get("removed_rules")),
        "reason": "",
        "expected_effect": "",
    }
    reason = _normalize_learning_text(patch.get("reason"))
    if reason and not _looks_task_specific_learning_text(reason):
        normalized["reason"] = reason
    expected = _normalize_learning_text(patch.get("expected_effect"))
    if expected and not _looks_task_specific_learning_text(expected):
        normalized["expected_effect"] = expected
    if not (normalized["added_rules"] or normalized["changed_rules"] or normalized["removed_rules"]):
        return None
    return normalized


def manager_run_phase_for_plan(plan: Optional[ProjectPlan], *, fallback: str = "plan") -> str:
    fallback_phase = str(fallback or "plan").strip() or "plan"
    if plan is None:
        return fallback_phase
    plan_status = str(getattr(plan, "status", "") or "").strip().lower()
    if plan_status == "completed" or str(getattr(plan, "completion_report", "") or "").strip():
        return "complete"

    current_task_id = str(getattr(plan, "current_task_id", "") or "").strip()
    current_task = None
    for task in list(getattr(plan, "tasks", []) or []):
        if current_task_id and str(getattr(task, "id", "") or "").strip() == current_task_id:
            current_task = task
            break

    ordered_tasks: List[Any] = []
    if current_task is not None:
        ordered_tasks.append(current_task)
    ordered_tasks.extend(list(getattr(plan, "tasks", []) or []))

    for task in ordered_tasks:
        task_status = str(getattr(task, "status", "") or "").strip().lower()
        if task_status == "in_review":
            return "review"
        if task_status == "in_progress":
            return "develop"
        if str(getattr(task, "review_verdict", "") or "").strip() or str(getattr(task, "review_comments", "") or "").strip():
            return "review"
        if str(getattr(task, "dev_report", "") or "").strip():
            return "develop"

    return "plan"


def manager_legacy_phase_for_run_phase(phase: str) -> str:
    phase_key = str(phase or "").strip().lower()
    return {
        "plan": "decompose",
        "develop": "dev",
        "review": "review",
        "complete": "final",
    }.get(phase_key, "decompose")


def manager_legacy_plan_sync_payload(plan: ProjectPlan) -> Dict[str, Any]:
    tasks = list(getattr(plan, "tasks", []) or [])
    raw_current_task_id = getattr(plan, "current_task_id", None)
    current_task_id = str(raw_current_task_id).strip() if raw_current_task_id is not None else None
    task_statuses = {
        str(getattr(task, "id", "") or "").strip(): str(getattr(task, "status", "") or "").strip()
        for task in tasks
        if str(getattr(task, "id", "") or "").strip()
    }
    approved_task_ids = [
        str(getattr(task, "id", "") or "").strip()
        for task in tasks
        if str(getattr(task, "id", "") or "").strip() and str(getattr(task, "status", "") or "").strip() == "approved"
    ]
    return {
        "synced": True,
        "task_count": len(tasks),
        "legacy_updated_at": str(getattr(plan, "updated_at", "") or "").strip(),
        "legacy_status": str(getattr(plan, "status", "") or "").strip(),
        "current_task_id": current_task_id or None,
        "completion_report_present": bool(str(getattr(plan, "completion_report", "") or "").strip()),
        "approved_task_ids": approved_task_ids,
        "task_statuses": task_statuses,
    }


def manager_run_plan_payload(plan: ProjectPlan, *, phase: str) -> Dict[str, Any]:
    sync_payload = manager_legacy_plan_sync_payload(plan)
    units = []
    for task in list(getattr(plan, "tasks", []) or []):
        units.append(
            {
                "id": str(getattr(task, "id", "") or "").strip(),
                "title": str(getattr(task, "title", "") or "").strip(),
                "status": str(getattr(task, "status", "") or "").strip(),
                "attempt": int(getattr(task, "attempt", 0) or 0),
                "max_attempts": int(getattr(task, "max_attempts", 0) or 0),
                "depends_on": [str(item).strip() for item in list(getattr(task, "depends_on", []) or []) if str(item).strip()],
                "covers_requirements": [
                    str(item).strip()
                    for item in list(getattr(task, "covers_requirements", []) or [])
                    if str(item).strip()
                ],
                "acceptance_criteria": [
                    str(item).strip()
                    for item in list(getattr(task, "acceptance_criteria", []) or [])
                    if str(item).strip()
                ],
                "has_dev_report": bool(str(getattr(task, "dev_report", "") or "").strip()),
                "has_review_comments": bool(str(getattr(task, "review_comments", "") or "").strip()),
                "review_verdict": str(getattr(task, "review_verdict", "") or "").strip() or None,
            }
        )
    return {
        "plan_kind": "manager_plan",
        "task_family": "manager",
        "project_goal": str(getattr(plan, "project_goal", "") or "").strip(),
        "legacy_status": str(getattr(plan, "status", "") or "").strip(),
        "current_task_id": str(getattr(plan, "current_task_id", "") or "").strip() or None,
        "completion_report_present": bool(str(getattr(plan, "completion_report", "") or "").strip()),
        "legacy_plan_sync": sync_payload,
        "units": units,
        "boundary_map": [
            {"legacy_phase": "decompose", "run_phase": "plan"},
            {"legacy_phase": "dev", "run_phase": "develop"},
            {"legacy_phase": "review", "run_phase": "review"},
            {"legacy_phase": "final", "run_phase": "complete"},
        ],
        "validation_contracts": [{"phase": phase, "legacy_phase": manager_legacy_phase_for_run_phase(phase)}],
    }


def manager_run_state_context_from_plan(plan: ProjectPlan, *, phase: str) -> Dict[str, Any]:
    sync_payload = manager_legacy_plan_sync_payload(plan)
    current_task_id = str(getattr(plan, "current_task_id", "") or "").strip()
    current_task = None
    for task in list(getattr(plan, "tasks", []) or []):
        if current_task_id and str(getattr(task, "id", "") or "").strip() == current_task_id:
            current_task = task
            break
    if current_task is None:
        for task in list(getattr(plan, "tasks", []) or []):
            task_status = str(getattr(task, "status", "") or "").strip().lower()
            if task_status in {"in_progress", "in_review"}:
                current_task = task
                break
    mode_context = {
        "legacy_phase": manager_legacy_phase_for_run_phase(phase),
        "legacy_plan_sync_status": "live",
        "legacy_plan_sync": sync_payload,
        "decompose_payload_valid": bool(list(getattr(plan, "tasks", []) or [])),
        "dynamic_validation_passed": bool(getattr(plan, "analysis", None) is not None),
    }
    if current_task is not None:
        dev_report = str(getattr(current_task, "dev_report", "") or "").strip()
        review_verdict = str(getattr(current_task, "review_verdict", "") or "").strip()
        review_comments = str(getattr(current_task, "review_comments", "") or "").strip()
        if dev_report:
            mode_context["dev_report"] = dev_report
            mode_context["task_status_consistent"] = True
        if review_verdict or review_comments:
            mode_context["review_payload_valid"] = True
            mode_context["review_decision_outcome"] = review_verdict or "rejected"
    final_report = str(getattr(plan, "completion_report", "") or "").strip()
    if final_report:
        mode_context["final_report"] = final_report
    return mode_context


def manager_apply_persisted_plan_metadata(target: ProjectPlan, persisted: ProjectPlan) -> ProjectPlan:
    """
    Copy persistence-side metadata back onto the in-memory plan without
    swapping task objects that may still be referenced by the current loop.
    """
    target.project_goal = str(
        getattr(persisted, "project_goal", "") or getattr(target, "project_goal", "")
    )
    target.status = str(getattr(persisted, "status", "") or getattr(target, "status", ""))
    target.created_at = str(
        getattr(persisted, "created_at", "") or getattr(target, "created_at", "")
    )
    target.updated_at = str(
        getattr(persisted, "updated_at", "") or getattr(target, "updated_at", "")
    )
    target.current_task_id = getattr(
        persisted,
        "current_task_id",
        getattr(target, "current_task_id", None),
    )
    target.completion_report = getattr(
        persisted,
        "completion_report",
        getattr(target, "completion_report", None),
    )
    if getattr(persisted, "analysis", None) is not None:
        target.analysis = getattr(persisted, "analysis", None)
    max_tasks_limit = int(getattr(persisted, "_manager_max_tasks_limit", 0) or 0)
    if max_tasks_limit > 0:
        setattr(target, "_manager_max_tasks_limit", max_tasks_limit)
    return target


# ---------------------------------------------------------------------------
# ManagerOrchestrator
# ---------------------------------------------------------------------------


class ManagerOrchestrator:
    """
    Manager mode: CLI does development, Agent (Executor) does review.
    All LLM calls here must use defaults.openai_model (see TZ_MANAGER.md).
    """

    _MANAGER_NOTIFICATION_TIMEOUT_SEC = 10.0

    def __init__(
        self,
        config: AppConfig,
        *,
        plan_service: Optional[PlanManagementService] = None,
        execution_service: Optional[ExecutionTrackingService] = None,
        review_service: Optional[ReviewAndMergeService] = None,
        ui_service: Optional[ManagerUIService] = None,
    ) -> None:
        self._config = config
        tool_registry = get_tool_registry(config)
        self._executor = Executor(config, tool_registry)
        self._plan_service = plan_service or PlanManagementService(
            max_attempts=int(getattr(config.defaults, "manager_max_attempts", 3) or 3)
        )
        self._execution_service = execution_service or ExecutionTrackingService(
            min_tasks_floor=int(MIN_TASKS_FLOOR),
            min_tasks_per_req=int(MIN_TASKS_PER_REQ),
            min_tasks_per_remaining=int(MIN_TASKS_PER_REMAINING),
        )
        self._review_service = review_service or ReviewAndMergeService(plan_service=self._plan_service)
        self._ui_service = ui_service or ManagerUIService()
        self._git_reconcile_service = GitReconcileService(self)
        # Cache git capability per workdir to avoid repeated filesystem walks / subprocess calls.
        # Values are: True if git is usable for this workdir (git binary exists AND a .git is present in this tree).
        self._git_usable_cache: Dict[str, bool] = {}

    def __getattr__(self, name: str):
        # Backward-compatible lazy init for tests that instantiate via object.__new__
        # and bypass __init__.
        if name == "_plan_service":
            defaults = getattr(getattr(self, "_config", None), "defaults", None)
            try:
                max_attempts = int(getattr(defaults, "manager_max_attempts", 3) or 3)
            except Exception:
                max_attempts = 3
            svc = PlanManagementService(max_attempts=max_attempts)
            setattr(self, "_plan_service", svc)
            return svc
        if name == "_execution_service":
            svc = ExecutionTrackingService(
                min_tasks_floor=int(MIN_TASKS_FLOOR),
                min_tasks_per_req=int(MIN_TASKS_PER_REQ),
                min_tasks_per_remaining=int(MIN_TASKS_PER_REMAINING),
            )
            setattr(self, "_execution_service", svc)
            return svc
        if name == "_review_service":
            svc = ReviewAndMergeService(plan_service=self._plan_service)
            setattr(self, "_review_service", svc)
            return svc
        if name == "_ui_service":
            svc = ManagerUIService()
            setattr(self, "_ui_service", svc)
            return svc
        if name == "_git_reconcile_service":
            svc = GitReconcileService(self)
            setattr(self, "_git_reconcile_service", svc)
            return svc
        raise AttributeError(name)

    def _artifact_store(self) -> Optional[RunArtifactStore]:
        config = getattr(self, "_config", None)
        if config is None:
            return None
        return RunArtifactStore(config)

    @staticmethod
    def _active_run_handle(session: Any) -> Optional[RunArtifactHandle]:
        handle = getattr(session, _MANAGER_RUN_HANDLE_SESSION_ATTR, None)
        return handle if isinstance(handle, RunArtifactHandle) else None

    @staticmethod
    def _run_resume_guard(session: Any) -> Dict[str, Any]:
        raw = getattr(session, _MANAGER_RUN_RESUME_GUARD_SESSION_ATTR, {})
        return dict(raw) if isinstance(raw, dict) else {}

    def _sync_active_run_from_legacy_plan(
        self,
        session: Any,
        plan: Optional[ProjectPlan],
        *,
        phase: Optional[str] = None,
        status: Optional[str] = None,
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ProjectPlan]:
        run = self._active_run_handle(session)
        artifact_store = self._artifact_store()
        if run is None or artifact_store is None or plan is None:
            return plan

        resolved_phase = manager_run_phase_for_plan(plan, fallback=phase or "plan")
        current_state = artifact_store.load_state(run)
        merged_mode_context = dict(current_state.get("mode_context") or {})
        merged_mode_context.update(manager_run_state_context_from_plan(plan, phase=resolved_phase))
        merged_mode_context.update(dict(mode_context or {}))
        resume_guard = self._run_resume_guard(session)
        if resume_guard and "resume_guard" not in merged_mode_context:
            merged_mode_context["resume_guard"] = dict(resume_guard)

        run_status = str(status or current_state.get("status") or "running").strip() or "running"
        legacy_status = str(getattr(plan, "status", "") or "").strip().lower()
        if legacy_status == "completed":
            run_status = "completed"
        elif legacy_status == "failed":
            run_status = "failed"
        elif legacy_status == "paused":
            run_status = "paused"

        artifact_store.save_plan(run, manager_run_plan_payload(plan, phase=resolved_phase))
        artifact_store.save_state(
            run,
            {
                "phase": resolved_phase,
                "status": run_status,
                "last_successful_phase": (
                    resolved_phase
                    if run_status in {"completed", "paused"}
                    else current_state.get("last_successful_phase")
                ),
                "mode_context": merged_mode_context,
            },
        )
        previous_phase = str(current_state.get("phase") or "").strip()
        if resolved_phase != previous_phase:
            artifact_store.append_checkpoint(
                run,
                {
                    "phase": resolved_phase,
                    "unit_id": f"manager:{resolved_phase}",
                    "status": "ok",
                    "legacy_phase": manager_legacy_phase_for_run_phase(resolved_phase),
                    "legacy_status": str(getattr(plan, "status", "") or "").strip(),
                    "current_task_id": str(getattr(plan, "current_task_id", "") or "").strip() or None,
                },
            )
        return plan

    @staticmethod
    def _plan_scoped_key(session: Any) -> Optional[str]:
        token = str(session_scoped_key(session) or "").strip()
        return token or None

    def _load_live_plan(self, session: Any) -> Optional[ProjectPlan]:
        return load_plan(getattr(session, "workdir", ""), scoped_key=self._plan_scoped_key(session))

    def _save_live_plan(self, session: Any, plan: ProjectPlan) -> None:
        save_plan(getattr(session, "workdir", ""), plan, scoped_key=self._plan_scoped_key(session))

    def _archive_live_plan(self, session: Any, status: str) -> Optional[str]:
        return archive_plan(getattr(session, "workdir", ""), status, scoped_key=self._plan_scoped_key(session))

    def _delete_live_plan(self, session: Any) -> None:
        delete_plan(getattr(session, "workdir", ""), scoped_key=self._plan_scoped_key(session))

    def _save_plan_with_run_artifacts(
        self,
        session: Any,
        plan: ProjectPlan,
        *,
        phase: Optional[str] = None,
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> ProjectPlan:
        self._save_live_plan(session, plan)
        persisted = self._load_live_plan(session) or plan
        resolved = manager_apply_persisted_plan_metadata(plan, persisted)
        self._sync_active_run_from_legacy_plan(session, resolved, phase=phase, mode_context=mode_context)
        return resolved

    def _mark_active_run_legacy_plan_archived(
        self,
        session: Any,
        *,
        phase: str = "complete",
    ) -> None:
        run = self._active_run_handle(session)
        artifact_store = self._artifact_store()
        if run is None or artifact_store is None:
            return
        current_state = artifact_store.load_state(run)
        merged_mode_context = dict(current_state.get("mode_context") or {})
        merged_mode_context["legacy_phase"] = manager_legacy_phase_for_run_phase(phase)
        merged_mode_context["legacy_plan_sync_status"] = "archived"
        artifact_store.save_state(
            run,
            {
                "phase": phase,
                "status": current_state.get("status") or "running",
                "mode_context": merged_mode_context,
            },
        )

    @staticmethod
    def _issue_code(raw_issue: object) -> str:
        return str(raw_issue or "").split(":", 1)[0].strip().upper()

    @classmethod
    def _is_goal_alignment_issue(cls, raw_issue: object) -> bool:
        issue = str(raw_issue or "").strip()
        if not issue:
            return False
        code = cls._issue_code(issue)
        if code == _GOAL_ALIGNMENT_ISSUE_TAG:
            return True
        return any(pattern.search(issue) for pattern in _GOAL_ALIGNMENT_ISSUE_PATTERNS)

    @classmethod
    def _issue_tags(cls, issues: List[str]) -> set[str]:
        tags: set[str] = set()
        for raw_issue in (issues or []):
            code = cls._issue_code(raw_issue)
            if code:
                tags.add(code)
            if cls._is_goal_alignment_issue(raw_issue):
                tags.add(_GOAL_ALIGNMENT_ISSUE_TAG)
        return tags

    @classmethod
    def _issues_are_goal_alignment_only(cls, issues: List[str]) -> bool:
        cleaned = [str(x or "").strip() for x in (issues or []) if str(x or "").strip()]
        if not cleaned:
            return False
        return all(cls._is_goal_alignment_issue(x) for x in cleaned)

    @staticmethod
    def _should_stabilize_task_count(issue_tags_history: List[set[str]]) -> bool:
        if len(issue_tags_history) < 2:
            return False
        recent = issue_tags_history[-3:]
        has_floor_issue = any("TASK_COUNT_BELOW_MIN" in tags for tags in recent)
        has_goal_alignment_issue = any(_GOAL_ALIGNMENT_ISSUE_TAG in tags for tags in recent)
        return has_floor_issue and has_goal_alignment_issue

    @staticmethod
    def _collect_atomicity_hotspots(plan: ProjectPlan) -> List[Dict[str, Any]]:
        """Return current tasks that already exceed the covers_requirements atomicity limit."""
        req_labels: Dict[str, str] = {}
        if plan.analysis and isinstance(plan.analysis.requirements, list):
            for raw_req in plan.analysis.requirements:
                req_label = str(raw_req).strip()
                if not req_label:
                    continue
                req_id = _normalize_requirement_ref(req_label)
                if req_id and req_id not in req_labels:
                    req_labels[req_id] = req_label
        if not req_labels:
            return []

        hotspots: List[Dict[str, Any]] = []
        for task in list(getattr(plan, "tasks", []) or []):
            covers = [
                _normalize_requirement_ref(x)
                for x in (task.covers_requirements or [])
                if _normalize_requirement_ref(x)
            ]
            if len(covers) <= int(ATOMICITY_MAX_REQS_PER_TASK):
                continue
            hotspots.append(
                {
                    "task_id": str(task.id or ""),
                    "title": str(task.title or ""),
                    "covers_requirements": len(covers),
                    "max_allowed": int(ATOMICITY_MAX_REQS_PER_TASK),
                    "requirement_ids": list(covers),
                }
            )
        return hotspots

    @staticmethod
    def _normalize_status(status: str) -> str:
        key = str(status or "").strip().lower()
        return STATUS_ALIASES.get(key, key)

    @staticmethod
    def _should_ignore_dirname(name: str) -> bool:
        return ReviewAndMergeService.should_ignore_dirname(name)

    @staticmethod
    def _should_ignore_relpath(rel_path: str) -> bool:
        return ReviewAndMergeService.should_ignore_relpath(rel_path)

    @staticmethod
    def _hash_file(path: str, *, max_bytes: int) -> Optional[str]:
        return ReviewAndMergeService.hash_file(path, max_bytes=max_bytes)

    def _snapshot_workdir(
        self,
        workdir: str,
        *,
        max_files: int = 50_000,
        hash_max_bytes: int = 256 * 1024,
    ) -> Dict[str, Dict[str, object]]:
        return self._git_reconcile_service.snapshot_workdir(
            workdir,
            max_files=max_files,
            hash_max_bytes=hash_max_bytes,
        )

    @staticmethod
    def _diff_snapshots(
        before: Dict[str, Dict[str, object]],
        after: Dict[str, Dict[str, object]],
    ) -> Dict[str, object]:
        return GitReconcileService.diff_snapshots(before, after)

    @staticmethod
    def _format_change_audit(diff: Dict[str, object], *, max_list: int = 50) -> str:
        return ReviewAndMergeService.format_change_audit(diff, max_list=max_list)

    async def _git_change_audit(self, workdir: str, *, max_lines: int = 200) -> Tuple[str, bool]:
        """
        Lightweight change audit based on git, for cases when we don't want to snapshot the filesystem.
        Returns (audit_text, has_changes).
        """
        if not self._git_is_usable(workdir):
            return ("", False)
        code, status_out = await self._run_git(workdir, ["status", "--porcelain"])
        if code != 0:
            return ("", False)
        status_out = (status_out or "").strip("\n")
        if not status_out.strip():
            return ("", False)
        # Add a compact name-status list for context (can be large; cap it).
        code, name_out = await self._run_git(workdir, ["diff", "--name-status"])
        name_out = (name_out or "").strip("\n") if code == 0 else ""
        status_lines = status_out.splitlines()
        name_lines = name_out.splitlines() if name_out else []

        status_lines, status_summ = filter_git_porcelain_lines(status_lines)
        name_summ = None
        if name_lines:
            name_lines, name_summ = filter_git_name_status_lines(name_lines)

        def _cap(lines: List[str], raw_len: int) -> List[str]:
            if len(lines) <= max_lines:
                return lines
            return lines[:max_lines] + [f"... (+{raw_len - max_lines} more)"]

        status_lines = _cap(status_lines, len(status_out.splitlines()))
        if name_lines:
            name_lines = _cap(name_lines, len(name_out.splitlines()))

        parts: List[str] = []
        parts.append("### Изменения в рабочем дереве (git)")
        parts.append("")
        parts.append("git status --porcelain:")
        parts.append("```")
        parts.extend(status_lines)
        parts.append("```")
        note = status_summ.format_ru()
        if note:
            parts.append(note)
        if name_lines:
            parts.append("")
            parts.append("git diff --name-status:")
            parts.append("```")
            parts.extend(name_lines)
            parts.append("```")
            if name_summ is not None:
                note2 = name_summ.format_ru()
                if note2:
                    parts.append(note2)
        return ("\n".join(parts).strip(), True)

    @staticmethod
    def _find_git_marker_root(workdir: str) -> Optional[str]:
        """
        Best-effort "is git used here?" check without invoking git:
        walk up parents until filesystem root and look for a `.git` file/dir.
        """
        d = os.path.abspath(workdir or ".")
        while True:
            if os.path.exists(os.path.join(d, ".git")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent

    def _git_is_usable(self, workdir: str) -> bool:
        """
        True if:
        - `git` executable is available, and
        - the project appears to use git (a `.git` marker exists in this directory tree).

        If False, Manager must not attempt auto-commit or run git commands for reporting.
        """
        wd = os.path.abspath(workdir or ".")
        cache = getattr(self, "_git_usable_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            try:
                self._git_usable_cache = cache
            except Exception:
                # If object was constructed in tests via object.__new__, still keep a local cache.
                _log.debug("git usable cache attach skipped", exc_info=True)
        cached = cache.get(wd)
        if cached is not None:
            return cached
        has_git_bin = bool(shutil.which("git"))
        has_git_marker = self._find_git_marker_root(wd) is not None
        usable = bool(has_git_bin and has_git_marker)
        cache[wd] = usable
        return usable

    @staticmethod
    def _load_manager_prompts(workdir: str) -> Dict[str, str]:
        ensure_project_prompts(workdir)
        return load_mode_prompt_texts(workdir, "manager")

    def _manager_prompt(self, workdir: str, key: str) -> str:
        prompts = self._load_manager_prompts(workdir)
        value = prompts.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"manager project prompts missing key: {key}")
        return value

    def _load_manager_prompt_learning(self, workdir: str) -> Dict[str, Any]:
        ensure_project_prompts(workdir)
        payload = load_mode_learning(workdir, "manager")
        raw_patches = payload.get("patches")
        patches: List[Dict[str, Any]] = []
        if isinstance(raw_patches, list):
            for raw_patch in raw_patches:
                normalized_patch = _normalize_general_learning_patch(raw_patch)
                if normalized_patch is not None:
                    patches.append(normalized_patch)
        normalized = {
            "patches": patches,
            "active_version": int(payload.get("active_version", 1) or 1),
        }
        try:
            if payload.get("patches") != normalized["patches"]:
                self._save_manager_prompt_learning(workdir, normalized)
        except Exception:
            _log.exception("manager prompt learning normalize-rewrite failed workdir=%s", workdir)
        return normalized

    def _save_manager_prompt_learning(self, workdir: str, learning: Dict[str, Any]) -> None:
        payload = learning if isinstance(learning, dict) else {"patches": [], "active_version": 1}
        raw_patches = payload.get("patches")
        patches: List[Dict[str, Any]] = []
        if isinstance(raw_patches, list):
            for raw_patch in raw_patches:
                normalized_patch = _normalize_general_learning_patch(raw_patch)
                if normalized_patch is not None:
                    patches.append(normalized_patch)
        payload["patches"] = patches
        payload["active_version"] = int(payload.get("active_version", 1) or 1)
        try:
            save_mode_learning(workdir, "manager", payload)
        except Exception:
            _log.exception("manager prompt learning save failed workdir=%s", workdir)

    def _apply_manager_prompt_learning(self, workdir: str, base_prompt: str) -> str:
        learning = self._load_manager_prompt_learning(workdir)
        patches = learning.get("patches") if isinstance(learning, dict) else []
        if not isinstance(patches, list) or not patches:
            return str(base_prompt or "")

        added_rules: List[str] = []
        changed_rules: List[str] = []
        removed_rules: List[str] = []
        reasons: List[str] = []
        for idx, patch in enumerate(patches[-20:], start=1):
            if not isinstance(patch, dict):
                continue
            added_rules.extend(_rule_items(patch.get("added_rules")))
            changed_rules.extend(_rule_items(patch.get("changed_rules")))
            removed_rules.extend(_rule_items(patch.get("removed_rules")))
            reason = str(patch.get("reason") or "").strip()
            if reason:
                reasons.append(f"{idx}. {reason}")

        lines = [str(base_prompt or "").strip()]
        added_rules = _dedupe_items(added_rules)
        changed_rules = _dedupe_items(changed_rules)
        removed_rules = _dedupe_items(removed_rules)
        if added_rules or changed_rules or removed_rules:
            lines.append("")
            lines.append("Дополнительные правила manager (накопленные коррекции):")
        if added_rules:
            lines.append("Новые правила:")
            lines.extend([f"- {x}" for x in added_rules])
        if changed_rules:
            lines.append("Измененные правила:")
            lines.extend([f"- {x}" for x in changed_rules])
        if removed_rules:
            lines.append("Отмененные правила:")
            lines.extend([f"- {x}" for x in removed_rules])
        if reasons:
            lines.append("Обоснования:")
            lines.extend(reasons)
        return "\n".join(lines).strip()

    def _with_invariant_policy(self, workdir: str, prompt_text: str) -> str:
        base = str(prompt_text or "").strip()
        policy = str(self._manager_prompt(workdir, "invariant_policy") or "").strip()
        if not policy:
            return base
        return f"{policy}\n\n{base}".strip()

    async def _send_adapter_message(self, bot, context, **kwargs):
        send_message = getattr(bot, "send_message", None)
        if callable(send_message):
            return await send_message(context, **kwargs)

        legacy_send_message = getattr(bot, "_send_message", None)
        if callable(legacy_send_message):
            _log.warning(
                "manager runtime send_message legacy fallback used bot_type=%s chat_id=%s",
                type(bot).__name__,
                kwargs.get("chat_id"),
            )
            return await legacy_send_message(context, **kwargs)

        raise RuntimeError("Manager runtime adapter send_message is not configured")

    async def _send_runtime_message(
        self,
        session: Session,
        bot,
        context,
        *,
        chat_id: Optional[int],
        text: str,
        important: bool = False,
        **kwargs,
    ) -> None:
        """Send manager runtime message respecting per-session quiet mode."""
        if chat_id is None:
            return
        quiet_mode = bool(
            getattr(getattr(session, "modes", None), "manager_quiet_mode", getattr(session, "manager_quiet_mode", False))
        )
        if quiet_mode and not important:
            return
        await self._send_adapter_message(bot, context, chat_id=chat_id, text=text, **kwargs)

    async def _send_runtime_message_best_effort(
        self,
        session: Session,
        bot,
        context,
        *,
        stage: str,
        task_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        timeout = float(self._MANAGER_NOTIFICATION_TIMEOUT_SEC)
        try:
            await asyncio.wait_for(
                self._send_runtime_message(session, bot, context, **kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "manager runtime message timed out stage=%s task_id=%s timeout_sec=%.1f",
                stage,
                task_id,
                timeout,
            )
        except Exception:
            _log.exception(
                "manager runtime message failed stage=%s task_id=%s",
                stage,
                task_id,
            )

    async def _send_final_report(
        self,
        session: Session,
        bot,
        context,
        dest: dict,
        report: str,
    ) -> None:
        """
        Send final Manager report through the common output path so large texts
        are automatically delivered as an attachment.
        """
        # `send_output` is mandatory in bot interface.
        # TODO(M3): route large output via a transport-agnostic MessagingService.send_large_output when available.
        await bot.send_output(session, dest, report, context, send_header=False)

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    async def run(self, session: Session, user_text: str, bot, context, dest: dict) -> str:
        chat_id = dest.get("chat_id")
        workdir = session.workdir
        # Determine whether git is actually used/available for this project before doing any Manager work.
        # If not used, Manager must not attempt auto-commit or run git commands for "change verification".
        git_usable = self._git_is_usable(workdir)
        try:
            session.manager_git_usable = git_usable  # runtime-only hint for UI inspection
        except Exception:
            _log.debug("manager_git_usable runtime hint attach skipped", exc_info=True)
        plan = self._load_live_plan(session)
        txt = (user_text or "").strip()

        if (
            plan
            and plan.status == "paused"
            and txt == MANAGER_CONTINUE_TOKEN
        ):
            # Explicit resume from pause.
            plan.set_status("active")
            plan.updated_at = _now_iso()
            plan = self._save_plan_with_run_artifacts(session, plan, phase=manager_run_phase_for_plan(plan, fallback="develop"))
            if chat_id is not None:
                await self._send_adapter_message(bot, context, chat_id=chat_id, text="▶️ Возобновляю приостановленный план...")
        elif plan and plan.status == "active" and (self._config.defaults.manager_auto_resume or txt == MANAGER_CONTINUE_TOKEN):
            _log.debug("manager continuing active plan without status transition")
        elif (
            plan
            and plan.status == "failed"
            and self._can_resume_failed(plan)
            and (self._config.defaults.manager_auto_resume or txt == MANAGER_CONTINUE_TOKEN)
        ):
            # Plan was failed (timeout / partial) but has retryable tasks — resume it.
            plan.set_status("active")
            plan.updated_at = _now_iso()
            plan = self._save_plan_with_run_artifacts(session, plan, phase=manager_run_phase_for_plan(plan, fallback="develop"))
            if chat_id is not None:
                await self._send_adapter_message(bot, context, chat_id=chat_id, text="🔄 Возобновляю ранее остановленный план...")
        else:
            plan = await self._start_new_plan(session, user_text, bot, context, dest)

        if not plan:
            return "manager: no plan"

        await self._notify_plan(session, plan, bot, context, dest)
        await self._run_loop(session, plan, bot, context, dest)

        # Final report
        if plan.status == "completed":
            final_audit = await self._run_final_spec_audit_and_close_gaps(
                session=session,
                plan=plan,
                bot=bot,
                context=context,
                dest=dest,
                original_goal=str(plan.project_goal or user_text or "").strip(),
            )
            if not final_audit.get("passed", False):
                plan.set_status("failed")
                plan = self._save_plan_with_run_artifacts(session, plan, phase="complete")
                if chat_id is not None:
                    await self._send_adapter_message(
                        bot,
                        context,
                        chat_id=chat_id,
                        text="❌ Финальная проверка по исходному ТЗ не пройдена. План помечен как failed.",
                    )

            report = await self._compose_final_report(plan, workdir=workdir)
            final_audit_summary = str(final_audit.get("summary_text") or "").strip()
            if final_audit_summary:
                report = f"{report}\n\n---\n\n{final_audit_summary}".strip()
            plan.completion_report = report
            plan = self._save_plan_with_run_artifacts(session, plan, phase="complete")
            if chat_id is not None:
                done_text = (
                    "✅ Готово. Результат ниже."
                    if plan.status == "completed"
                    else "⚠️ Основной план завершен, но финальная проверка по ТЗ выявила незакрытые gap. Результат ниже."
                )
                await self._send_adapter_message(bot, context, chat_id=chat_id, text=done_text)
                await self._send_final_report(session, bot, context, dest, report)
            if plan.status == "completed":
                self._archive_live_plan(session, plan.status)
                self._mark_active_run_legacy_plan_archived(session, phase="complete")
        elif plan.status == "failed":
            report = await self._compose_final_report(plan, workdir=workdir)
            plan.completion_report = report
            plan = self._save_plan_with_run_artifacts(session, plan, phase="complete")
            if chat_id is not None:
                await self._send_adapter_message(bot, context, chat_id=chat_id, text="❌ План провален. Результат ниже.")
                try:
                    await self._send_final_report(session, bot, context, dest, report)
                except Exception as e:
                    _log.exception("failed to send manager final report: %s", e)
                    await self._send_adapter_message(
                        bot,
                        context,
                        chat_id=chat_id,
                        text="⚠️ Не удалось отправить полный отчёт. Выберите следующее действие.",
                    )
                # Ask user: retry or archive? (must always be sent even if report delivery failed)
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                from modes.sdk.services.callback_data import build_mode_action_callback_data
                mode_id = str(get_active_mode(session, "") or "").strip() or "manager"
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔄 Повторить",
                            callback_data=build_mode_action_callback_data(mode_id, "failed_retry", session=session),
                        ),
                        InlineKeyboardButton(
                            "📦 В архив",
                            callback_data=build_mode_action_callback_data(mode_id, "failed_archive", session=session),
                        ),
                    ],
                ])
                await self._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text="Что сделать с проваленным планом?",
                    reply_markup=keyboard,
                )

        full_report = str(getattr(plan, "completion_report", "") or "").strip()
        if full_report:
            return full_report
        return self._ui_service.plan_summary(plan)

    # -----------------------------------------------------------------------
    # Plan creation
    # -----------------------------------------------------------------------

    async def _start_new_plan(self, session: Session, user_text: str, bot, context, dest: dict) -> Optional[ProjectPlan]:
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            await self._send_adapter_message(bot, context, chat_id=chat_id, text="🏗 Manager: декомпозиция задачи и анализ проекта...")
        plan = await self._decompose(session, user_text, bot=bot, context=context, dest=dest)
        if not plan:
            if chat_id is not None:
                await self._send_adapter_message(bot, context, chat_id=chat_id, text="Не удалось построить план Manager.")
            return None
        return self._save_plan_with_run_artifacts(
            session,
            plan,
            phase="plan",
            mode_context={
                "decompose_payload_valid": True,
                "dynamic_validation_passed": bool(getattr(plan, "analysis", None) is not None),
            },
        )

    # -----------------------------------------------------------------------
    # Decomposition (two-phase: CLI → direct JSON parse → Agent normalization)
    # -----------------------------------------------------------------------

    async def _decompose(
        self, session: Session, user_goal: str, bot=None,
        context=None, dest: Optional[dict] = None,
    ) -> Optional[ProjectPlan]:
        chat_id = dest.get("chat_id") if isinstance(dest, dict) else None
        timeout = int(self._config.defaults.manager_decompose_timeout_sec)
        max_tasks = int(self._config.defaults.manager_max_tasks)
        min_tasks_dynamic = _min_tasks_dynamic(None)
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        workdir = session.workdir
        instr = self._manager_prompt(workdir, "decompose_instruction").format(
            user_goal=user_goal,
            max_tasks=max_tasks,
            min_tasks_dynamic=min_tasks_dynamic,
            max_requirements_per_task=int(ATOMICITY_MAX_REQS_PER_TASK),
        )
        instr = self._with_invariant_policy(workdir, instr)
        instr = self._apply_manager_prompt_learning(workdir, instr)
        if not self._git_is_usable(workdir):
            # Avoid prompting the CLI to run git commands in projects without git metadata.
            instr = instr.replace(
                "- Проверь git status и git log (последние коммиты).\n",
                "- Git в проекте не используется: пропусти git status/git log.\n",
            )

        if archive_enabled:
            _archive_response_write(workdir, "manager_decompose_prompt", "Decompose Prompt → CLI", instr)

        # === Phase 1: CLI analyzes the project ===
        try:
            cli_used, cli_text = await run_prompt_routed_meta(
                session,
                self._config,
                "planning",
                instr,
                response_format=CLIResponseFormat.JSON_OBJECT,
                timeout_sec=timeout,
                chat_id=chat_id,
            )
            _log.info("decompose: used cli=%s", cli_used)
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                _log.exception("decompose: interrupt after timeout failed")
            _log.warning("decompose: CLI timeout (%ds)", timeout)
            return None
        except Exception as exc:
            _log.warning("decompose: CLI error: %s", exc)
            return None

        cli_text = strip_ansi(cli_text or "")
        _log.info("decompose phase 1 done: CLI output %d chars", len(cli_text))

        if archive_enabled:
            _archive_response_write(workdir, "cli_decompose_response", "CLI Decompose Response", cli_text)

        # === Try direct JSON parse ===
        plan = self._try_parse_plan(cli_text, user_goal, max_tasks)
        if plan:
            _log.info("decompose: direct parse succeeded")
            if archive_enabled:
                _archive_response_write(
                    workdir,
                    "manager_decompose_result",
                    "Decompose Result (direct parse)",
                    json.dumps(asdict(plan), ensure_ascii=False, indent=2),
                )
        else:
            # === Phase 2: Agent normalization (fallback) ===
            _log.info("decompose: direct parse failed, invoking agent normalization")
            plan = await self._normalize_plan(cli_text, user_goal, max_tasks, workdir=workdir)
            if not plan:
                # Retry normalization with strict mode
                _log.warning("decompose phase 2: first normalization failed, retrying strict")
                plan = await self._normalize_plan(cli_text, user_goal, max_tasks, strict=True, workdir=workdir)

        if not plan:
            _log.error("decompose: all normalization attempts failed")
            raise ManagerDecomposeNormalizationError()

        raw_min_tasks_dynamic = _min_tasks_dynamic(getattr(plan, "analysis", None))
        frozen_min_tasks_dynamic = min(raw_min_tasks_dynamic, max_tasks)
        setattr(plan, "_manager_min_tasks_dynamic", int(frozen_min_tasks_dynamic))
        setattr(plan, "_manager_max_tasks_limit", int(max_tasks))
        if raw_min_tasks_dynamic > max_tasks:
            _log.warning(
                "decompose: min_tasks_dynamic=%d exceeds max_tasks=%d, clamping validation threshold",
                raw_min_tasks_dynamic,
                max_tasks,
            )

        # === Phase 3: Validate plan (up to N correction attempts) ===
        chat_id = (dest or {}).get("chat_id")
        max_fix_attempts = int(self._config.defaults.manager_max_attempts) + 4
        issue_tags_history: List[set[str]] = []
        floor_issue_seen = False
        for fix_attempt in range(1, max_fix_attempts + 1):
            setattr(plan, "_manager_min_tasks_dynamic", int(frozen_min_tasks_dynamic))
            setattr(plan, "_manager_max_tasks_limit", int(max_tasks))
            actual_tasks = len(list(getattr(plan, "tasks", []) or []))
            min_tasks_dynamic = int(frozen_min_tasks_dynamic)
            if chat_id is not None and bot is not None:
                await self._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"🔎 Валидация плана (проверка {fix_attempt}/{max_fix_attempts})...",
                )
            issues = await self._validate_plan(plan, workdir)
            if not issues:
                _log.info(
                    "decompose: plan validation passed (attempt %d/%d) "
                    "min_tasks_dynamic=%d max_tasks=%d actual_tasks=%d",
                    fix_attempt,
                    max_fix_attempts,
                    min_tasks_dynamic,
                    max_tasks,
                    actual_tasks,
                )
                if chat_id is not None and bot is not None:
                    await self._send_adapter_message(bot, context, chat_id=chat_id, text="✅ План прошёл валидацию")
                break

            issue_tags = self._issue_tags(issues)
            issue_tags_history.append(set(issue_tags))
            if "TASK_COUNT_BELOW_MIN" in issue_tags:
                floor_issue_seen = True
            if (
                floor_issue_seen
                and actual_tasks >= min_tasks_dynamic
                and self._issues_are_goal_alignment_only(issues)
            ):
                _log.warning(
                    "decompose: accepting plan with goal-alignment semantic warnings after floor reached "
                    "(attempt %d/%d) min_tasks_dynamic=%d actual_tasks=%d issues=%s",
                    fix_attempt,
                    max_fix_attempts,
                    min_tasks_dynamic,
                    actual_tasks,
                    issues,
                )
                if chat_id is not None and bot is not None:
                    await self._send_adapter_message(
                        bot,
                        context,
                        chat_id=chat_id,
                        text=(
                            "⚠️ Семантические замечания по формулировке этапов не влияют на покрытие ТЗ. "
                            "План принят без снижения количества задач."
                        ),
                    )
                break

            issues_short = "; ".join(issues[:3])
            if len(issues) > 3:
                issues_short += f" (+ещё {len(issues) - 3})"
            replan_reason = str(issues[0] if issues else "unknown_issue").strip() or "unknown_issue"
            _log.warning("decompose: plan validation failed (attempt %d/%d): %s",
                         fix_attempt, max_fix_attempts, issues)
            _log.info(
                "decompose: validation diagnostics attempt=%d/%d "
                "min_tasks_dynamic=%d max_tasks=%d actual_tasks=%d replan_reason=%s",
                fix_attempt,
                max_fix_attempts,
                min_tasks_dynamic,
                max_tasks,
                actual_tasks,
                replan_reason,
            )
            if archive_enabled:
                _archive_response_write(
                    workdir,
                    f"manager_validate_issues_{fix_attempt}",
                    f"Plan Validation Issues (attempt {fix_attempt}/{max_fix_attempts})",
                    "\n".join(f"- {x}" for x in issues),
                )

            if fix_attempt >= max_fix_attempts:
                _log.warning("decompose: max fix attempts reached, using plan as-is")
                if chat_id is not None and bot is not None:
                    await self._send_adapter_message(
                        bot,
                        context,
                        chat_id=chat_id,
                        text=f"⚠️ План содержит замечания, но исчерпаны попытки корректировки: {issues_short}",
                    )
                break

            if chat_id is not None and bot is not None:
                await self._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=(
                        f"⚠️ Проблемы в плане: {issues_short}\n"
                        f"🔄 Отправляю CLI на корректировку ({fix_attempt}/{max_fix_attempts})..."
                    ),
                )

            fixed_plan = await self._fix_plan_via_cli(
                session,
                plan,
                issues,
                user_goal,
                timeout,
                workdir,
                chat_id=chat_id,
                min_tasks_dynamic=min_tasks_dynamic,
                stabilize_task_count=self._should_stabilize_task_count(issue_tags_history),
            )
            if fixed_plan:
                plan = fixed_plan
                _log.info("decompose: plan corrected (attempt %d), re-validating...", fix_attempt)
            else:
                _log.warning("decompose: CLI fix failed (attempt %d), using current plan", fix_attempt)
                if chat_id is not None and bot is not None:
                    await self._send_adapter_message(
                        bot,
                        context,
                        chat_id=chat_id,
                        text="⚠️ CLI не смог исправить план, используем текущий",
                    )
                break

        return plan

    def _try_parse_plan(self, raw_text: str, user_goal: str, max_tasks: int) -> Optional[ProjectPlan]:
        """Try to parse CLI output directly as JSON."""
        try:
            if not str(raw_text or "").strip():
                return None
            payload = loads_safe(raw_text, strict_first=False)
            if not isinstance(payload, dict):
                return None
            validate_payload(payload, PLAN_PAYLOAD_SCHEMA, context="try_parse_plan")
            return self._payload_to_plan(payload, user_goal, max_tasks)
        except Exception as e:
            _log.warning("try_parse_plan: parse failed: %s", e)
            return None

    async def _normalize_plan(
        self, cli_output: str, user_goal: str, max_tasks: int, strict: bool = False,
        workdir: str = "",
    ) -> Optional[ProjectPlan]:
        """Phase 2: Agent extracts structured plan from free-form CLI text."""
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        system = self._manager_prompt(workdir, "decompose_normalize_system").format(max_tasks=max_tasks)
        system = self._with_invariant_policy(workdir, system)
        if strict:
            system += "\n\nПРЕДЫДУЩАЯ ПОПЫТКА НЕ РАСПАРСИЛАСЬ. Верни ТОЛЬКО валидный JSON, ничего больше."
        user_msg = (
            f"Цель проекта: {user_goal}\n\n"
            f"Ответ CLI (анализ проекта и план):\n{cli_output}"
        )
        raw = await chat_completion(self._config, system, user_msg, response_format={"type": "json_object"})
        if archive_enabled and workdir:
            suffix = "_strict" if strict else ""
            _archive_response_write(
                workdir,
                f"agent_normalize{suffix}_response",
                f"Agent Normalize Response{' (strict)' if strict else ''}",
                raw or "(empty)",
            )
        if not raw:
            return None
        try:
            payload = loads_safe(raw, strict_first=False)
            if not isinstance(payload, dict):
                return None
            validate_payload(payload, PLAN_PAYLOAD_SCHEMA, context="normalize_plan")
            plan = self._payload_to_plan(payload, user_goal, max_tasks)
            if plan and archive_enabled and workdir:
                _archive_response_write(
                    workdir,
                    "manager_decompose_result",
                    "Decompose Result (normalized)",
                    json.dumps(asdict(plan), ensure_ascii=False, indent=2),
                )
            return plan
        except Exception as e:
            _log.warning("normalize_plan: JSON parse error: %s", e)
            return None

    async def _fix_plan_via_cli(
        self, session: Session, plan: ProjectPlan, issues: List[str],
        user_goal: str, timeout: int, workdir: str,
        *,
        chat_id: Optional[int] = None,
        min_tasks_dynamic: int = 0,
        stabilize_task_count: bool = False,
    ) -> Optional[ProjectPlan]:
        """Send the plan back to CLI for correction based on validation issues."""
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        max_tasks = int(self._config.defaults.manager_max_tasks)
        min_tasks = max(1, int(min_tasks_dynamic or 0))
        relax_no_new_tasks = False
        for raw_issue in (issues or []):
            code = self._issue_code(raw_issue)
            if code in _PLAN_FIX_RELAX_CODES:
                relax_no_new_tasks = True
                break
        analysis_payload = self._plan_service.serialize_analysis(plan.analysis)
        atomicity_hotspots = self._collect_atomicity_hotspots(plan)
        payload = {
            "issues": [str(x) for x in (issues or []) if x],
            "project_analysis": analysis_payload,
            "checklist_table": list(analysis_payload.get("checklist_table") or []),
            "atomicity_hotspots": list(atomicity_hotspots),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "acceptance_criteria": list(t.acceptance_criteria or []),
                    "covers_requirements": list(t.covers_requirements or []),
                    "depends_on": list(t.depends_on or []),
                }
                for t in (plan.tasks or [])
            ],
            "rules": {
                "max_tasks": max_tasks,
                "min_tasks": min_tasks,
                "max_requirements_per_task": int(ATOMICITY_MAX_REQS_PER_TASK),
                "preserve_ids": True,
                "no_new_tasks_by_default": not relax_no_new_tasks,
                "prevent_count_oscillation": bool(stabilize_task_count),
            },
        }
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        instr = self._manager_prompt(workdir, "plan_fix_minimal_instruction").format(
            payload_json=payload_json,
            max_tasks=max_tasks,
            max_requirements_per_task=int(ATOMICITY_MAX_REQS_PER_TASK),
        )
        instr = self._with_invariant_policy(workdir, instr)
        if archive_enabled:
            _archive_response_write(workdir, "manager_fix_payload_minimal", "Plan Fix Payload (minimal)", payload_json)
            _archive_response_write(workdir, "manager_fix_prompt", "Plan Fix Prompt → CLI (fresh=true)", instr)

        try:
            cli_used, cli_text = await run_prompt_routed_meta(
                session,
                self._config,
                "planning",
                instr,
                response_format=CLIResponseFormat.JSON_OBJECT,
                timeout_sec=timeout,
                force_fresh=True,
                chat_id=chat_id,
            )
            _log.info("fix_plan: used cli=%s", cli_used)
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                _log.exception("fix_plan: interrupt after timeout failed")
            _log.warning("fix_plan: CLI timeout")
            return None
        except Exception as exc:
            _log.warning("fix_plan: CLI error: %s", exc)
            return None

        cli_text = strip_ansi(cli_text or "")
        if archive_enabled:
            _archive_response_write(workdir, "cli_fix_response", "CLI Fix Response", cli_text)

        # Try to parse corrected plan
        fixed = self._try_parse_plan(cli_text, user_goal, max_tasks)
        if fixed:
            fixed = self._merge_plan_analysis_context(plan, fixed)
            if archive_enabled:
                _archive_response_write(
                    workdir,
                    "manager_fix_result",
                    "Fixed Plan (direct parse)",
                    json.dumps(asdict(fixed), ensure_ascii=False, indent=2),
                )
            if not self._plan_equivalent(plan, fixed):
                return fixed
            _log.warning("fix_plan: CLI returned equivalent plan (no changes), falling back to LLM fixer")
            llm_fixed = await self._fix_plan_via_llm(
                plan,
                issues,
                user_goal,
                workdir=workdir,
                min_tasks_dynamic=min_tasks,
                stabilize_task_count=stabilize_task_count,
            )
            return llm_fixed or fixed

        # If CLI output is not parseable, use an LLM to apply fixes deterministically.
        llm_fixed = await self._fix_plan_via_llm(
            plan,
            issues,
            user_goal,
            workdir=workdir,
            min_tasks_dynamic=min_tasks,
            stabilize_task_count=stabilize_task_count,
        )
        if llm_fixed and archive_enabled:
            _archive_response_write(
                workdir,
                "manager_fix_result",
                "Fixed Plan (LLM fix fallback)",
                json.dumps(asdict(llm_fixed), ensure_ascii=False, indent=2),
            )
        return self._merge_plan_analysis_context(plan, llm_fixed)

    @staticmethod
    def _plan_equivalent(a: ProjectPlan, b: ProjectPlan) -> bool:
        """Compare two plans ignoring runtime fields (status/attempts/etc)."""
        def _sig(p: ProjectPlan) -> list[dict]:
            return [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "acceptance_criteria": list(t.acceptance_criteria or []),
                    "covers_requirements": list(t.covers_requirements or []),
                    "depends_on": list(t.depends_on or []),
                }
                for t in (p.tasks or [])
            ]
        return _sig(a) == _sig(b)

    @staticmethod
    def _merge_plan_analysis_context(source: ProjectPlan, target: Optional[ProjectPlan]) -> Optional[ProjectPlan]:
        """Keep analysis context stable across plan-fix even if fixer omits it partially."""
        return PlanManagementService.merge_analysis_context(source, target)

    async def _fix_plan_via_llm(
        self,
        plan: ProjectPlan,
        issues: List[str],
        user_goal: str,
        *,
        workdir: str = "",
        min_tasks_dynamic: int = 0,
        stabilize_task_count: bool = False,
    ) -> Optional[ProjectPlan]:
        """LLM-based plan fixer: apply issues to the given plan and return a corrected plan."""
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        max_tasks = int(self._config.defaults.manager_max_tasks)
        min_tasks = max(1, int(min_tasks_dynamic or 0))
        relax_no_new_tasks = False
        for raw_issue in (issues or []):
            code = self._issue_code(raw_issue)
            if code in _PLAN_FIX_RELAX_CODES:
                relax_no_new_tasks = True
                break
        # Keep the LLM-fix payload minimal as well to avoid duplicated large contexts.
        analysis_payload = self._plan_service.serialize_analysis(plan.analysis)
        atomicity_hotspots = self._collect_atomicity_hotspots(plan)
        payload = {
            "issues": [str(x) for x in (issues or []) if x],
            "project_analysis": analysis_payload,
            "checklist_table": list(analysis_payload.get("checklist_table") or []),
            "atomicity_hotspots": list(atomicity_hotspots),
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "acceptance_criteria": list(t.acceptance_criteria or []),
                    "covers_requirements": list(t.covers_requirements or []),
                    "depends_on": list(t.depends_on or []),
                }
                for t in (plan.tasks or [])
            ],
            "rules": {
                "max_tasks": max_tasks,
                "min_tasks": min_tasks,
                "max_requirements_per_task": int(ATOMICITY_MAX_REQS_PER_TASK),
                "preserve_ids": True,
                "no_new_tasks_by_default": not relax_no_new_tasks,
                "prevent_count_oscillation": bool(stabilize_task_count),
            },
        }
        user_msg = json.dumps(payload, ensure_ascii=False, indent=2)
        system = self._manager_prompt(workdir, "plan_fix_system").format(
            max_tasks=max_tasks,
            max_requirements_per_task=int(ATOMICITY_MAX_REQS_PER_TASK),
        )
        system = self._with_invariant_policy(workdir, system)
        raw = await chat_completion(self._config, system, user_msg, response_format={"type": "json_object"})
        if archive_enabled and workdir:
            _archive_response_write(workdir, "agent_fix_plan_response", "Agent Plan Fix Response", raw or "(empty)")
        if not raw:
            return None
        try:
            payload = loads_safe(raw, strict_first=False)
            if not isinstance(payload, dict):
                return None
            validate_payload(payload, PLAN_PAYLOAD_SCHEMA, context="fix_plan_via_llm")
            fixed = self._payload_to_plan(payload, user_goal, max_tasks)
            return self._merge_plan_analysis_context(plan, fixed)
        except Exception as e:
            _log.warning("fix_plan_via_llm: JSON parse error: %s", e)
            return None

    def _payload_to_plan(self, payload: dict, user_goal: str, max_tasks: int) -> Optional[ProjectPlan]:
        """Convert a parsed JSON dict to ProjectPlan."""
        return self._plan_service.plan_from_payload(
            payload,
            user_goal=user_goal,
            max_tasks=max_tasks,
        )

    # -----------------------------------------------------------------------
    # Plan validation
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate_plan_structure(plan: ProjectPlan) -> List[str]:
        """Check plan for structural issues. Returns list of problems (empty = OK)."""
        issues: List[str] = []
        task_ids = set()
        id_list = [str(t.id or "") for t in plan.tasks]
        tasks_count = len(plan.tasks)
        frozen_min_tasks_dynamic = int(getattr(plan, "_manager_min_tasks_dynamic", 0) or 0)
        if frozen_min_tasks_dynamic > 0:
            min_tasks_dynamic = frozen_min_tasks_dynamic
        else:
            min_tasks_dynamic = _min_tasks_dynamic(plan.analysis)
        max_tasks_limit = int(getattr(plan, "_manager_max_tasks_limit", 0) or 0)
        if max_tasks_limit > 0:
            min_tasks_dynamic = min(min_tasks_dynamic, max_tasks_limit)
        if tasks_count < min_tasks_dynamic:
            issues.append(
                "TASK_COUNT_BELOW_MIN: "
                f"tasks_count={tasks_count}, min_tasks_dynamic={min_tasks_dynamic}"
            )

        req_labels: Dict[str, str] = {}
        if plan.analysis and isinstance(plan.analysis.requirements, list):
            for raw_req in plan.analysis.requirements:
                req_label = str(raw_req).strip()
                if not req_label:
                    continue
                req_id = _normalize_requirement_ref(req_label)
                if req_id and req_id not in req_labels:
                    req_labels[req_id] = req_label

        req_ids = list(req_labels.keys())
        coverage_map: Dict[str, List[str]] = {rid: [] for rid in req_ids}

        for t in plan.tasks:
            tid = str(t.id or "")
            if tid != tid.strip():
                issues.append(f"ID задачи содержит пробелы по краям: '{t.id}'")
            if not tid.strip():
                issues.append("Обнаружена задача с пустым ID")
                # Continue checks but avoid using empty id in dependency diagnostics.
                continue

            # Duplicate IDs
            if t.id in task_ids:
                issues.append(f"Дублирующийся ID задачи: '{t.id}'")
            task_ids.add(t.id)

            # Empty fields
            if not t.title.strip():
                issues.append(f"Задача '{t.id}': пустой title")
            if not t.description.strip():
                issues.append(f"Задача '{t.id}': пустой description")
            if not t.acceptance_criteria or not any(str(x or "").strip() for x in t.acceptance_criteria):
                issues.append(f"Задача '{t.id}': нет acceptance_criteria")
            else:
                for idx, c in enumerate(t.acceptance_criteria, start=1):
                    if not str(c or "").strip():
                        issues.append(f"Задача '{t.id}': пустой acceptance_criteria[{idx}]")

            # Self-dependency
            if t.id in t.depends_on:
                issues.append(f"Задача '{t.id}' зависит от самой себя")

            # depends_on must be unique and non-empty strings
            deps_norm = [str(x or "").strip() for x in (t.depends_on or [])]
            if any(not d for d in deps_norm):
                issues.append(f"Задача '{t.id}': depends_on содержит пустые значения")
            if len(set(deps_norm)) != len(deps_norm):
                issues.append(f"Задача '{t.id}': depends_on содержит дубликаты")

            # Missing dependencies
            for dep in deps_norm:
                if dep and dep not in id_list:
                    issues.append(f"Задача '{t.id}' зависит от несуществующей '{dep}'")

            if req_ids:
                covers = [
                    _normalize_requirement_ref(x)
                    for x in (t.covers_requirements or [])
                    if _normalize_requirement_ref(x)
                ]
                if len(covers) > int(ATOMICITY_MAX_REQS_PER_TASK):
                    issues.append(
                        "TASK_TOO_BROAD_REQ_COVERAGE: "
                        f"task_id={t.id}, covers_requirements={len(covers)}, "
                        f"max_allowed={ATOMICITY_MAX_REQS_PER_TASK}"
                    )
                for rid in covers:
                    if rid not in coverage_map:
                        issues.append(f"Задача '{t.id}': covers_requirements содержит неизвестный '{rid}'")
                        continue
                    coverage_map[rid].append(t.id)

        # Circular dependencies (topological sort)
        if not issues:  # only check if no basic issues
            visited: Dict[str, int] = {}  # 0=in progress, 1=done

            def _has_cycle(tid: str) -> bool:
                if tid in visited:
                    return visited[tid] == 0
                visited[tid] = 0
                task_map = {t.id: t for t in plan.tasks}
                task = task_map.get(tid)
                if task:
                    for dep in task.depends_on:
                        if _has_cycle(dep):
                            return True
                visited[tid] = 1
                return False

            for t in plan.tasks:
                if t.id not in visited:
                    if _has_cycle(t.id):
                        issues.append("Обнаружена циклическая зависимость между задачами")
                        break

        if req_ids:
            has_any_links = any(coverage_map[rid] for rid in req_ids)
            if not has_any_links:
                issues.append("Трассируемость требований отсутствует: ни одна задача не ссылается на REQ-*")
            for rid in req_ids:
                if not coverage_map[rid]:
                    issues.append(f"Требование '{req_labels.get(rid, rid)}' не покрыто ни одной задачей")

        return issues

    async def _validate_plan_semantics(self, plan: ProjectPlan, workdir: str) -> List[str]:
        """LLM-based validation: check for logical contradictions between tasks."""
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        analysis_payload = self._plan_service.serialize_analysis(plan.analysis)
        validation_payload = {
            "project_goal": str(plan.project_goal or ""),
            "project_analysis": analysis_payload,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "acceptance_criteria": t.acceptance_criteria,
                    "covers_requirements": t.covers_requirements,
                    "depends_on": t.depends_on,
                }
                for t in plan.tasks
            ],
        }
        tasks_text = json.dumps(validation_payload, ensure_ascii=False, indent=2)
        if archive_enabled:
            _archive_response_write(workdir, "manager_validate_prompt", "Plan Validation Prompt", tasks_text)

        max_tasks = int(self._config.defaults.manager_max_tasks)
        base_system = self._manager_prompt(workdir, "plan_validation_system").format(max_tasks=max_tasks)
        systems = [
            base_system,
            (
                f"{base_system}\n\n"
                "ПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ. "
                "Верни ТОЛЬКО валидный JSON-объект формата "
                '{"valid": true|false, "issues": ["..."]} без markdown и пояснений.'
            ),
        ]
        for attempt, system in enumerate(systems, start=1):
            raw = await chat_completion(
                self._config, system, tasks_text, response_format={"type": "json_object"}
            )
            if archive_enabled:
                suffix = "" if attempt == 1 else " (retry)"
                _archive_response_write(
                    workdir,
                    f"agent_validate_response{'' if attempt == 1 else '_retry'}",
                    f"Plan Validation Response{suffix}",
                    raw or "(empty)",
                )
            if not raw:
                _log.warning(
                    "validate_plan_semantics: empty response from validator attempt=%d/%d",
                    attempt,
                    len(systems),
                )
                continue
            try:
                payload = parse_normalize_validate(raw, PLAN_VALIDATION_RESPONSE_SCHEMA)
                if not payload.get("valid", True):
                    return [str(x) for x in (payload.get("issues") or []) if str(x).strip()]
                return []
            except Exception as e:
                _log.warning(
                    "validate_plan_semantics: invalid JSON from validator attempt=%d/%d: %s",
                    attempt,
                    len(systems),
                    e,
                )
                continue
        return ["semantic_validator_parse_error"]

    async def _validate_plan(self, plan: ProjectPlan, workdir: str) -> List[str]:
        """Full plan validation: structural + semantic. Returns list of issues."""
        if not int(getattr(plan, "_manager_max_tasks_limit", 0) or 0):
            setattr(plan, "_manager_max_tasks_limit", int(self._config.defaults.manager_max_tasks))

        # 1. Structural checks (fast, deterministic)
        issues = self._validate_plan_structure(plan)
        if issues:
            return issues

        # 2. Semantic checks (LLM-based, only if structure is OK)
        semantic_issues = await self._validate_plan_semantics(plan, workdir)
        return semantic_issues

    # -----------------------------------------------------------------------
    # Plan notification
    # -----------------------------------------------------------------------

    async def _notify_plan(self, session: Session, plan: ProjectPlan, bot, context, dest: dict) -> None:
        chat_id = dest.get("chat_id")
        if chat_id is None:
            return
        plan_text = self._ui_service.format_plan_notification(plan)
        if len(plan_text) <= 3900:
            await self._send_adapter_message(bot, context, chat_id=chat_id, text=plan_text)
            return
        await self._send_adapter_message(
            bot,
            context,
            chat_id=chat_id,
            text="📎 План длинный, отправил его файлом.",
        )
        # TODO(M3): route large output via a transport-agnostic MessagingService.send_large_output when available.
        await bot.send_output(
            session,
            dest,
            plan_text,
            context,
            send_header=False,
            force_html=True,
            send_summary=False,
        )

    # -----------------------------------------------------------------------
    # Next ready task (with RETRIABLE_STATUSES normalization per TZ)
    # -----------------------------------------------------------------------

    def _next_ready_task(self, plan: ProjectPlan) -> Optional[DevTask]:
        """Select the next task ready for execution, normalizing stale statuses after restart."""
        for t in plan.tasks:
            t.set_status(self._normalize_status(t.status))
        tasks_by_id = {t.id: t for t in plan.tasks}
        has_retriable = False
        waiting_for_deps = False

        # --- Pass 1: normalize interrupted / stale statuses ---
        for t in plan.tasks:
            if t.status == "in_progress":
                # Interrupted during development.
                # Keep current stage so resume continues from development.
                # Attempt limit is enforced when starting a NEW cycle from pending/rejected.
                # If we are already in_progress, we should allow finishing this attempt.
                continue
            elif t.status == "in_review":
                # Interrupted during review.
                # Keep current stage so resume continues from review.
                # Attempt limit is enforced only before starting a new development cycle.
                continue
            elif t.status == "rejected":
                if t.attempt >= t.max_attempts:
                    t.set_status("failed")
                else:
                    t.set_status("pending")
            elif t.status == "failed" and t.attempt < t.max_attempts:
                # Previously failed task can be retried if attempts remain.
                t.set_status("pending")

        # --- Pass 2: re-evaluate blocked tasks (they may be unblocked now) ---
        for t in plan.tasks:
            if t.status == "blocked":
                deps = [tasks_by_id[dep_id] for dep_id in t.depends_on if dep_id in tasks_by_id]
                if not any(d.status == "failed" for d in deps):
                    t.set_status("pending")

        # --- Pass 3: find next ready task ---
        for t in plan.tasks:
            # Cascade blocking: if any dependency failed → block
            deps = [tasks_by_id[dep_id] for dep_id in t.depends_on if dep_id in tasks_by_id]
            if any(d.status == "failed" for d in deps):
                if t.status not in ("approved", "failed"):
                    t.set_status("blocked")
                continue

            if t.status in ("approved", "failed", "blocked"):
                continue

            if t.status in RETRIABLE_STATUSES:
                has_retriable = True

            # All deps must be approved
            if all(d.status == "approved" for d in deps):
                return t
            if deps and t.status in RETRIABLE_STATUSES:
                # Not ready yet, but this is normal: waiting for prerequisites.
                waiting_for_deps = True

        # No ready task found.
        # Warn only when there are retriable tasks but none is ready and none is simply "waiting".
        if has_retriable and not waiting_for_deps:
            _log.warning("_next_ready_task: no ready tasks; possible deadlock/cascade block")
        return None

    @staticmethod
    def _can_resume_failed(plan: ProjectPlan) -> bool:
        """True if a failed plan still has tasks that can be retried."""
        normalizer = ManagerOrchestrator._normalize_status
        for t in plan.tasks:
            status = normalizer(t.status)
            if status in ("pending", "rejected", "in_progress", "in_review"):
                return True
            # Blocked tasks may become unblocked after normalization
            if status == "blocked":
                return True
            # A failed task with attempts left can be retried
            if status == "failed" and t.attempt < t.max_attempts:
                return True
        return False

    def _is_plan_blocked(self, plan: ProjectPlan) -> bool:
        """True if all remaining non-approved tasks are blocked/failed (no more progress possible)."""
        normalizer = self._normalize_status
        for t in plan.tasks:
            if normalizer(t.status) in ("pending", "rejected", "in_progress", "in_review"):
                return False
        return True

    async def _pause_after_post_approval_error(
        self,
        session: Session,
        plan: ProjectPlan,
        task: DevTask,
        bot,
        context,
        dest: dict,
        *,
        stage: str,
        exc: Exception,
    ) -> ProjectPlan:
        plan.set_status("paused")
        plan.updated_at = _now_iso()
        plan = self._save_plan_with_run_artifacts(session, plan, phase="review")

        chat_id = dest.get("chat_id")
        if chat_id is None:
            return plan
        message_thread_id = dest.get("message_thread_id")

        reason = f"{type(exc).__name__}: {str(exc or '').strip()}"
        text = (
            f"⛔ Manager остановлен после принятия задачи {task.id}: {stage} упал.\n"
            f"Причина: {reason[:500]}\n"
            "План переведён в paused; после устранения причины его можно продолжить."
        )
        send_kwargs = {}
        if message_thread_id is not None:
            send_kwargs["message_thread_id"] = message_thread_id
        try:
            await self._send_runtime_message(
                session,
                bot,
                context,
                chat_id=chat_id,
                text=text,
                important=True,
                **send_kwargs,
            )
        except Exception:
            _log.exception(
                "_run_loop: failed to notify post-approval error task_id=%s stage=%s",
                task.id,
                stage,
            )
        return plan

    # -----------------------------------------------------------------------
    # Main execution loop
    # -----------------------------------------------------------------------

    async def _run_loop(self, session: Session, plan: ProjectPlan, bot, context, dest: dict) -> None:
        chat_id = dest.get("chat_id")
        max_iterations = int(self._config.defaults.manager_max_tasks) * int(self._config.defaults.manager_max_attempts)
        iteration = 0
        git_usable = self._git_is_usable(session.workdir)
        auto_commit_enabled = bool(getattr(self._config.defaults, "manager_auto_commit", False))
        baseline_committed = False
        # Filesystem snapshots are expensive, but are the most reliable option when:
        # - git isn't available, or
        # - auto-commit is disabled (we still want a deterministic audit of what the CLI changed).
        capture_fs_audit = (not git_usable) or (not auto_commit_enabled)

        while True:
            if plan.status in ("paused", "completed", "failed"):
                break

            iteration += 1
            if iteration > max_iterations:
                _log.warning("_run_loop: max iterations (%d) exceeded", max_iterations)
                plan.set_status("failed")
                plan = self._save_plan_with_run_artifacts(session, plan, phase="develop")
                await self._send_runtime_message(
                    session,
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⛔ Превышен лимит итераций ({max_iterations}). План остановлен.",
                    important=True,
                )
                break

            task = self._next_ready_task(plan)
            if not task:
                # No ready tasks: either all done or blocked.
                plan.current_task_id = None
                if all(t.status == "approved" for t in plan.tasks):
                    plan.set_status("completed")
                else:
                    # Mark remaining as blocked
                    for t in plan.tasks:
                        if t.status in ("pending", "rejected"):
                            t.set_status("blocked")
                    plan.set_status("failed")
                    await self._send_runtime_message(
                        session,
                        bot,
                        context,
                        chat_id=chat_id,
                        text="⛔ План остановлен: невозможно продолжить (задачи заблокированы).",
                        important=True,
                    )
                plan = self._save_plan_with_run_artifacts(session, plan, phase=manager_run_phase_for_plan(plan, fallback="complete"))
                break

            plan.current_task_id = task.id
            if not baseline_committed:
                await self._auto_commit_baseline_before_first_step(session, plan, bot, context, dest)
                baseline_committed = True
            skip_dev = task.status == "in_review"  # dev done, review was interrupted
            # Attempt is incremented only when starting a new development cycle.
            # For resumed stages (in_progress/in_review), keep the current attempt.
            if task.status in ("pending", "rejected"):
                task.attempt += 1
            task.started_at = task.started_at or _now_iso()

            if skip_dev:
                # Development already completed — go straight to review
                await self._send_runtime_message(
                    session,
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"🔍 Продолжаю ревью: {task.title} (попытка {task.attempt}/{task.max_attempts})",
                )
            else:
                task.set_status("in_progress")
                plan = self._save_plan_with_run_artifacts(session, plan, phase="develop")
                task_num, task_total = self._ui_service.task_progress(plan, task)
                await self._send_runtime_message(
                    session,
                    bot,
                    context,
                    chat_id=chat_id,
                    text=(
                        f"🔧 Разработка ({task_num}/{task_total}): {task.title} "
                        f"(попытка {task.attempt}/{task.max_attempts})"
                    ),
                )

                # === DEVELOPMENT ===
                before_snap = self._snapshot_workdir(session.workdir) if capture_fs_audit else None
                dev_ok, dev_report = await self._delegate_develop(session, plan, task, chat_id=chat_id)
                audit_text = ""
                has_changes = False
                if capture_fs_audit:
                    after_snap = self._snapshot_workdir(session.workdir)
                    diff = self._diff_snapshots(before_snap or {}, after_snap or {})
                    audit_text = self._format_change_audit(diff)
                    has_changes = bool(
                        (diff.get("created") or []) or (diff.get("modified") or []) or (diff.get("deleted") or [])
                    )
                elif git_usable:
                    audit_text, has_changes = await self._git_change_audit(session.workdir)
                task.manager_change_audit = audit_text
                task.manager_change_audit_has_changes = bool(has_changes)

                max_chars = int(getattr(self._config.defaults, "manager_dev_report_max_chars", 8000) or 8000)
                combined = (dev_report or "").rstrip()
                if audit_text and has_changes:
                    combined = (combined + "\n\n" + audit_text).strip()
                dev_report = self._ui_service.truncate_report(combined, max_chars)
                task.dev_report = dev_report
                plan = self._save_plan_with_run_artifacts(session, plan, phase="develop")
                if not dev_ok:
                    if task.attempt >= task.max_attempts:
                        task.set_status("failed")
                        task.completed_at = _now_iso()
                        plan = self._save_plan_with_run_artifacts(session, plan, phase="develop")
                        await self._send_runtime_message(
                            session,
                            bot,
                            context,
                            chat_id=chat_id,
                            text=f"❌ Провал: {task.title} — исчерпаны попытки ({task.max_attempts}). {dev_report[:150]}",
                            important=True,
                        )
                        # Check if plan is now blocked
                        if self._is_plan_blocked(plan):
                            plan.set_status("failed")
                            plan = self._save_plan_with_run_artifacts(session, plan, phase="develop")
                            await self._send_runtime_message(
                                session,
                                bot,
                                context,
                                chat_id=chat_id,
                                text="⛔ План остановлен: критическая задача провалена.",
                                important=True,
                            )
                            break
                    else:
                        task.set_status("pending")  # will be retried on next iteration
                        plan = self._save_plan_with_run_artifacts(session, plan, phase="develop")
                        await self._send_runtime_message(
                            session,
                            bot,
                            context,
                            chat_id=chat_id,
                            text=(
                                f"⚠️ Ошибка: {task.title} (попытка {task.attempt}/{task.max_attempts}): "
                                f"{dev_report[:150]}\n🔄 Повтор..."
                            ),
                        )
                    continue

            # === REVIEW ===
            task.set_status("in_review")
            plan = self._save_plan_with_run_artifacts(session, plan, phase="review")
            await self._send_runtime_message(
                session,
                bot,
                context,
                chat_id=chat_id,
                text=f"🔍 Ревью: {task.title}",
            )

            review = await self._delegate_review(session, plan, task, bot, context, dest)
            task.review_verdict = "approved" if review.approved else "rejected"
            task.review_comments = review.comments
            plan = self._save_plan_with_run_artifacts(session, plan, phase="review")

            # === ARBITER DECISION ===
            verdict, reasons = await self._make_decision(task, review, workdir=session.workdir)
            if verdict == "approved":
                task.set_status("approved")
                task.completed_at = _now_iso()
                plan.current_task_id = None
                plan = self._save_plan_with_run_artifacts(session, plan, phase="review")
                await self._send_runtime_message_best_effort(
                    session,
                    bot,
                    context,
                    stage="approved notification",
                    task_id=task.id,
                    chat_id=chat_id,
                    text=f"✅ Принято: {task.title}",
                )
                # Auto-commit approved changes
                committed = False
                try:
                    committed = await self._auto_commit(session, task, plan, bot, context, dest)
                except Exception as exc:
                    _log.exception("_run_loop: auto_commit failed task_id=%s", task.id)
                    plan = await self._pause_after_post_approval_error(
                        session,
                        plan,
                        task,
                        bot,
                        context,
                        dest,
                        stage="auto_commit",
                        exc=exc,
                    )
                    break
                # Reconcile plan: CLI may have done more than asked
                if committed:
                    try:
                        await self._reconcile_plan_after_commit(session, task, plan, bot, context, dest)
                    except Exception as exc:
                        _log.exception("_run_loop: reconcile after commit failed task_id=%s", task.id)
                        plan = await self._pause_after_post_approval_error(
                            session,
                            plan,
                            task,
                            bot,
                            context,
                            dest,
                            stage="reconcile after commit",
                            exc=exc,
                        )
                        break
                else:
                    # No git commit possible (or skipped). If we observed file changes, still reconcile.
                    if bool(task.manager_change_audit_has_changes):
                        try:
                            await self._reconcile_plan_after_change_audit(session, task, plan, bot, context, dest)
                        except Exception as exc:
                            _log.exception("_run_loop: reconcile after change audit failed task_id=%s", task.id)
                            plan = await self._pause_after_post_approval_error(
                                session,
                                plan,
                                task,
                                bot,
                                context,
                                dest,
                                stage="reconcile after change audit",
                                exc=exc,
                            )
                            break
                continue

            # rejected
            merged_rejection = self._ui_service.merge_rejection_feedback(review.comments, reasons)
            task.review_comments = merged_rejection
            task.rejection_history.append({
                "attempt": task.attempt,
                "comments": merged_rejection,
                "timestamp": _now_iso(),
            })
            if task.attempt >= task.max_attempts:
                task.set_status("failed")
                task.completed_at = _now_iso()
                plan = self._save_plan_with_run_artifacts(session, plan, phase="review")
                await self._send_runtime_message(
                    session,
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"❌ Провал: {task.title} — исчерпаны попытки ({task.max_attempts})",
                    important=True,
                )
                # Check if plan is now blocked
                if self._is_plan_blocked(plan):
                    plan.set_status("failed")
                    plan = self._save_plan_with_run_artifacts(session, plan, phase="review")
                    await self._send_runtime_message(
                        session,
                        bot,
                        context,
                        chat_id=chat_id,
                        text="⛔ План остановлен: критическая задача провалена.",
                        important=True,
                    )
                    break
            else:
                task.set_status("pending")  # will be retried
                plan = self._save_plan_with_run_artifacts(session, plan, phase="review")
                reasons_txt = ", ".join(reasons) if reasons else (review.comments or "см. замечания")
                await self._send_runtime_message(
                    session,
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"🔄 Доработка: {task.title} (попытка {task.attempt + 1})\nПричины: {reasons_txt}",
                )

    # -----------------------------------------------------------------------
    # Delegate development to CLI
    # -----------------------------------------------------------------------

    async def _classify_dev_task_work_type(
        self,
        plan: ProjectPlan,
        task: DevTask,
        *,
        workdir: str,
    ) -> Optional[str]:
        """
        Classify a Manager DevTask into a work type to route CLI selection.

        Uses OpenAI (defaults.openai_model). If OpenAI is not configured or the classification
        is not confident/valid for a dev task, returns None (caller should fall back to "default").
        """
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        max_ctx = 4000

        # If OpenAI isn't configured, we can't reliably classify.
        if not (self._config.defaults.openai_api_key and self._config.defaults.openai_model):
            _log.info("work_type: OpenAI not configured, using default routing")
            return None

        ctx = ""
        if plan.analysis and plan.analysis.current_state:
            ctx = plan.analysis.current_state
        ctx = (ctx or "")[:max_ctx]

        user_msg = (
            "Классифицируй тип работы для задачи.\n\n"
            f"title: {task.title}\n"
            f"description: {task.description}\n"
            f"context: {ctx}\n"
        )
        try:
            raw = await chat_completion(
                self._config,
                self._manager_prompt(workdir, "work_type_classifier_system"),
                user_msg,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            _log.info("work_type: classifier call failed: %s", e)
            return None

        wt, conf, reason = self._execution_service.parse_work_type_json(
            raw,
            allowed_work_types=WORK_TYPES,
            logger=_log,
        )
        if archive_enabled:
            _archive_response_write(
                workdir,
                f"manager_work_type_{task.id}",
                f"Work Type Classification [{task.id}]",
                f"raw:\n{raw}\n\nparsed:\nwork_type={wt}\nconfidence={conf}\nreason={reason}\n",
            )
        if not wt:
            return None
        if wt not in DEV_TASK_WORK_TYPES:
            # Only DevTask-related types are allowed to affect routing.
            return None
        if conf < 0.70:
            return None
        return wt

    async def _delegate_develop(
        self,
        session: Session,
        plan: ProjectPlan,
        task: DevTask,
        *,
        chat_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        timeout = int(self._config.defaults.manager_dev_timeout_sec)
        max_chars = int(self._config.defaults.manager_dev_report_max_chars)
        archive_enabled = bool(self._config.defaults.manager_response_archive)

        # Build context
        ctx = ""
        if plan.analysis and plan.analysis.current_state:
            ctx = plan.analysis.current_state

        already_done = ""
        if plan.analysis and plan.analysis.already_done:
            already_done = ", ".join(plan.analysis.already_done)

        completed_tasks = [t for t in plan.tasks if t.status == "approved"]
        completed_summary = ", ".join(t.title for t in completed_tasks) if completed_tasks else "(нет)"

        # Partial work block: what was already done by a previous task's CLI
        partial_work_block = ""
        if task.partial_work_note:
            partial_work_block = (
                f"### ⚠️ Часть работы уже выполнена (НЕ переделывай!):\n"
                f"{task.partial_work_note}\n\n"
                f"Учти это при выполнении задачи. Проверь перечисленные файлы/функции — "
                f"они уже существуют. Сконцентрируйся только на ОСТАВШЕЙСЯ работе."
            )

        is_rework = task.attempt > 1 and task.review_comments

        if is_rework:
            # Rework: task was already implemented, focus on fixing review issues
            rejection_history_block = ""
            if len(task.rejection_history) > 1:
                history_lines = []
                for entry in task.rejection_history[:-1]:
                    att = entry.get("attempt", "?")
                    comments = entry.get("comments", "")
                    if comments:
                        history_lines.append(f"- Попытка {att}: {comments}")
                if history_lines:
                    rejection_history_block = (
                        "### История предыдущих замечаний:\n"
                        + "\n".join(history_lines)
                    )

            instr = self._execution_service.build_rework_instruction(
                self._manager_prompt(session.workdir, "dev_rework_instruction_template"),
                task_title=task.title,
                task_description=task.description,
                dev_report=task.dev_report or "(отчёт отсутствует)",
                review_comments=task.review_comments,
                rejection_history_block=rejection_history_block,
                task_acceptance=self._ui_service.task_acceptance(task),
                partial_work_block=partial_work_block,
                project_context=ctx or "(контекст не задан)",
                already_done=already_done or "(нет данных)",
                completed_tasks_summary=completed_summary,
                attempt=task.attempt,
                max_attempts=task.max_attempts,
            )
        else:
            # First attempt: full task description
            instr = self._execution_service.build_dev_instruction(
                self._manager_prompt(session.workdir, "dev_instruction_template"),
                task_title=task.title,
                task_description=task.description,
                task_acceptance=self._ui_service.task_acceptance(task),
                rejection_block="",
                partial_work_block=partial_work_block,
                project_context=ctx or "(контекст не задан)",
                already_done=already_done or "(нет данных)",
                completed_tasks_summary=completed_summary,
            )
        instr = self._with_invariant_policy(session.workdir, instr)
        instr = self._apply_manager_prompt_learning(session.workdir, instr)
        if archive_enabled:
            _archive_response_write(
                session.workdir,
                f"manager_dev_prompt_{task.id}",
                f"Dev Prompt → CLI [{task.id}] (attempt {task.attempt})",
                instr,
            )
        try:
            work_type = await self._classify_dev_task_work_type(plan, task, workdir=session.workdir)
            routed = work_type or "default"
            cli_used, out = await run_prompt_routed_meta(
                session,
                self._config,
                routed,
                instr,
                response_format=CLIResponseFormat.JSON_OBJECT,
                timeout_sec=timeout,
                chat_id=chat_id,
            )
            out = strip_ansi(out or "")
            if archive_enabled:
                _archive_response_write(
                    session.workdir,
                    f"cli_dev_response_{task.id}",
                    f"CLI Dev Response [{task.id}] (attempt {task.attempt})\n\n"
                    f"(work_type={routed}, cli={cli_used})",
                    out,
                )
            out = self._ui_service.truncate_report(out, max_chars)
            return True, out
        except RoutedCallError as e:
            return False, f"ERROR: {e}"
        except Exception as e:
            return False, f"ERROR: {e}"

    # -----------------------------------------------------------------------
    # Delegate review to Agent (Executor with reviewer profile)
    # -----------------------------------------------------------------------

    async def _delegate_review(self, session: Session, plan: ProjectPlan, task: DevTask, bot, context, dest: dict) -> ReviewResult:
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        tool_registry = get_tool_registry(self._config)
        profile = build_reviewer_profile(self._config, tool_registry)
        dev_report = task.dev_report or ""

        # Reviewer-only context: either last git commit (filtered/capped) or no-git filesystem audit.
        if self._git_is_usable(session.workdir):
            code, last_commit = await self._run_git(
                session.workdir, ["log", "-1", "--name-status", "--format=%s (%h)"]
            )
            if code == 0 and (last_commit or "").strip():
                last_commit_info = format_git_log_name_status(last_commit, max_lines=80) or "(нет данных)"
            else:
                last_commit_info = "(нет данных)"
        else:
            audit = str(task.manager_change_audit or "").strip()
            last_commit_info = audit if audit else "(нет данных)"

        instr = self._execution_service.build_review_instruction(
            self._manager_prompt(session.workdir, "review_instruction_template"),
            task_title=task.title,
            task_description=task.description,
            task_acceptance=self._ui_service.task_acceptance(task),
            dev_report=dev_report,
            last_commit_info=last_commit_info,
        )
        instr = self._with_invariant_policy(session.workdir, instr)
        if archive_enabled:
            _archive_response_write(
                session.workdir,
                f"manager_review_prompt_{task.id}",
                f"Review Prompt → Agent [{task.id}]",
                instr,
            )
        req = self._execution_service.build_review_executor_request(
            task=task,
            instruction=instr,
            workdir=session.workdir,
            allowed_tools=profile.allowed_tools,
            deadline_ms=profile.timeout_ms,
        )
        try:
            resp = await self._executor.run(session, req, bot, context, dest, profile)
            text = self._execution_service.extract_executor_primary_text(resp)
        except Exception as e:
            return ReviewResult(approved=False, summary="Ошибка ревью", comments=str(e))

        if archive_enabled:
            _archive_response_write(
                session.workdir,
                f"agent_review_response_{task.id}",
                f"Agent Review Response [{task.id}]",
                text,
            )

        # Two-phase review result parsing (same as decompose)
        # 1. Try direct parse
        review = self._execution_service.parse_review_result(
            text,
            logger=_log,
            allow_action_payload_fallback=False,
        )
        if review:
            if archive_enabled:
                _archive_response_write(
                    session.workdir,
                    f"manager_review_result_{task.id}",
                    f"Review Result [{task.id}] (direct parse)",
                    json.dumps(asdict(review), ensure_ascii=False, indent=2),
                )
            return review

        # 2. Agent normalization
        normalized = await chat_completion(
            self._config,
            self._manager_prompt(session.workdir, "review_normalize_system"),
            text,
            response_format={"type": "json_object"},
            normalize_error_handler=lambda content, exc: self._recover_review_normalize_error(
                source_text=text,
                normalized_content=content,
                exc=exc,
            ),
        )
        if archive_enabled:
            _archive_response_write(
                session.workdir,
                f"agent_review_normalize_{task.id}",
                f"Agent Review Normalize Response [{task.id}]",
                normalized or "(empty)",
            )
        review = self._try_parse_review(normalized or "")
        if review:
            if archive_enabled:
                _archive_response_write(
                    session.workdir,
                    f"manager_review_result_{task.id}",
                    f"Review Result [{task.id}] (normalized)",
                    json.dumps(asdict(review), ensure_ascii=False, indent=2),
                )
            return review

        # 3. Fallback
        return ReviewResult(
            approved=False,
            summary="Не удалось определить вердикт",
            comments="Не удалось распарсить ответ ревьюера, требуется доработка.",
        )

    def _recover_review_normalize_error(self, *, source_text: str, normalized_content: str, exc: Exception) -> str | None:
        recovered = self._try_parse_review(normalized_content)
        if recovered:
            _log.warning("review normalize fallback used recovered review result: %s", exc)
            return json.dumps(asdict(recovered), ensure_ascii=False)

        parts = [
            "Не удалось нормализовать ответ ревьюера в JSON.",
            f"Ошибка normalizer LLM: {exc}",
        ]
        source_preview = str(source_text or "").strip()
        if source_preview:
            parts.append(f"Исходный ответ ревьюера:\n{source_preview[:3000]}")
        normalized_preview = str(normalized_content or "").strip()
        if normalized_preview and normalized_preview != source_preview:
            parts.append(f"Ответ normalizer LLM:\n{normalized_preview[:2000]}")

        _log.warning("review normalize fallback used synthetic rejected result: %s", exc)
        return json.dumps(
            {
                "approved": False,
                "summary": "Не удалось нормализовать ответ ревьюера",
                "comments": "\n\n".join(parts),
                "tests_passed": None,
                "files_reviewed": [],
                "not_done_assessment": [],
            },
            ensure_ascii=False,
        )

    def _try_parse_review(self, text: str) -> Optional[ReviewResult]:
        """Try to parse review text as JSON ReviewResult."""
        return self._execution_service.parse_review_result(text, logger=_log)

    # -----------------------------------------------------------------------
    # Arbiter decision (always called; decides by acceptance criteria)
    # -----------------------------------------------------------------------

    async def _make_decision(self, task: DevTask, review: ReviewResult, workdir: str = "") -> Tuple[str, List[str]]:
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        user_msg = (
            f"### Задача: {task.title}\n\n"
            f"### Описание:\n{task.description}\n\n"
            f"### Критерии приёмки:\n{self._ui_service.task_acceptance(task)}\n\n"
            f"### Отчёт разработчика:\n{task.dev_report or '(пусто)'}\n\n"
            f"### Вердикт ревьюера:\n{json.dumps(asdict(review), ensure_ascii=False)}"
        )
        if archive_enabled and workdir:
            _archive_response_write(
                workdir,
                f"manager_decision_prompt_{task.id}",
                f"Decision Prompt → Arbiter [{task.id}]",
                user_msg,
            )
        raw = await chat_completion(
            self._config,
            self._manager_prompt(workdir, "decision_system"),
            user_msg,
            response_format={"type": "json_object"},
        )
        if archive_enabled and workdir:
            _archive_response_write(
                workdir,
                f"agent_decision_response_{task.id}",
                f"Arbiter Decision Response [{task.id}]",
                raw or "(empty)",
            )
        verdict = "approved" if review.approved else "rejected"
        reasons: List[str] = []
        if raw:
            try:
                payload = loads_safe(raw, strict_first=False)
                if isinstance(payload, dict):
                    verdict = str(payload.get("verdict") or verdict)
                    rs = payload.get("reasons") or []
                    if isinstance(rs, list):
                        reasons = [str(x) for x in rs if x]
            except Exception as e:
                _log.warning("decide_on_review: invalid JSON from arbiter: %s", e)
                reasons = reasons or ["arbiter_parse_error"]
        if verdict not in ("approved", "rejected"):
            verdict = "approved" if review.approved else "rejected"
        return verdict, reasons

    # -----------------------------------------------------------------------
    # Final TZ audit and gap closing
    # -----------------------------------------------------------------------

    def _parse_final_spec_audit_json(self, raw: str) -> Dict[str, Any]:
        try:
            payload = parse_normalize_validate(
                raw,
                FINAL_SPEC_AUDIT_SCHEMA,
                strict_json_document=True,
            )
        except JSONSchemaValidationError as exc:
            if "lint" not in str(exc):
                raise
            parsed = loads_safe(raw, strict_first=False)
            if not isinstance(parsed, dict) or "lint" in parsed:
                raise
            parsed["lint"] = []
            _log.warning(
                "_parse_final_spec_audit_json: missing lint in audit payload; injected empty lint list"
            )
            payload = normalize_payload(parsed, FINAL_SPEC_AUDIT_SCHEMA)
        status = str(payload.get("status") or "").strip().upper()

        def _str_list(name: str) -> List[str]:
            vals = payload.get(name) or []
            if not isinstance(vals, list):
                return []
            return [str(x).strip() for x in vals if str(x).strip()]

        fixes_raw = payload.get("fixes_applied") or []
        fixes: List[Dict[str, str]] = []
        if isinstance(fixes_raw, list):
            for item in fixes_raw:
                if not isinstance(item, dict):
                    continue
                fixes.append(
                    {
                        "gap": str(item.get("gap") or "").strip(),
                        "changes": str(item.get("changes") or "").strip(),
                        "evidence": str(item.get("evidence") or "").strip(),
                    }
                )

        tests = payload.get("tests") if isinstance(payload.get("tests"), list) else []
        lint = payload.get("lint") if isinstance(payload.get("lint"), list) else []
        requirement_matrix: List[Dict[str, Any]] = []
        matrix_raw = payload.get("requirement_matrix") or []
        if isinstance(matrix_raw, list):
            for item in matrix_raw:
                if not isinstance(item, dict):
                    continue
                requirement_matrix.append(
                    {
                        "req_id": str(item.get("req_id") or "").strip(),
                        "status": str(item.get("status") or "").strip().upper(),
                        "tasks": [str(x).strip() for x in (item.get("tasks") or []) if str(x).strip()],
                        "evidence": [str(x).strip() for x in (item.get("evidence") or []) if str(x).strip()],
                        "gap": str(item.get("gap") or "").strip(),
                    }
                )
        patch_candidate = payload.get("manager_prompt_patch_candidate")
        if not isinstance(patch_candidate, dict):
            patch_candidate = {}
        return {
            "status": status,
            "summary": str(payload.get("summary") or "").strip(),
            "gaps_found": _str_list("gaps_found"),
            "fixes_applied": fixes,
            "remaining_gaps": _str_list("remaining_gaps"),
            "tests": tests,
            "lint": lint,
            "requirement_matrix": requirement_matrix,
            "manager_prompt_patch_candidate": patch_candidate,
            "raw": payload,
        }

    @staticmethod
    def _build_retryable_final_spec_audit_failure(
        *, summary: str, raw_text: str = ""
    ) -> Dict[str, Any]:
        preview = " ".join(strip_ansi(raw_text or "").split())
        if len(preview) > 280:
            preview = preview[:277].rstrip() + "..."
        remaining_gaps = [summary]
        if preview:
            remaining_gaps.append(
                f"Вместо итогового audit JSON был получен ответ: {preview}"
            )
        return {
            "status": "FAIL",
            "summary": summary,
            "gaps_found": [],
            "fixes_applied": [],
            "remaining_gaps": remaining_gaps,
            "tests": [],
            "lint": [],
            "requirement_matrix": [],
        }

    async def _build_manager_prompt_patch_from_final_audit(
        self,
        *,
        original_goal: str,
        audit_result: Dict[str, Any],
        workdir: str,
    ) -> Optional[Dict[str, Any]]:
        user_payload = json.dumps(
            {
                "original_goal": str(original_goal or ""),
                "final_audit_result": audit_result,
            },
            ensure_ascii=False,
            indent=2,
        )
        raw = await chat_completion(
            self._config,
            self._manager_prompt(workdir, "manager_prompt_patch_system"),
            user_payload,
            response_format={"type": "json_object"},
        )
        if not raw:
            return None
        parsed = loads_safe(raw, strict_first=False)
        if not isinstance(parsed, dict):
            return None
        return _normalize_general_learning_patch(parsed)

    async def _compact_manager_prompt_patches_llm(
        self,
        workdir: str,
        patches: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        user_payload = json.dumps(
            {"patches": patches},
            ensure_ascii=False,
            indent=2,
        )
        raw = await chat_completion(
            self._config,
            self._manager_prompt(workdir, "manager_prompt_compact_system"),
            user_payload,
            response_format={"type": "json_object"},
        )
        if not raw:
            raise RuntimeError("empty compact response")
        parsed = loads_safe(raw, strict_first=False)
        if not isinstance(parsed, dict):
            raise RuntimeError("compact response is not dict")
        compact = _normalize_general_learning_patch(parsed)
        if compact is None:
            raise RuntimeError("compact patch has no rules")
        if not (compact["added_rules"] or compact["changed_rules"] or compact["removed_rules"]):
            raise RuntimeError("compact patch has no rules")
        return compact

    async def _maybe_compact_manager_prompt_learning(
        self,
        workdir: str,
        learning: Dict[str, Any],
    ) -> Dict[str, Any]:
        patches = learning.get("patches")
        if not isinstance(patches, list) or len(patches) <= 20:
            return learning
        valid = [x for x in patches if isinstance(x, dict)]
        if len(valid) <= 20:
            learning["patches"] = valid
            return learning
        try:
            compact = await self._compact_manager_prompt_patches_llm(workdir, valid)
            learning["patches"] = [compact]
            return learning
        except Exception:
            _log.exception("manager prompt patch compaction failed")
            learning["patches"] = valid
            return learning

    async def _learn_from_final_spec_audit(
        self,
        *,
        workdir: str,
        original_goal: str,
        audit_result: Dict[str, Any],
    ) -> None:
        try:
            candidate = audit_result.get("manager_prompt_patch_candidate")
            patch: Optional[Dict[str, Any]] = None
            if isinstance(candidate, dict):
                patch = _normalize_general_learning_patch(candidate)
            if patch is None:
                patch = await self._build_manager_prompt_patch_from_final_audit(
                    original_goal=original_goal,
                    audit_result=audit_result,
                    workdir=workdir,
                )
            if patch is None:
                return

            learning = self._load_manager_prompt_learning(workdir)
            patches = learning.get("patches") if isinstance(learning.get("patches"), list) else []
            patches.append(patch)
            learning["patches"] = patches
            learning = await self._maybe_compact_manager_prompt_learning(workdir, learning)
            learning["active_version"] = int(learning.get("active_version", 1) or 1) + 1
            self._save_manager_prompt_learning(workdir, learning)
        except Exception:
            _log.exception("manager final audit prompt-learning failed")

    async def _run_final_spec_audit_and_close_gaps(
        self,
        *,
        session: Session,
        plan: ProjectPlan,
        bot,
        context,
        dest: Dict[str, Any],
        original_goal: str,
    ) -> Dict[str, Any]:
        chat_id = dest.get("chat_id")
        workdir = session.workdir
        timeout = int(getattr(self._config.defaults, "manager_dev_timeout_sec", 3600) or 3600)
        max_rounds = max(1, min(int(getattr(self._config.defaults, "manager_max_attempts", 3) or 3), 6))
        last_result: Optional[Dict[str, Any]] = None
        remaining_gaps_text = "- (нет)"

        for round_no in range(1, max_rounds + 1):
            if chat_id is not None:
                await self._send_runtime_message(
                    session,
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"🧪 Финальная проверка ТЗ ({round_no}/{max_rounds})...",
                    important=True,
                )
            if round_no == 1:
                prompt = self._manager_prompt(workdir, "final_spec_audit_task").format(
                    original_goal=original_goal
                )
            else:
                prompt = self._manager_prompt(workdir, "final_spec_audit_retry_task").format(
                    original_goal=original_goal,
                    remaining_gaps=remaining_gaps_text,
                )
            prompt = self._with_invariant_policy(workdir, prompt)
            prompt = self._apply_manager_prompt_learning(workdir, prompt)

            try:
                _, out = await run_prompt_routed_meta(
                    session,
                    self._config,
                    "development",
                    prompt,
                    response_format=CLIResponseFormat.JSON_OBJECT,
                    timeout_sec=timeout,
                    force_fresh=True,
                    chat_id=chat_id,
                )
            except Exception as e:
                _log.exception("manager final tz-audit call failed: %s", e)
                return {
                    "passed": False,
                    "summary_text": f"Финальный шаг проверки ТЗ завершился ошибкой: {e}",
                    "result": {"status": "FAIL", "remaining_gaps": ["Ошибка выполнения финальной проверки ТЗ"]},
                }

            text = strip_ansi(out or "")
            try:
                result = self._parse_final_spec_audit_json(text)
            except Exception as e:
                _log.warning(
                    "manager final tz-audit parse failed round=%s: %s",
                    round_no,
                    e,
                    exc_info=True,
                )
                result = self._build_retryable_final_spec_audit_failure(
                    summary=(
                        "Финальный шаг проверки ТЗ вернул невалидный JSON "
                        f"в раунде {round_no}: {e}"
                    ),
                    raw_text=text,
                )

            last_result = result
            remaining = list(result.get("remaining_gaps") or [])
            status = str(result.get("status") or "").upper()
            if status == "PASS" and not remaining:
                break
            if status == "GAP_FIXED" and not remaining:
                break
            remaining_gaps_text = "\n".join(f"- {x}" for x in remaining) if remaining else "- (не перечислены)"

        final = last_result or {
            "status": "FAIL",
            "summary": "Финальный шаг не вернул результата",
            "gaps_found": [],
            "fixes_applied": [],
            "remaining_gaps": ["Финальный шаг не вернул результата"],
            "tests": [],
            "lint": [],
        }
        has_fixes = bool(final.get("fixes_applied"))
        if has_fixes:
            await self._learn_from_final_spec_audit(
                workdir=workdir,
                original_goal=original_goal,
                audit_result=final,
            )

        final_status = str(final.get("status") or "").upper()
        remaining = list(final.get("remaining_gaps") or [])
        req_rows = list(final.get("requirement_matrix") or [])
        req_failed = [
            r for r in req_rows
            if str(r.get("status") or "").upper() in {"PARTIAL", "FAIL"}
        ]
        passed = final_status in {"PASS", "GAP_FIXED"} and not remaining
        summary_lines = [
            "Финальный шаг проверки исходного ТЗ:",
            f"- status: {final_status or 'UNKNOWN'}",
            f"- найдено gap: {len(final.get('gaps_found') or [])}",
            f"- доработок внесено: {len(final.get('fixes_applied') or [])}",
            f"- требований в матрице: {len(req_rows)}",
            f"- требований с проблемами: {len(req_failed)}",
            f"- осталось gap: {len(remaining)}",
            f"- итог: {'PASS' if passed else 'FAIL'}",
            f"- summary: {str(final.get('summary') or '').strip() or '(пусто)'}",
        ]
        if remaining:
            summary_lines.append("- remaining_gaps:")
            summary_lines.extend([f"  - {x}" for x in remaining[:10]])
        return {
            "passed": passed,
            "summary_text": "\n".join(summary_lines),
            "result": final,
        }

    # -----------------------------------------------------------------------
    # Final report
    # -----------------------------------------------------------------------

    async def _compose_final_report(self, plan: ProjectPlan, workdir: str = "") -> str:
        archive_enabled = bool(self._config.defaults.manager_response_archive)
        payload = json.dumps(asdict(plan), ensure_ascii=False, indent=2)
        if archive_enabled and workdir:
            _archive_response_write(workdir, "manager_final_report_prompt", "Final Report Prompt → Agent", payload)
        out = await chat_completion(
            self._config,
            self._manager_prompt(workdir, "final_report_system"),
            payload,
        )
        if archive_enabled and workdir:
            _archive_response_write(workdir, "agent_final_report_response", "Agent Final Report Response", out or "(empty)")
        return out or "Отчёт недоступен (пустой ответ модели)."

    # -----------------------------------------------------------------------
    # Git auto-commit after approved task
    # -----------------------------------------------------------------------

    @staticmethod
    async def _run_git(workdir: str, args: List[str]) -> Tuple[int, str]:
        """Run a git command in *workdir* and return (returncode, output)."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        command_label = "git " + " ".join(str(arg) for arg in args)

        def _run_sync() -> Tuple[int, str]:
            completed = subprocess.run(
                ["git", *args],
                cwd=workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=_GIT_COMMAND_TIMEOUT_SEC,
                check=False,
            )
            return int(completed.returncode or 0), str(completed.stdout or "")

        last_code = 1
        last_error = ""
        for attempt in range(1, _GIT_COMMAND_ATTEMPTS + 1):
            try:
                _log.debug(
                    "manager git command start attempt=%d/%d timeout_sec=%.1f cmd=%s",
                    attempt,
                    _GIT_COMMAND_ATTEMPTS,
                    _GIT_COMMAND_TIMEOUT_SEC,
                    command_label,
                )
                return await asyncio.to_thread(_run_sync)
            except FileNotFoundError:
                # git binary missing: treat as "git not usable" instead of crashing Manager.
                return 127, "git: command not found"
            except subprocess.TimeoutExpired:
                last_code = 124
                last_error = f"git: timed out after {_GIT_COMMAND_TIMEOUT_SEC:.1f}s: {command_label}"
                _log.warning(
                    "manager git command timed out attempt=%d/%d timeout_sec=%.1f cmd=%s",
                    attempt,
                    _GIT_COMMAND_ATTEMPTS,
                    _GIT_COMMAND_TIMEOUT_SEC,
                    command_label,
                )
                if attempt < _GIT_COMMAND_ATTEMPTS:
                    await asyncio.sleep(_GIT_COMMAND_RETRY_DELAY_SEC)
                    continue
            except Exception as e:
                last_code = 1
                last_error = f"git: failed to run: {e}"
                _log.warning(
                    "manager git command failed attempt=%d/%d cmd=%s error=%s",
                    attempt,
                    _GIT_COMMAND_ATTEMPTS,
                    command_label,
                    e,
                )
                if attempt < _GIT_COMMAND_ATTEMPTS:
                    await asyncio.sleep(_GIT_COMMAND_RETRY_DELAY_SEC)
                    continue
            break
        return last_code, last_error or f"git: failed to run: {command_label}"

    async def _auto_commit(self, session: Session, task: DevTask, plan: ProjectPlan, bot, context, dest: dict) -> bool:
        """Perform git add -A && git commit after an approved task. Returns True if committed."""
        if not bool(getattr(self._config.defaults, "manager_auto_commit", False)):
            return False

        chat_id = dest.get("chat_id")
        workdir = session.workdir
        archive_enabled = bool(self._config.defaults.manager_response_archive)

        # 1. Check git is usable for this project before running any git commands.
        if not self._git_is_usable(workdir):
            _log.info("auto_commit: git not usable for workdir, skipping")
            return False

        # 2. Check if there are changes
        code, status_out = await self._run_git(workdir, ["status", "--porcelain"])
        if code != 0:
            _log.warning("auto_commit: git status failed: %s", status_out)
            if chat_id is not None:
                await self._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⚠️ Git status failed: {(status_out or '')[:200]}",
                )
            return False
        if not status_out.strip():
            _log.info("auto_commit: no changes to commit")
            return False

        # 3. Get diff stat for commit message context
        code, stat_out = await self._run_git(workdir, ["diff", "--stat"])
        if code != 0:
            stat_out = ""
        # Include staged changes stat too
        code, staged_stat = await self._run_git(workdir, ["diff", "--staged", "--stat"])
        if code == 0 and staged_stat.strip():
            stat_out = f"{stat_out}\n{staged_stat}".strip()

        # Filter low-signal paths from git outputs to keep the LLM prompt focused.
        status_lines, status_summ = filter_git_porcelain_lines((status_out or "").splitlines())
        status_txt = "\n".join(status_lines).strip()

        stat_txt_raw = (stat_out or "").strip()
        stat_txt, stat_summ = filter_git_stat_text(stat_txt_raw)
        stat_txt = (stat_txt or "").strip()

        # 4. Generate commit message via LLM
        notes: List[str] = []
        n1 = status_summ.format_ru()
        if n1:
            notes.append(f"status: {n1}")
        n2 = stat_summ.format_ru()
        if n2:
            notes.append(f"stat: {n2}")
        notes_txt = ("\n".join(notes) + "\n\n") if notes else ""

        user_msg = (
            f"Задача: {task.title}\n"
            f"Описание: {task.description}\n"
            f"Критерии приёмки:\n{self._ui_service.task_acceptance(task)}\n\n"
            f"{notes_txt}"
            f"git status --porcelain:\n{status_txt}\n\n"
            f"git diff --stat:\n{stat_txt}"
        )
        if archive_enabled:
            _archive_response_write(
                workdir,
                f"manager_commit_prompt_{task.id}",
                f"Commit Message Prompt [{task.id}]",
                user_msg,
            )

        raw = await chat_completion(
            self._config,
            self._manager_prompt(workdir, "commit_message_system"),
            user_msg[:8000],
        )

        if archive_enabled:
            _archive_response_write(
                workdir,
                f"agent_commit_response_{task.id}",
                f"Commit Message Response [{task.id}]",
                raw or "(empty)",
            )

        summary_line = ""
        body_lines: List[str] = []
        if raw:
            in_body = False
            for line in raw.splitlines():
                if line.startswith("SUMMARY:"):
                    summary_line = line.replace("SUMMARY:", "", 1).strip()
                    continue
                if line.startswith("BODY:"):
                    in_body = True
                    continue
                if in_body and line.strip():
                    body_lines.append(line.rstrip())

        # Fallback: use task title as commit message
        if not summary_line:
            summary_line = f"[Manager] {task.title}"

        # Sanitize
        if len(summary_line) > 100:
            summary_line = summary_line[:100].rstrip()
        body = "\n".join(body_lines).strip()
        if len(body) > 2000:
            body = body[:2000].rstrip()

        # 5. git add -A
        code, add_out = await self._run_git(workdir, ["add", "-A"])
        if code != 0:
            _log.warning("auto_commit: git add failed: %s", add_out)
            if chat_id is not None:
                await self._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⚠️ Git add failed: {add_out[:200]}",
                )
            return False

        # 6. git commit
        args = ["commit", "-m", summary_line]
        if body:
            args += ["-m", body]
        code, commit_out = await self._run_git(workdir, args)
        if code != 0:
            _log.warning("auto_commit: git commit failed: %s", commit_out)
            if chat_id is not None:
                await self._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⚠️ Git commit failed: {commit_out[:200]}",
                )
            return False

        _log.info("auto_commit: committed for task %s: %s", task.id, summary_line)
        if chat_id is not None:
            await self._send_adapter_message(
                bot,
                context,
                chat_id=chat_id,
                text=f"📝 Коммит: {summary_line}",
            )
        return True

    async def _auto_commit_baseline_before_first_step(
        self,
        session: Session,
        plan: ProjectPlan,
        bot,
        context,
        dest: dict,
    ) -> bool:
        """Create a rollback baseline commit before the first executable step."""
        return await self._git_reconcile_service.auto_commit_baseline_before_first_step(
            session, plan, bot, context, dest
        )

    # -----------------------------------------------------------------------
    # Plan reconciliation after commit
    # -----------------------------------------------------------------------

    async def _reconcile_plan_after_commit(
        self, session: Session, task: DevTask, plan: ProjectPlan, bot, context, dest: dict,
    ) -> None:
        """After a commit, check if CLI did more than asked and adjust the plan accordingly."""
        await self._git_reconcile_service.reconcile_plan_after_commit(
            session, task, plan, bot, context, dest
        )

    async def _reconcile_plan_after_change_audit(
        self, session: Session, task: DevTask, plan: ProjectPlan, bot, context, dest: dict,
    ) -> None:
        """
        If git isn't used, detect 'CLI did more than asked' via filesystem change audit
        captured during the dev step, and adjust remaining tasks accordingly.
        """
        await self._git_reconcile_service.reconcile_plan_after_change_audit(
            session, task, plan, bot, context, dest
        )

    # -----------------------------------------------------------------------
    # External controls (UI commands)
    # -----------------------------------------------------------------------

    def pause(self, session: Session) -> None:
        plan = self._load_live_plan(session)
        if not plan:
            return
        plan.set_status("paused")
        self._save_plan_with_run_artifacts(session, plan, phase=manager_run_phase_for_plan(plan, fallback="develop"))

    def reset(self, session: Session) -> None:
        plan = self._load_live_plan(session)
        if plan:
            self._archive_live_plan(session, plan.status)
        self._delete_live_plan(session)
