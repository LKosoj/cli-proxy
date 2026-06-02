from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional, Tuple

from modes.sdk.file_lock import lock_file, unlock_file
from modes.sdk.runtime.json_normalizer import loads_safe
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan
from sessions.scoped_key import is_session_scoped_key, sanitize_scoped_key_token
from utils.paths import cli_proxy_artifact_path

_log = logging.getLogger(__name__)

MANAGER_CONTINUE_TOKEN = "__MANAGER_CONTINUE__"

# ---------------------------------------------------------------------------
# Plan write-back observers
# ---------------------------------------------------------------------------
# A consumer (e.g. the SDD mode) can register a callback keyed by
# (workdir, scoped_key) that fires after every successful save_plan() for that
# plan. This is the single chokepoint through which BOTH the manager engine
# (ManagerOrchestrator) and the manager transport (ManagerMode) persist plans,
# so every decompose / reconcile / final-audit / resume mutation is covered.
# The mechanism is consumer-agnostic: planning knows nothing about specs/ or SDD.
PlanObserver = Callable[[ProjectPlan], None]

_plan_observers: Dict[Tuple[str, str], PlanObserver] = {}


def _observer_key(workdir: str, scoped_key: Optional[str]) -> Tuple[str, str]:
    return (str(workdir or ""), str(scoped_key or "").strip())


def register_plan_observer(workdir: str, scoped_key: Optional[str], observer: PlanObserver) -> None:
    """Register a write-back observer fired after every save_plan for this plan.

    The key must match the (workdir, scoped_key) the manager uses to persist the
    plan — i.e. the same scoped_key returned by session_scoped_key(session).
    """
    _plan_observers[_observer_key(workdir, scoped_key)] = observer


def unregister_plan_observer(workdir: str, scoped_key: Optional[str]) -> None:
    """Remove a previously registered write-back observer (no-op if absent)."""
    _plan_observers.pop(_observer_key(workdir, scoped_key), None)


def _notify_plan_observer(workdir: str, scoped_key: Optional[str], plan: ProjectPlan) -> None:
    observer = _plan_observers.get(_observer_key(workdir, scoped_key))
    if observer is None:
        return
    try:
        observer(plan)
    except Exception:
        # The plan is already persisted; a sink failure must be loud but must not
        # break the autonomous manager pipeline.
        _log.exception("plan observer failed (workdir=%s scoped_key=%s)", workdir, scoped_key)


class ManagerDecomposeNormalizationError(RuntimeError):
    """Raised when manager decompose output cannot be normalized to a valid plan."""

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(
            str(
                message
                or (
                    "Не удалось построить план: ответ декомпозиции не распознан как валидный JSON-план. "
                    "Пришлите задачу заново в формате: outcome, ограничения, проверки."
                )
            )
        )


_LEGACY_PLAN_FILENAME = "MANAGER_PLAN.json"


def _legacy_plan_path(workdir: str) -> str:
    return os.path.join(workdir, _LEGACY_PLAN_FILENAME)


def _scoped_plan_dir(workdir: str) -> str:
    return cli_proxy_artifact_path(workdir, ".manager/plans")


def _scoped_plan_path(workdir: str, scoped_key: str) -> str:
    token = sanitize_scoped_key_token(scoped_key)
    return os.path.join(_scoped_plan_dir(workdir), f"plan_{token}.json")


def manager_plan_path(workdir: str, scoped_key: Optional[str] = None) -> str:
    token = sanitize_scoped_key_token(scoped_key)
    if token and is_session_scoped_key(token):
        return _scoped_plan_path(workdir, token)
    return _legacy_plan_path(workdir)


def _plan_path(workdir: str, scoped_key: Optional[str] = None) -> str:
    return manager_plan_path(workdir, scoped_key=scoped_key)


def _lock_path(path: str) -> str:
    abs_path = os.path.abspath(path)
    marker = f"{os.sep}.cli-proxy{os.sep}"
    if marker in abs_path:
        base_dir = abs_path.split(marker, 1)[0]
    else:
        base_dir = os.path.dirname(abs_path)
    lock_dir = cli_proxy_artifact_path(base_dir, ".manager")
    return os.path.join(lock_dir, f"{os.path.basename(path)}.lock")


