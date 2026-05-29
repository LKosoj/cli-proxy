from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional

from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class PlanManagementService:
    """Domain operations around ProjectPlan lifecycle and payload mapping."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        try:
            parsed = int(max_attempts or 0)
        except Exception:
            parsed = 0
        self._default_max_attempts = max(1, parsed)

    @staticmethod
    def _normalize_list(values: Any) -> List[str]:
        if not isinstance(values, list):
            return []
        out: List[str] = []
        for item in values:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _normalize_checklist_row(row: Dict[str, Any]) -> Dict[str, str]:
        return {
            "item": str(row.get("item") or "").strip(),
            "status": str(row.get("status") or "").strip(),
            "how": str(row.get("how") or "").strip(),
            "why_not": str(row.get("why_not") or "").strip(),
        }

    @classmethod
    def _normalize_checklist_table(cls, checklist_raw: Any) -> List[Dict[str, str]]:
        if not isinstance(checklist_raw, list):
            return []
        out: List[Dict[str, str]] = []
        for row in checklist_raw:
            if isinstance(row, dict):
                out.append(cls._normalize_checklist_row(row))
        return out

    def create_plan(
        self,
        *,
        project_goal: str,
        analysis: Optional[ProjectAnalysis] = None,
        status: str = "active",
        max_tasks_limit: int = 0,
    ) -> ProjectPlan:
        now = _now_iso()
        plan = ProjectPlan(
            project_goal=str(project_goal or "").strip(),
            tasks=[],
            analysis=analysis,
            status=str(status or "active"),
            created_at=now,
            updated_at=now,
            current_task_id=None,
        )
        limit = int(max_tasks_limit or 0)
        if limit > 0:
            setattr(plan, "_manager_max_tasks_limit", limit)
        return plan

    def serialize_analysis(self, analysis: Optional[ProjectAnalysis]) -> Dict[str, Any]:
        if analysis is None:
            return {}
        return {
            "current_state": str(analysis.current_state or ""),
            "already_done": [str(x) for x in (analysis.already_done or []) if str(x).strip()],
            "remaining_work": [str(x) for x in (analysis.remaining_work or []) if str(x).strip()],
            "requirements": [str(x) for x in (analysis.requirements or []) if str(x).strip()],
            "checklist_table": self._normalize_checklist_table(analysis.checklist_table),
        }

    def serialize_tasks(self, tasks: List[DevTask]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for task in list(tasks or []):
            out.append(
                {
                    "id": str(task.id or "").strip(),
                    "title": str(task.title or "").strip(),
                    "description": str(task.description or "").strip(),
                    "acceptance_criteria": [str(x) for x in (task.acceptance_criteria or []) if str(x).strip()],
                    "covers_requirements": [str(x) for x in (task.covers_requirements or []) if str(x).strip()],
                    "depends_on": [str(x) for x in (task.depends_on or []) if str(x).strip()],
                }
            )
        return out

    def serialize_plan(self, plan: ProjectPlan) -> Dict[str, Any]:
        payload = asdict(plan)
        limit = int(getattr(plan, "_manager_max_tasks_limit", 0) or 0)
        if limit > 0:
            payload["manager_max_tasks_limit"] = limit
        return payload

    def add_task(
        self,
        plan: ProjectPlan,
        task_payload: Dict[str, Any],
        *,
        fallback_index: int,
        max_attempts: Optional[int] = None,
    ) -> Optional[DevTask]:
        if not isinstance(task_payload, dict):
            return None

        tid = str(task_payload.get("id") or "").strip()
        if not tid:
            tid = f"task_{int(fallback_index)}"
        title = str(task_payload.get("title") or f"Задача {int(fallback_index)}").strip()
        description = str(task_payload.get("description") or "").strip()
        acceptance = self._normalize_list(task_payload.get("acceptance_criteria"))
        covers = self._normalize_list(task_payload.get("covers_requirements"))
        raw_deps = self._normalize_list(task_payload.get("depends_on"))
        deps_seen: set[str] = set()
        depends_on = [dep for dep in raw_deps if not (dep in deps_seen or deps_seen.add(dep))]

        attempts_limit = self._default_max_attempts
        if max_attempts is not None:
            try:
                attempts_limit = max(1, int(max_attempts))
            except Exception:
                attempts_limit = self._default_max_attempts

        task = DevTask(
            id=tid,
            title=title,
            description=description,
            acceptance_criteria=acceptance,
            covers_requirements=covers,
            depends_on=depends_on,
            max_attempts=attempts_limit,
        )
        plan.tasks.append(task)
        return task

    @staticmethod
    def mark_task_completed(
        plan: ProjectPlan,
        task_id: str,
        *,
        now_iso: Optional[Callable[[], str]] = None,
    ) -> bool:
        tid = str(task_id or "").strip()
        if not tid:
            return False
        now_fn = now_iso or _now_iso
        for task in list(plan.tasks or []):
            if str(task.id or "").strip() != tid:
                continue
            task.set_status("approved")
            task.completed_at = now_fn()
            if not (task.review_verdict or "").strip():
                task.review_verdict = "approved"
            if str(plan.current_task_id or "").strip() == tid:
                plan.current_task_id = None
            return True
        return False

    @staticmethod
    def get_next_pending_task(plan: ProjectPlan) -> Optional[DevTask]:
        tasks_by_id = {str(t.id or "").strip(): t for t in list(plan.tasks or []) if str(t.id or "").strip()}
        for task in list(plan.tasks or []):
            status = str(task.status or "").strip()
            if status in ("approved", "failed", "blocked"):
                continue
            deps = [tasks_by_id.get(str(dep or "").strip()) for dep in list(task.depends_on or [])]
            deps = [dep for dep in deps if dep is not None]
            if any(str(dep.status or "").strip() == "failed" for dep in deps):
                if status not in ("approved", "failed"):
                    task.set_status("blocked")
                continue
            if deps and not all(str(dep.status or "").strip() == "approved" for dep in deps):
                continue
            return task
        return None

    def analysis_from_payload(self, payload: Dict[str, Any]) -> Optional[ProjectAnalysis]:
        analysis_raw = payload.get("project_analysis") or payload.get("analysis")
        checklist_raw: Any
        if isinstance(analysis_raw, dict):
            checklist_raw = analysis_raw.get("checklist_table")
        else:
            checklist_raw = None
        if not isinstance(checklist_raw, list):
            checklist_raw = payload.get("checklist_table")
        checklist_table = self._normalize_checklist_table(checklist_raw)

        if not isinstance(analysis_raw, dict):
            if checklist_table:
                return ProjectAnalysis(
                    current_state="",
                    already_done=[],
                    remaining_work=[],
                    requirements=[],
                    checklist_table=checklist_table,
                )
            return None

        return ProjectAnalysis(
            current_state=str(analysis_raw.get("current_state") or ""),
            already_done=self._normalize_list(analysis_raw.get("already_done")),
            remaining_work=self._normalize_list(analysis_raw.get("remaining_work")),
            requirements=self._normalize_list(analysis_raw.get("requirements")),
            checklist_table=checklist_table,
        )

    def plan_from_payload(
        self,
        payload: Dict[str, Any],
        *,
        user_goal: str,
        max_tasks: int,
    ) -> Optional[ProjectPlan]:
        if not isinstance(payload, dict):
            return None
        tasks_raw = payload.get("tasks") or []
        if not isinstance(tasks_raw, list) or not tasks_raw:
            return None

        analysis = self.analysis_from_payload(payload)
        goal = str(payload.get("project_goal") or user_goal or "").strip()
        plan = self.create_plan(
            project_goal=goal,
            analysis=analysis,
            status="active",
            max_tasks_limit=max(0, int(max_tasks or 0)),
        )

        limit = max(1, int(max_tasks or 1))
        for idx, task_payload in enumerate(tasks_raw[:limit], start=1):
            self.add_task(plan, task_payload, fallback_index=idx)

        if not plan.tasks:
            return None
        return plan

    @staticmethod
    def merge_analysis_context(
        source: ProjectPlan,
        target: Optional[ProjectPlan],
    ) -> Optional[ProjectPlan]:
        if target is None:
            return None

        source_max_tasks_limit = int(getattr(source, "_manager_max_tasks_limit", 0) or 0)
        if source_max_tasks_limit > 0:
            setattr(target, "_manager_max_tasks_limit", source_max_tasks_limit)
        source_frozen_min = int(getattr(source, "_manager_min_tasks_dynamic", 0) or 0)
        if source_frozen_min > 0:
            if source_max_tasks_limit > 0:
                source_frozen_min = min(source_frozen_min, source_max_tasks_limit)
            setattr(target, "_manager_min_tasks_dynamic", int(source_frozen_min))

        source_analysis = getattr(source, "analysis", None)
        if not source_analysis:
            return target

        checklist_copy: List[Dict[str, Any]] = []
        for row in list(source_analysis.checklist_table or []):
            if isinstance(row, dict):
                checklist_copy.append(dict(row))
        target.analysis = ProjectAnalysis(
            current_state=str(source_analysis.current_state or ""),
            already_done=list(source_analysis.already_done or []),
            remaining_work=list(source_analysis.remaining_work or []),
            requirements=list(source_analysis.requirements or []),
            checklist_table=checklist_copy,
        )
        return target

    def update_plan_analysis(
        self,
        plan: ProjectPlan,
        updated_analysis: Any,
    ) -> bool:
        if not isinstance(updated_analysis, dict):
            return False
        if not plan.analysis:
            plan.analysis = ProjectAnalysis(current_state="", already_done=[], remaining_work=[])

        changed = False
        current_state_raw = updated_analysis.get("current_state")
        if isinstance(current_state_raw, str) and current_state_raw.strip():
            new_state = current_state_raw.strip()
            if new_state != str(plan.analysis.current_state or ""):
                plan.analysis.current_state = new_state
                changed = True

        already_done_raw = updated_analysis.get("already_done")
        if isinstance(already_done_raw, list):
            new_done = [str(x) for x in already_done_raw if str(x).strip()]
            if new_done != list(plan.analysis.already_done or []):
                plan.analysis.already_done = new_done
                changed = True

        remaining_raw = updated_analysis.get("remaining_work")
        if isinstance(remaining_raw, list):
            new_remaining = [str(x) for x in remaining_raw if str(x).strip()]
            if new_remaining != list(plan.analysis.remaining_work or []):
                plan.analysis.remaining_work = new_remaining
                changed = True

        requirements_raw = updated_analysis.get("requirements")
        if isinstance(requirements_raw, list):
            new_requirements = [str(x) for x in requirements_raw if str(x).strip()]
            if new_requirements != list(plan.analysis.requirements or []):
                plan.analysis.requirements = new_requirements
                changed = True

        checklist_raw = updated_analysis.get("checklist_table")
        if isinstance(checklist_raw, list):
            new_checklist = self._normalize_checklist_table(checklist_raw)
            if new_checklist != list(plan.analysis.checklist_table or []):
                plan.analysis.checklist_table = new_checklist
                changed = True

        return changed

    def update_analysis(
        self,
        plan: ProjectPlan,
        updated_analysis: Any,
    ) -> bool:
        return self.update_plan_analysis(plan, updated_analysis)
