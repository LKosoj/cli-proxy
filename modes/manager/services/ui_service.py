from __future__ import annotations

from typing import Dict, List, Tuple

from modes.sdk.runtime.contracts import DevTask, ProjectPlan


class ManagerUIService:
    """Pure text-formatting helpers for Manager mode UI/runtime messages."""

    @staticmethod
    def format_acceptance(items: List[str]) -> str:
        if not items:
            return "- (нет критериев)"
        return "\n".join([f"- {x}" for x in items])

    @classmethod
    def task_acceptance(cls, task: DevTask) -> str:
        return cls.format_acceptance(task.acceptance_criteria)

    @staticmethod
    def plan_summary(plan: ProjectPlan) -> str:
        done = sum(1 for t in plan.tasks if t.status == "approved")
        total = len(plan.tasks)
        return f"План: {done}/{total} задач выполнено. Статус: {plan.status}."

    @staticmethod
    def truncate_report(text: str, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text or ""
        head_size = max_chars * 3 // 8
        tail_size = max_chars * 5 // 8
        skipped = len(text) - head_size - tail_size
        return f"{text[:head_size]}\n\n...(обрезано {skipped} символов)...\n\n{text[-tail_size:]}"

    @staticmethod
    def merge_rejection_feedback(review_comments: str, arbiter_reasons: List[str]) -> str:
        parts: List[str] = []
        comments = (review_comments or "").strip()
        if comments:
            parts.append(f"Замечания ревьюера:\n{comments}")

        reasons_clean: List[str] = []
        seen = set()
        for item in arbiter_reasons or []:
            txt = str(item or "").strip()
            if not txt or txt in seen:
                continue
            seen.add(txt)
            reasons_clean.append(txt)
        if reasons_clean:
            reasons_text = "\n".join(f"- {r}" for r in reasons_clean)
            parts.append(f"Причины арбитра:\n{reasons_text}")

        if not parts:
            return "Требуется доработка: причины отклонения не были явно сформулированы."
        return "\n\n".join(parts)

    @staticmethod
    def describe_failed_plan_reason(plan: ProjectPlan) -> str:
        for task in plan.tasks:
            if task.status not in ("failed", "rejected"):
                continue
            comments = (task.review_comments or "").strip()
            if comments:
                return f"{task.title}: {comments}"

        exhausted = [
            t for t in plan.tasks
            if t.status == "failed" and t.attempt >= t.max_attempts
        ]
        if exhausted:
            first = exhausted[0]
            return (
                f"Задача «{first.title}» достигла лимита попыток "
                f"({first.attempt}/{first.max_attempts})."
            )

        blocked = [t for t in plan.tasks if t.status == "blocked"]
        if blocked:
            first = blocked[0]
            deps = ", ".join(first.depends_on) if first.depends_on else "неизвестной зависимости"
            return f"Задача «{first.title}» заблокирована из-за {deps}."

        return "План находится в состоянии failed (детальная причина не сохранена)."

    @staticmethod
    def task_progress(plan: ProjectPlan, task: DevTask) -> Tuple[int, int]:
        total = len(plan.tasks)
        for idx, candidate in enumerate(plan.tasks, start=1):
            if candidate is task or candidate.id == task.id:
                return idx, total
        return 0, total

    @staticmethod
    def _status_emoji(status: str) -> str:
        mapping: Dict[str, str] = {
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

    @classmethod
    def _append_task_details(
        cls,
        lines: List[str],
        task: DevTask,
        *,
        tasks_by_id: Dict[str, DevTask],
        max_comment_chars: int,
    ) -> None:
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
            comments = task.review_comments.strip()
            if len(comments) > max_comment_chars:
                comments = comments[:max_comment_chars] + "…"
            lines.append(f"   └ Замечания: {comments}")

    @classmethod
    def format_manager_status(cls, plan: ProjectPlan, *, max_comment_chars: int = 400) -> str:
        lines: List[str] = []
        lines.append(f"📋 План: «{plan.project_goal}»")
        lines.append(cls.plan_summary(plan))
        if plan.created_at or plan.updated_at:
            lines.append(f"Создан: {plan.created_at or '—'} | Обновлён: {plan.updated_at or '—'}")
        if plan.current_task_id:
            lines.append(f"Текущая задача: {plan.current_task_id}")
        lines.append("")

        tasks_by_id = {t.id: t for t in (plan.tasks or []) if getattr(t, "id", None)}
        for i, task in enumerate(plan.tasks, start=1):
            dep = f" | зависит от: {', '.join(task.depends_on)}" if task.depends_on else ""
            lines.append(
                f"{i}. {cls._status_emoji(task.status)} {task.title} "
                f"[{task.status}] (попытка {task.attempt}/{task.max_attempts}){dep}"
            )
            cls._append_task_details(
                lines,
                task,
                tasks_by_id=tasks_by_id,
                max_comment_chars=max_comment_chars,
            )

        return "\n".join(lines)

    @classmethod
    def format_manager_status_brief(cls, plan: ProjectPlan, *, max_comment_chars: int = 400) -> str:
        lines: List[str] = []
        lines.append(cls.plan_summary(plan))
        if plan.created_at or plan.updated_at:
            lines.append(f"Создан: {plan.created_at or '—'} | Обновлён: {plan.updated_at or '—'}")
        if plan.current_task_id:
            lines.append(f"Текущая задача: {plan.current_task_id}")
        lines.append("")

        tasks_by_id = {t.id: t for t in (plan.tasks or []) if getattr(t, "id", None)}
        for i, task in enumerate(plan.tasks, start=1):
            dep = f" | зависит от: {', '.join(task.depends_on)}" if task.depends_on else ""
            lines.append(
                f"{i}. {cls._status_emoji(task.status)} {task.title} "
                f"[{task.status}] (попытка {task.attempt}/{task.max_attempts}){dep}"
            )
            cls._append_task_details(
                lines,
                task,
                tasks_by_id=tasks_by_id,
                max_comment_chars=max_comment_chars,
            )
        return "\n".join(lines)

    @classmethod
    def format_plan_notification(cls, plan: ProjectPlan) -> str:
        lines = [f"📋 План: {plan.project_goal}", cls.plan_summary(plan), ""]
        for i, task in enumerate(plan.tasks, start=1):
            dep = f" (depends_on: {', '.join(task.depends_on)})" if task.depends_on else ""
            lines.append(f"{i}. {task.title} [{task.status}]{dep}")
        return "\n".join(lines)


__all__ = ["ManagerUIService"]