def _archive_dir(workdir: str) -> str:
    return cli_proxy_artifact_path(workdir, ".manager_archive")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _scoped_plan_candidates(workdir: str) -> list[str]:
    plan_dir = _scoped_plan_dir(workdir)
    if not os.path.isdir(plan_dir):
        return []
    return sorted(
        os.path.join(plan_dir, name)
        for name in os.listdir(plan_dir)
        if name.startswith("plan_") and name.endswith(".json")
    )


def _single_scoped_plan_path(workdir: str) -> Optional[str]:
    candidates = _scoped_plan_candidates(workdir)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_plan_path(
    workdir: str,
    *,
    scoped_key: Optional[str] = None,
    migrate_legacy: bool,
) -> str:
    token = sanitize_scoped_key_token(scoped_key)
    if not token or not is_session_scoped_key(token):
        legacy_path = _legacy_plan_path(workdir)
        if os.path.exists(legacy_path):
            return legacy_path
        singleton_path = _single_scoped_plan_path(workdir)
        if singleton_path:
            return singleton_path
        return legacy_path

    scoped_path = _scoped_plan_path(workdir, token)
    if os.path.exists(scoped_path):
        return scoped_path
    if not migrate_legacy:
        return scoped_path

    legacy_path = _legacy_plan_path(workdir)
    if not os.path.exists(legacy_path):
        return scoped_path

    try:
        with _fs_locked(legacy_path, shared=False):
            if os.path.exists(scoped_path) or not os.path.exists(legacy_path):
                return scoped_path
            _ensure_parent(scoped_path)
            os.replace(legacy_path, scoped_path)
    except Exception:
        _log.exception(
            "planning scoped legacy migration failed workdir=%s scoped_key=%s",
            workdir,
            token,
        )
        return legacy_path

    _log.warning(
        "planning migrated legacy manager plan to scoped storage: %s -> %s",
        legacy_path,
        scoped_path,
    )
    return scoped_path


@contextmanager
def _fs_locked(path: str, *, shared: bool):
    lpath = _lock_path(path)
    _ensure_parent(lpath)
    with open(lpath, "a+", encoding="utf-8") as lock_fh:
        lock_file(lock_fh, shared=shared)
        try:
            yield
        finally:
            unlock_file(lock_fh)


