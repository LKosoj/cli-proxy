from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from config import AppConfig
from session import Session

from .contracts import DevTask, ProjectAnalysis, ProjectPlan, ReviewResult, ExecutorRequest
from .executor import Executor
from .manager_prompts import (
    DECOMPOSE_INSTRUCTION,
    DECOMPOSE_NORMALIZE_SYSTEM,
    DEV_INSTRUCTION_TEMPLATE,
    REVIEW_INSTRUCTION_TEMPLATE,
    REVIEW_NORMALIZE_SYSTEM,
    DECISION_SYSTEM,
    FINAL_REPORT_SYSTEM,
)
from .manager_store import archive_plan, delete_plan, load_plan, save_plan
from .openai_client import chat_completion
from .profiles import build_reviewer_profile
from .tooling.registry import get_tool_registry

_log = logging.getLogger(__name__)

MANAGER_CONTINUE_TOKEN = "__MANAGER_CONTINUE__"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _extract_json_object(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return s
    if s.startswith("```"):
        s = s.strip("`")
    if s.startswith("{") and s.endswith("}"):
        return s
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        return s[i : j + 1]
    return s


def _format_acceptance(items: List[str]) -> str:
    if not items:
        return "- (нет критериев)"
    return "\n".join([f"- {x}" for x in items])


def _task_acceptance(task: DevTask) -> str:
    return _format_acceptance(task.acceptance_criteria)


def _plan_summary(plan: ProjectPlan) -> str:
    done = sum(1 for t in plan.tasks if t.status == "approved")
    total = len(plan.tasks)
    return f"План: {done}/{total} задач выполнено. Статус: {plan.status}."


def needs_resume_choice(plan: Optional[ProjectPlan], *, auto_resume: bool, user_text: str) -> bool:
    """
    True when we have an active plan but auto-resume is disabled and the user sent a "new goal" text.
    Used by the bot to ask "continue or start new?" before running long manager loop.
    """
    if not plan or plan.status != "active":
        return False
    if auto_resume:
        return False
    txt = (user_text or "").strip()
    if not txt:
        return False
    if txt == MANAGER_CONTINUE_TOKEN:
        return False
    return True


def format_manager_status(plan: ProjectPlan, *, max_comment_chars: int = 400) -> str:
    def _emoji(status: str) -> str:
        m = {
            "approved": "✅",
            "in_review": "🔄",
            "in_progress": "🔧",
            "pending": "⏳",
            "rejected": "❌",
            "failed": "❌",
            "blocked": "⛔",
            "paused": "💤",
        }
        return m.get(status, "•")

    lines: List[str] = []
    lines.append(f"📋 План: «{plan.project_goal}»")
    lines.append(_plan_summary(plan))
    if plan.created_at or plan.updated_at:
        lines.append(f"Создан: {plan.created_at or '—'} | Обновлён: {plan.updated_at or '—'}")
    if plan.current_task_id:
        lines.append(f"Текущая задача: {plan.current_task_id}")
    lines.append("")

    for i, t in enumerate(plan.tasks, start=1):
        dep = f" | зависит от: {', '.join(t.depends_on)}" if t.depends_on else ""
        lines.append(
            f"{i}. {_emoji(t.status)} {t.title} [{t.status}] (попытка {t.attempt}/{t.max_attempts}){dep}"
        )
        if t.status in ("rejected", "failed") and t.review_comments:
            comments = t.review_comments.strip()
            if len(comments) > max_comment_chars:
                comments = comments[:max_comment_chars] + "…"
            lines.append(f"   └ Замечания: {comments}")

    return "\n".join(lines)


class ManagerOrchestrator:
    """
    Manager mode: CLI does development, Agent (Executor) does review.
    All LLM calls here must use defaults.openai_model (see TZ_MANAGER.md).
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        tool_registry = get_tool_registry(config)
        self._executor = Executor(config, tool_registry)

    async def run(self, session: Session, user_text: str, bot, context, dest: dict) -> str:
        chat_id = dest.get("chat_id")
        workdir = session.workdir
        plan = load_plan(workdir)
        txt = (user_text or "").strip()
        if plan and plan.status == "active" and (self._config.defaults.manager_auto_resume or txt == MANAGER_CONTINUE_TOKEN):
            # continue silently
            pass
        else:
            plan = await self._start_new_plan(session, user_text, bot, context, dest)

        if not plan:
            return "manager: no plan"

        await self._notify_plan(session, plan, bot, context, dest)
        await self._run_loop(session, plan, bot, context, dest)

        # Final report
        if plan.status in ("completed", "failed"):
            report = await self._compose_final_report(plan)
            plan.completion_report = report
            save_plan(workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="Готово. Результат ниже.")
                await bot._send_message(context, chat_id=chat_id, text=report)
        return _plan_summary(plan)

    async def _start_new_plan(self, session: Session, user_text: str, bot, context, dest: dict) -> Optional[ProjectPlan]:
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            await bot._send_message(context, chat_id=chat_id, text="🏗 Manager: декомпозиция задачи и анализ проекта...")
        plan = await self._decompose(session, user_text)
        if not plan:
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="Не удалось построить план Manager.")
            return None
        save_plan(session.workdir, plan)
        return plan

    async def _decompose(self, session: Session, user_goal: str) -> Optional[ProjectPlan]:
        timeout = int(self._config.defaults.manager_decompose_timeout_sec)
        max_tasks = int(self._config.defaults.manager_max_tasks)
        instr = DECOMPOSE_INSTRUCTION.format(user_goal=user_goal, max_tasks=max_tasks)
        try:
            cli_text = await asyncio.wait_for(session.run_prompt(instr), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                pass
            return None
        except Exception:
            return None

        normalized = await chat_completion(self._config, DECOMPOSE_NORMALIZE_SYSTEM, cli_text)
        if not normalized:
            return None
        try:
            payload = json.loads(_extract_json_object(normalized))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        tasks_raw = payload.get("tasks") or []
        if not isinstance(tasks_raw, list) or not tasks_raw:
            return None

        analysis = payload.get("analysis")
        analysis_obj: Optional[ProjectAnalysis] = None
        if isinstance(analysis, dict):
            analysis_obj = ProjectAnalysis(
                current_state=str(analysis.get("current_state") or ""),
                already_done=list(analysis.get("already_done") or []),
                remaining_work=list(analysis.get("remaining_work") or []),
            )

        tasks: List[DevTask] = []
        for idx, t in enumerate(tasks_raw[:max_tasks], start=1):
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or f"task_{idx}").strip()
            tasks.append(
                DevTask(
                    id=tid,
                    title=str(t.get("title") or f"Задача {idx}").strip(),
                    description=str(t.get("description") or "").strip(),
                    acceptance_criteria=list(t.get("acceptance_criteria") or []),
                    depends_on=[str(x) for x in (t.get("depends_on") or []) if x],
                    max_attempts=int(self._config.defaults.manager_max_attempts),
                )
            )
        if not tasks:
            return None
        plan = ProjectPlan(
            project_goal=str(payload.get("project_goal") or user_goal),
            tasks=tasks,
            analysis=analysis_obj,
            status="active",
            created_at=_now_iso(),
            updated_at=_now_iso(),
            current_task_id=None,
        )
        return plan

    async def _notify_plan(self, session: Session, plan: ProjectPlan, bot, context, dest: dict) -> None:
        chat_id = dest.get("chat_id")
        if chat_id is None:
            return
        lines = [f"📋 План: {plan.project_goal}", _plan_summary(plan), ""]
        for i, t in enumerate(plan.tasks, start=1):
            dep = f" (depends_on: {', '.join(t.depends_on)})" if t.depends_on else ""
            lines.append(f"{i}. {t.title} [{t.status}]{dep}")
        await bot._send_message(context, chat_id=chat_id, text="\n".join(lines))

    def _next_ready_task(self, plan: ProjectPlan) -> Optional[DevTask]:
        tasks_by_id = {t.id: t for t in plan.tasks}
        for t in plan.tasks:
            if t.status not in ("pending", "rejected"):
                continue
            if t.attempt >= t.max_attempts:
                continue
            # deps must be approved
            if any(tasks_by_id.get(dep) and tasks_by_id[dep].status != "approved" for dep in t.depends_on):
                continue
            return t
        return None

    async def _run_loop(self, session: Session, plan: ProjectPlan, bot, context, dest: dict) -> None:
        chat_id = dest.get("chat_id")
        while True:
            if plan.status in ("paused", "completed", "failed"):
                break
            task = self._next_ready_task(plan)
            if not task:
                # No ready tasks: either all done or blocked.
                if all(t.status == "approved" for t in plan.tasks):
                    plan.status = "completed"
                else:
                    # Mark remaining as blocked
                    for t in plan.tasks:
                        if t.status in ("pending", "rejected"):
                            t.status = "blocked"
                    plan.status = "failed"
                save_plan(session.workdir, plan)
                break

            plan.current_task_id = task.id
            task.status = "in_progress"
            task.attempt += 1
            task.started_at = task.started_at or _now_iso()
            save_plan(session.workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text=f"🔧 Разработка: {task.title} (попытка {task.attempt}/{task.max_attempts})")

            dev_ok, dev_report = await self._delegate_develop(session, plan, task)
            task.dev_report = dev_report
            save_plan(session.workdir, plan)
            if not dev_ok:
                task.status = "failed"
                task.completed_at = _now_iso()
                save_plan(session.workdir, plan)
                continue

            task.status = "in_review"
            save_plan(session.workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text=f"🕵️ Ревью: {task.title}")

            review = await self._delegate_review(session, plan, task, bot, context, dest)
            task.review_verdict = "approved" if review.approved else "rejected"
            task.review_comments = review.comments
            save_plan(session.workdir, plan)

            verdict, reasons = await self._make_decision(task, review)
            if verdict == "approved":
                task.status = "approved"
                task.completed_at = _now_iso()
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    await bot._send_message(context, chat_id=chat_id, text=f"✅ Принято: {task.title}")
                continue

            # rejected
            task.status = "rejected"
            task.rejection_history.append({"attempt": task.attempt, "comments": review.comments, "timestamp": _now_iso()})
            save_plan(session.workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text=f"❌ Отклонено: {task.title}\nПричины: {', '.join(reasons) if reasons else 'см. замечания'}")
            if task.attempt >= task.max_attempts:
                task.status = "failed"
                task.completed_at = _now_iso()
                save_plan(session.workdir, plan)

    async def _delegate_develop(self, session: Session, plan: ProjectPlan, task: DevTask) -> Tuple[bool, str]:
        timeout = int(self._config.defaults.manager_dev_timeout_sec)
        reviewer_comments = task.review_comments or "(нет)"
        ctx_parts = []
        if plan.analysis and plan.analysis.current_state:
            ctx_parts.append(plan.analysis.current_state)
        ctx = "\n".join(ctx_parts) if ctx_parts else "(контекст не задан)"
        instr = DEV_INSTRUCTION_TEMPLATE.format(
            task_title=task.title,
            task_description=task.description,
            task_acceptance=_task_acceptance(task),
            project_context=ctx,
            reviewer_comments=reviewer_comments,
        )
        try:
            out = await asyncio.wait_for(session.run_prompt(instr), timeout=timeout)
            # Truncate for later review prompt
            max_chars = int(self._config.defaults.manager_dev_report_max_chars)
            if len(out) > max_chars:
                out = out[-max_chars:]
            return True, out
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                pass
            return False, "⚠️ таймаут разработки"
        except Exception as e:
            return False, f"⚠️ ошибка разработки: {e}"

    async def _delegate_review(self, session: Session, plan: ProjectPlan, task: DevTask, bot, context, dest: dict) -> ReviewResult:
        tool_registry = get_tool_registry(self._config)
        profile = build_reviewer_profile(self._config, tool_registry)
        dev_report = task.dev_report or ""
        instr = REVIEW_INSTRUCTION_TEMPLATE.format(
            task_title=task.title,
            task_description=task.description,
            task_acceptance=_task_acceptance(task),
            dev_report=dev_report,
        )
        req = ExecutorRequest(
            task_id=f"review:{task.id}",
            goal=instr,
            context=f"workdir={session.workdir}",
            inputs={},
            allowed_tools=profile.allowed_tools,
            deadline_ms=profile.timeout_ms,
        )
        try:
            resp = await self._executor.run(session, req, bot, context, dest, profile)
            text = (resp.outputs[0].get("content") if resp.outputs else "") or resp.summary or ""
        except Exception as e:
            return ReviewResult(approved=False, summary="Ошибка ревью", comments=str(e))

        normalized = await chat_completion(self._config, REVIEW_NORMALIZE_SYSTEM, text)
        try:
            payload = json.loads(_extract_json_object(normalized or text))
        except Exception:
            payload = {}
        approved = bool(payload.get("approved")) if isinstance(payload, dict) else False
        return ReviewResult(
            approved=approved,
            summary=str(payload.get("summary") or ""),
            comments=str(payload.get("comments") or ""),
            tests_passed=payload.get("tests_passed") if isinstance(payload, dict) else None,
            files_reviewed=list(payload.get("files_reviewed") or []) if isinstance(payload, dict) else [],
        )

    async def _make_decision(self, task: DevTask, review: ReviewResult) -> Tuple[str, List[str]]:
        raw = await chat_completion(
            self._config,
            DECISION_SYSTEM,
            f"dev_report:\n{task.dev_report or ''}\n\nreview:\n{asdict(review)}",
        )
        verdict = "approved" if review.approved else "rejected"
        reasons: List[str] = []
        if raw:
            try:
                payload = json.loads(_extract_json_object(raw))
                if isinstance(payload, dict):
                    verdict = str(payload.get("verdict") or verdict)
                    rs = payload.get("reasons") or []
                    if isinstance(rs, list):
                        reasons = [str(x) for x in rs if x]
            except Exception:
                pass
        if verdict not in ("approved", "rejected"):
            verdict = "approved" if review.approved else "rejected"
        return verdict, reasons

    async def _compose_final_report(self, plan: ProjectPlan) -> str:
        payload = json.dumps(asdict(plan), ensure_ascii=False, indent=2)
        out = await chat_completion(self._config, FINAL_REPORT_SYSTEM, payload)
        return out or "Отчёт недоступен (пустой ответ модели)."

    # External controls (UI commands)
    def pause(self, session: Session) -> None:
        plan = load_plan(session.workdir)
        if not plan:
            return
        plan.status = "paused"
        save_plan(session.workdir, plan)

    def reset(self, session: Session) -> None:
        plan = load_plan(session.workdir)
        if plan:
            archive_plan(session.workdir, plan.status)
        delete_plan(session.workdir)
