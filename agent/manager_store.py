from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

import logging

from .contracts import DevTask, ProjectAnalysis, ProjectPlan

_log = logging.getLogger(__name__)


def _plan_path(workdir: str) -> str:
    return os.path.join(workdir, "MANAGER_PLAN.json")


def _archive_dir(workdir: str) -> str:
    return os.path.join(workdir, "archive")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _task_from_dict(d: Dict[str, Any]) -> DevTask:
    return DevTask(
        id=str(d.get("id") or "").strip(),
        title=str(d.get("title") or "").strip(),
        description=str(d.get("description") or "").strip(),
        acceptance_criteria=list(d.get("acceptance_criteria") or []),
        depends_on=[str(x) for x in (d.get("depends_on") or []) if x],
        status=str(d.get("status") or "pending"),
        attempt=int(d.get("attempt") or 0),
        max_attempts=int(d.get("max_attempts") or 3),
        dev_report=d.get("dev_report"),
        review_verdict=d.get("review_verdict"),
        review_comments=d.get("review_comments"),
        rejection_history=list(d.get("rejection_history") or []),
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
    )


def _analysis_from_dict(d: Dict[str, Any]) -> ProjectAnalysis:
    return ProjectAnalysis(
        current_state=str(d.get("current_state") or ""),
        already_done=list(d.get("already_done") or []),
        remaining_work=list(d.get("remaining_work") or []),
    )


def _plan_from_dict(d: Dict[str, Any]) -> ProjectPlan:
    analysis = d.get("analysis")
    return ProjectPlan(
        project_goal=str(d.get("project_goal") or ""),
        tasks=[_task_from_dict(x) for x in (d.get("tasks") or []) if isinstance(x, dict)],
        analysis=_analysis_from_dict(analysis) if isinstance(analysis, dict) else None,
        status=str(d.get("status") or "active"),
        created_at=str(d.get("created_at") or ""),
        updated_at=str(d.get("updated_at") or ""),
        current_task_id=d.get("current_task_id"),
        completion_report=d.get("completion_report"),
    )


def load_plan(workdir: str) -> Optional[ProjectPlan]:
    """Загрузить план из MANAGER_PLAN.json. Вернуть None, если файла нет."""
    path = _plan_path(workdir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return None
            return _plan_from_dict(payload)
    except Exception as e:
        _log.exception("tool failed load_plan: %s", e)
        return None


def save_plan(workdir: str, plan: ProjectPlan) -> None:
    """Атомарно сохранить план в MANAGER_PLAN.json."""
    path = _plan_path(workdir)
    os.makedirs(workdir, exist_ok=True)
    tmp = f"{path}.tmp"
    payload = asdict(plan)
    if not plan.created_at:
        payload["created_at"] = _now_iso()
    payload["updated_at"] = _now_iso()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        _log.exception("tool failed save_plan: %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def delete_plan(workdir: str) -> None:
    """Удалить файл плана (при /manager reset)."""
    path = _plan_path(workdir)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        _log.exception("tool failed delete_plan: %s", e)


def archive_plan(workdir: str, status: str) -> Optional[str]:
    """Переместить MANAGER_PLAN.json в archive/MANAGER_PLAN_{date}_{status}.json."""
    src = _plan_path(workdir)
    if not os.path.exists(src):
        return None
    dst_dir = _archive_dir(workdir)
    os.makedirs(dst_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    safe_status = "".join(ch for ch in (status or "unknown") if ch.isalnum() or ch in ("_", "-")) or "unknown"
    dst = os.path.join(dst_dir, f"MANAGER_PLAN_{stamp}_{safe_status}.json")
    try:
        os.replace(src, dst)
        return dst
    except Exception as e:
        _log.exception("tool failed archive_plan: %s", e)
        return None