def _read_json_unlocked(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback = dict(default or {})
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return fallback
        payload = loads_safe(raw, strict_first=True)
        if isinstance(payload, dict):
            return payload
        return fallback
    except Exception:
        return fallback


def _write_json_unlocked(path: str, data: Dict[str, Any]) -> None:
    _ensure_parent(path)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            _log.exception("planning write_json_locked cleanup failed: %s", tmp)
        raise


def read_json_locked(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _fs_locked(path, shared=True):
        return _read_json_unlocked(path, default=default)


def write_json_locked(path: str, data: Dict[str, Any]) -> None:
    payload = data if isinstance(data, dict) else {}
    with _fs_locked(path, shared=False):
        _write_json_unlocked(path, payload)


def _task_from_dict(d: Dict[str, Any]) -> DevTask:
    return DevTask(
        id=str(d.get("id") or "").strip(),
        title=str(d.get("title") or "").strip(),
        description=str(d.get("description") or "").strip(),
        acceptance_criteria=list(d.get("acceptance_criteria") or []),
        covers_requirements=[str(x) for x in (d.get("covers_requirements") or []) if x],
        depends_on=[str(x) for x in (d.get("depends_on") or []) if x],
        status=str(d.get("status") or "pending"),
        attempt=int(d.get("attempt") or 0),
        max_attempts=int(d.get("max_attempts") or 3),
        dev_report=d.get("dev_report"),
        review_verdict=d.get("review_verdict"),
        review_comments=d.get("review_comments"),
        rejection_history=list(d.get("rejection_history") or []),
        partial_work_note=d.get("partial_work_note"),
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
        manager_change_audit=d.get("manager_change_audit"),
        manager_change_audit_has_changes=(
            d.get("manager_change_audit_has_changes")
            if isinstance(d.get("manager_change_audit_has_changes"), bool)
            else None
        ),
    )


def _analysis_from_dict(d: Dict[str, Any]) -> ProjectAnalysis:
    return ProjectAnalysis(
        current_state=str(d.get("current_state") or ""),
        already_done=list(d.get("already_done") or []),
        remaining_work=list(d.get("remaining_work") or []),
        requirements=list(d.get("requirements") or []),
        checklist_table=list(d.get("checklist_table") or []),
    )


def _plan_from_dict(d: Dict[str, Any]) -> ProjectPlan:
    analysis = d.get("analysis")
    plan = ProjectPlan(
        project_goal=str(d.get("project_goal") or ""),
        tasks=[_task_from_dict(x) for x in (d.get("tasks") or []) if isinstance(x, dict)],
        analysis=_analysis_from_dict(analysis) if isinstance(analysis, dict) else None,
        status=str(d.get("status") or "active"),
        created_at=str(d.get("created_at") or ""),
        updated_at=str(d.get("updated_at") or ""),
        current_task_id=d.get("current_task_id"),
        completion_report=d.get("completion_report"),
    )
    raw_limit = d.get("manager_max_tasks_limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except Exception:
            limit = 0
        if limit > 0:
            setattr(plan, "_manager_max_tasks_limit", limit)
    return plan


def load_plan(workdir: str, scoped_key: Optional[str] = None) -> Optional[ProjectPlan]:
    path = _resolve_plan_path(workdir, scoped_key=scoped_key, migrate_legacy=True)
    try:
        payload = read_json_locked(path, default={})
        if not payload:
            if scoped_key is None and path == _legacy_plan_path(workdir):
                singleton_path = _single_scoped_plan_path(workdir)
                if singleton_path:
                    payload = read_json_locked(singleton_path, default={})
            if not payload:
                return None
        return _plan_from_dict(payload)
    except Exception as exc:
        _log.exception("planning load_plan failed: %s", exc)
        return None


def save_plan(workdir: str, plan: ProjectPlan, scoped_key: Optional[str] = None) -> None:
    path = _resolve_plan_path(workdir, scoped_key=scoped_key, migrate_legacy=True)
    _ensure_parent(path)
    payload = asdict(plan)
    max_tasks_limit = int(getattr(plan, "_manager_max_tasks_limit", 0) or 0)
    if max_tasks_limit > 0:
        payload["manager_max_tasks_limit"] = max_tasks_limit
    if not plan.created_at:
        payload["created_at"] = _now_iso()
    payload["updated_at"] = _now_iso()
    try:
        write_json_locked(path, payload)
    except Exception as exc:
        _log.exception("planning save_plan failed: %s", exc)
        raise
    _notify_plan_observer(workdir, scoped_key, plan)


def delete_plan(workdir: str, scoped_key: Optional[str] = None) -> None:
    path = _resolve_plan_path(workdir, scoped_key=scoped_key, migrate_legacy=True)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        _log.exception("planning delete_plan failed: %s", exc)


def archive_plan(workdir: str, status: str, scoped_key: Optional[str] = None) -> Optional[str]:
    src = _resolve_plan_path(workdir, scoped_key=scoped_key, migrate_legacy=True)
    dst_dir = _archive_dir(workdir)
    try:
        with _fs_locked(src, shared=False):
            if not os.path.exists(src):
                return None
            os.makedirs(dst_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
            safe_status = "".join(ch for ch in (status or "unknown") if ch.isalnum() or ch in ("_", "-")) or "unknown"
            token = sanitize_scoped_key_token(scoped_key)
            if token and is_session_scoped_key(token):
                dst_name = f"plan_{token}_{stamp}_{safe_status}.json"
            else:
                dst_name = f"MANAGER_PLAN_{stamp}_{safe_status}.json"
            dst = os.path.join(dst_dir, dst_name)
            os.replace(src, dst)
            return dst
    except Exception as exc:
        _log.exception("planning archive_plan failed: %s", exc)
        return None


def _normalize_status(status: str) -> str:
    return str(status or "").strip().lower()


def can_resume_failed(plan: ProjectPlan) -> bool:
    for task in plan.tasks:
        status = _normalize_status(str(getattr(task, "status", "") or ""))
        if status in {"pending", "rejected", "in_progress", "in_review", "blocked"}:
            return True
        if status == "failed" and int(getattr(task, "attempt", 0) or 0) < int(getattr(task, "max_attempts", 0) or 0):
            return True
    return False


def needs_resume_choice(plan: Optional[ProjectPlan], *, auto_resume: bool, user_text: str) -> bool:
    if not plan or str(getattr(plan, "status", "") or "") not in {"active", "paused"}:
        return False
    if auto_resume and str(getattr(plan, "status", "") or "") != "paused":
        return False
    txt = str(user_text or "").strip()
    if not txt:
        return False
    if txt == MANAGER_CONTINUE_TOKEN:
        return False
    return True


def needs_failed_resume_choice(plan: Optional[ProjectPlan], *, auto_resume: bool, user_text: str) -> bool:
    if not plan or str(getattr(plan, "status", "") or "") != "failed":
        return False
    if auto_resume:
        return False
    if not can_resume_failed(plan):
        return False
    txt = str(user_text or "").strip()
    if not txt:
        return False
    if txt == MANAGER_CONTINUE_TOKEN:
        return False
    return True


def _plan_summary(plan: ProjectPlan) -> str:
    done = sum(1 for task in plan.tasks if str(getattr(task, "status", "")) == "approved")
    total = len(plan.tasks)
    return f"План: {done}/{total} задач выполнено. Статус: {plan.status}."


def format_manager_status_brief(plan: ProjectPlan, *, max_comment_chars: int = 400) -> str:
    def _emoji(status: str) -> str:
        mapping = {
            "approved": "✅",
            "in_review": "🔄",
            "in_progress": "🔧",
            "pending": "⏳",
            "rejected": "❌",
            "failed": "❌",
            "blocked": "⛔",
            "paused": "💤",
        }
        return mapping.get(status, "•")

    lines = [_plan_summary(plan)]
    if plan.created_at or plan.updated_at:
        lines.append(f"Создан: {plan.created_at or '—'} | Обновлён: {plan.updated_at or '—'}")
    if plan.current_task_id:
        lines.append(f"Текущая задача: {plan.current_task_id}")
    lines.append("")

    tasks_by_id = {task.id: task for task in (plan.tasks or []) if getattr(task, "id", None)}
    for idx, task in enumerate(plan.tasks, start=1):
        dep = f" | зависит от: {', '.join(task.depends_on)}" if task.depends_on else ""
        lines.append(f"{idx}. {_emoji(task.status)} {task.title} [{task.status}] (попытка {task.attempt}/{task.max_attempts}){dep}")
        if task.status == "blocked":
            deps = [tasks_by_id.get(dep_id) for dep_id in (task.depends_on or [])]
            deps = [d for d in deps if d is not None]
            failed = [d.id for d in deps if d.status == "failed"]
            waiting = [d.id for d in deps if d.status != "approved"]
            if failed:
                lines.append(f"   └ Причина: заблокирована из-за failed зависимостей: {', '.join(failed)}")
            elif waiting:
                lines.append(f"   └ Причина: ожидает выполнения зависимостей: {', '.join(waiting)}")
            else:
                lines.append("   └ Причина: заблокирована (детали неизвестны)")
        if task.status in ("rejected", "failed") and task.review_comments:
            comments = str(task.review_comments or "").strip()
            if len(comments) > max_comment_chars:
                comments = comments[:max_comment_chars] + "…"
            lines.append(f"   └ Замечания: {comments}")
    return "\n".join(lines)


__all__ = [
    "DevTask",
    "ProjectPlan",
    "MANAGER_CONTINUE_TOKEN",
    "ManagerDecomposeNormalizationError",
    "PlanObserver",
    "archive_plan",
    "can_resume_failed",
    "delete_plan",
    "format_manager_status_brief",
    "load_plan",
    "needs_failed_resume_choice",
    "needs_resume_choice",
    "read_json_locked",
    "register_plan_observer",
    "save_plan",
    "unregister_plan_observer",
    "write_json_locked",
]
