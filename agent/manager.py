from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from config import AppConfig
from session import Session
from utils import strip_ansi

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

# Statuses eligible for retry (normalization after crash/restart).
RETRIABLE_STATUSES = ("pending", "rejected", "in_progress", "in_review")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _debug_ts() -> str:
    """Compact timestamp for debug filenames: 20260207_143012."""
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _debug_write(workdir: str, prefix: str, title: str, body: str) -> None:
    """Write a debug markdown log file to .manager/ inside the workdir."""
    try:
        debug_dir = os.path.join(workdir, ".manager")
        os.makedirs(debug_dir, exist_ok=True)
        ts = _debug_ts()
        fname = f"{prefix}_{ts}.md"
        path = os.path.join(debug_dir, fname)
        content = f"# {title}\n\n**Timestamp:** {_now_iso()}\n\n---\n\n{body}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        _log.debug("debug_write failed: %s", e)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json_object(raw: str) -> str:
    """Extract a JSON object from text that may contain markdown fences or extra text."""
    s = (raw or "").strip()
    if not s:
        return s

    # 1. Try to extract from ```json ... ``` block
    m = _JSON_BLOCK_RE.search(s)
    if m:
        inner = m.group(1).strip()
        if inner:
            return inner

    # 2. Already clean JSON
    if s.startswith("{") and s.endswith("}"):
        return s

    # 3. Find outermost braces
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        return s[i : j + 1]

    return s


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


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


def _truncate_report(text: str, max_chars: int) -> str:
    """Truncate long text preserving beginning and end with a marker in the middle."""
    if not text or len(text) <= max_chars:
        return text or ""
    head_size = max_chars * 3 // 8   # ~3000 for 8000
    tail_size = max_chars * 5 // 8   # ~5000 for 8000
    skipped = len(text) - head_size - tail_size
    return f"{text[:head_size]}\n\n...(обрезано {skipped} символов)...\n\n{text[-tail_size:]}"


# ---------------------------------------------------------------------------
# Public helpers used by bot.py
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ManagerOrchestrator
# ---------------------------------------------------------------------------


class ManagerOrchestrator:
    """
    Manager mode: CLI does development, Agent (Executor) does review.
    All LLM calls here must use defaults.openai_model (see TZ_MANAGER.md).
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        tool_registry = get_tool_registry(config)
        self._executor = Executor(config, tool_registry)

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

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
            report = await self._compose_final_report(plan, workdir=workdir)
            plan.completion_report = report
            save_plan(workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="Готово. Результат ниже.")
                await bot._send_message(context, chat_id=chat_id, text=report)
            # Archive completed/failed plan
            archive_plan(workdir, plan.status)

        return _plan_summary(plan)

    # -----------------------------------------------------------------------
    # Plan creation
    # -----------------------------------------------------------------------

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

    # -----------------------------------------------------------------------
    # Decomposition (two-phase: CLI → direct JSON parse → Agent normalization)
    # -----------------------------------------------------------------------

    async def _decompose(self, session: Session, user_goal: str) -> Optional[ProjectPlan]:
        timeout = int(self._config.defaults.manager_decompose_timeout_sec)
        max_tasks = int(self._config.defaults.manager_max_tasks)
        debug = bool(self._config.defaults.manager_debug_log)
        workdir = session.workdir
        instr = DECOMPOSE_INSTRUCTION.format(user_goal=user_goal, max_tasks=max_tasks)

        if debug:
            _debug_write(workdir, "manager_decompose_prompt", "Decompose Prompt → CLI", instr)

        # === Phase 1: CLI analyzes the project ===
        try:
            cli_text = await asyncio.wait_for(session.run_prompt(instr), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                pass
            _log.warning("decompose: CLI timeout (%ds)", timeout)
            return None
        except Exception as exc:
            _log.warning("decompose: CLI error: %s", exc)
            return None

        cli_text = strip_ansi(cli_text or "")
        _log.info("decompose phase 1 done: CLI output %d chars", len(cli_text))

        if debug:
            _debug_write(workdir, "cli_decompose_response", "CLI Decompose Response", cli_text)

        # === Try direct JSON parse ===
        plan = self._try_parse_plan(cli_text, user_goal, max_tasks)
        if plan:
            _log.info("decompose: direct parse succeeded")
            if debug:
                _debug_write(workdir, "manager_decompose_result", "Decompose Result (direct parse)",
                             json.dumps(asdict(plan), ensure_ascii=False, indent=2))
            return plan

        # === Phase 2: Agent normalization (fallback) ===
        _log.info("decompose: direct parse failed, invoking agent normalization")
        plan = await self._normalize_plan(cli_text, user_goal, max_tasks, workdir=workdir)
        if plan:
            return plan

        # Retry normalization with strict mode
        _log.warning("decompose phase 2: first normalization failed, retrying strict")
        plan = await self._normalize_plan(cli_text, user_goal, max_tasks, strict=True, workdir=workdir)
        if plan:
            return plan

        # Fallback: single task
        _log.warning("decompose: all normalization failed, using single-task fallback")
        return ProjectPlan(
            project_goal=user_goal,
            tasks=[DevTask(
                id="task_1",
                title="Выполнить задачу",
                description=user_goal,
                acceptance_criteria=["Задача выполнена"],
                max_attempts=int(self._config.defaults.manager_max_attempts),
            )],
            status="active",
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )

    def _try_parse_plan(self, raw_text: str, user_goal: str, max_tasks: int) -> Optional[ProjectPlan]:
        """Try to parse CLI output directly as JSON."""
        try:
            json_str = _extract_json_object(raw_text)
            if not json_str:
                return None
            payload = json.loads(json_str)
            if not isinstance(payload, dict):
                return None
            return self._payload_to_plan(payload, user_goal, max_tasks)
        except Exception:
            return None

    async def _normalize_plan(
        self, cli_output: str, user_goal: str, max_tasks: int, strict: bool = False,
        workdir: str = "",
    ) -> Optional[ProjectPlan]:
        """Phase 2: Agent extracts structured plan from free-form CLI text."""
        debug = bool(self._config.defaults.manager_debug_log)
        system = DECOMPOSE_NORMALIZE_SYSTEM
        if strict:
            system += "\n\nПРЕДЫДУЩАЯ ПОПЫТКА НЕ РАСПАРСИЛАСЬ. Верни ТОЛЬКО валидный JSON, ничего больше."
        user_msg = (
            f"Цель проекта: {user_goal}\n\n"
            f"Ответ CLI (анализ проекта и план):\n{cli_output}"
        )
        raw = await chat_completion(self._config, system, user_msg, response_format={"type": "json_object"})
        if debug and workdir:
            suffix = "_strict" if strict else ""
            _debug_write(workdir, f"agent_normalize{suffix}_response",
                         f"Agent Normalize Response{' (strict)' if strict else ''}", raw or "(empty)")
        if not raw:
            return None
        try:
            payload = json.loads(_extract_json_object(raw))
            if not isinstance(payload, dict):
                return None
            plan = self._payload_to_plan(payload, user_goal, max_tasks)
            if plan and debug and workdir:
                _debug_write(workdir, "manager_decompose_result", "Decompose Result (normalized)",
                             json.dumps(asdict(plan), ensure_ascii=False, indent=2))
            return plan
        except Exception as e:
            _log.warning("normalize_plan: JSON parse error: %s", e)
            return None

    def _payload_to_plan(self, payload: dict, user_goal: str, max_tasks: int) -> Optional[ProjectPlan]:
        """Convert a parsed JSON dict to ProjectPlan."""
        tasks_raw = payload.get("tasks") or []
        if not isinstance(tasks_raw, list) or not tasks_raw:
            return None

        # Support both "project_analysis" and "analysis" keys
        analysis_raw = payload.get("project_analysis") or payload.get("analysis")
        analysis_obj: Optional[ProjectAnalysis] = None
        if isinstance(analysis_raw, dict):
            analysis_obj = ProjectAnalysis(
                current_state=str(analysis_raw.get("current_state") or ""),
                already_done=list(analysis_raw.get("already_done") or []),
                remaining_work=list(analysis_raw.get("remaining_work") or []),
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

        return ProjectPlan(
            project_goal=str(payload.get("project_goal") or user_goal),
            tasks=tasks,
            analysis=analysis_obj,
            status="active",
            created_at=_now_iso(),
            updated_at=_now_iso(),
            current_task_id=None,
        )

    # -----------------------------------------------------------------------
    # Plan notification
    # -----------------------------------------------------------------------

    async def _notify_plan(self, session: Session, plan: ProjectPlan, bot, context, dest: dict) -> None:
        chat_id = dest.get("chat_id")
        if chat_id is None:
            return
        lines = [f"📋 План: {plan.project_goal}", _plan_summary(plan), ""]
        for i, t in enumerate(plan.tasks, start=1):
            dep = f" (depends_on: {', '.join(t.depends_on)})" if t.depends_on else ""
            lines.append(f"{i}. {t.title} [{t.status}]{dep}")
        await bot._send_message(context, chat_id=chat_id, text="\n".join(lines))

    # -----------------------------------------------------------------------
    # Next ready task (with RETRIABLE_STATUSES normalization per TZ)
    # -----------------------------------------------------------------------

    def _next_ready_task(self, plan: ProjectPlan) -> Optional[DevTask]:
        """Select the next task ready for execution, normalizing stale statuses after restart."""
        tasks_by_id = {t.id: t for t in plan.tasks}
        has_pending = False
        all_blocked_or_done = True

        for t in plan.tasks:
            # Normalize interrupted / stale statuses
            if t.status in ("in_progress", "in_review"):
                # Interrupted during execution: reset to pending and decrement attempt
                # so that the loop increment brings it back to the same attempt number.
                t.status = "pending"
                if t.attempt > 0:
                    t.attempt -= 1
            elif t.status == "rejected":
                # Interrupted after rejection but before pending: just reset to pending.
                # Attempt count remains (so loop increments to next attempt).
                if t.attempt >= t.max_attempts:
                    t.status = "failed"
                else:
                    t.status = "pending"

            # Check for cascade blocking: if any dependency failed → block
            deps = [tasks_by_id[dep_id] for dep_id in t.depends_on if dep_id in tasks_by_id]
            if any(d.status == "failed" for d in deps):
                if t.status not in ("approved", "failed"):
                    t.status = "blocked"
                continue

            if t.status == "blocked":
                continue

            if t.status in ("approved", "failed"):
                continue

            if t.status in RETRIABLE_STATUSES:
                has_pending = True

            # All deps must be approved
            if all(d.status == "approved" for d in deps):
                all_blocked_or_done = False
                return t

        # No ready task found.
        # If there are still pending tasks but they're all blocked — deadlock.
        if has_pending and all_blocked_or_done:
            _log.warning("_next_ready_task: deadlock or cascade block detected")
        return None

    def _is_plan_blocked(self, plan: ProjectPlan) -> bool:
        """True if all remaining non-approved tasks are blocked/failed (no more progress possible)."""
        for t in plan.tasks:
            if t.status in ("pending", "rejected", "in_progress", "in_review"):
                return False
        return True

    # -----------------------------------------------------------------------
    # Main execution loop
    # -----------------------------------------------------------------------

    async def _run_loop(self, session: Session, plan: ProjectPlan, bot, context, dest: dict) -> None:
        chat_id = dest.get("chat_id")
        max_iterations = int(self._config.defaults.manager_max_tasks) * int(self._config.defaults.manager_max_attempts)
        iteration = 0

        while True:
            if plan.status in ("paused", "completed", "failed"):
                break

            iteration += 1
            if iteration > max_iterations:
                _log.warning("_run_loop: max iterations (%d) exceeded", max_iterations)
                plan.status = "failed"
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    await bot._send_message(context, chat_id=chat_id, text=f"⛔ Превышен лимит итераций ({max_iterations}). План остановлен.")
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
                    if chat_id is not None:
                        await bot._send_message(context, chat_id=chat_id, text="⛔ План остановлен: невозможно продолжить (задачи заблокированы).")
                save_plan(session.workdir, plan)
                break

            plan.current_task_id = task.id
            task.status = "in_progress"
            task.attempt += 1
            task.started_at = task.started_at or _now_iso()
            save_plan(session.workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text=f"🔧 Разработка: {task.title} (попытка {task.attempt}/{task.max_attempts})")

            # === DEVELOPMENT ===
            dev_ok, dev_report = await self._delegate_develop(session, plan, task)
            task.dev_report = dev_report
            save_plan(session.workdir, plan)
            if not dev_ok:
                task.status = "failed"
                task.completed_at = _now_iso()
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    await bot._send_message(context, chat_id=chat_id, text=f"❌ Провал: {task.title} — {dev_report[:200]}")
                # Check if plan is now blocked
                if self._is_plan_blocked(plan):
                    plan.status = "failed"
                    save_plan(session.workdir, plan)
                    if chat_id is not None:
                        await bot._send_message(context, chat_id=chat_id, text="⛔ План остановлен: критическая задача провалена.")
                    break
                continue

            # === REVIEW ===
            task.status = "in_review"
            save_plan(session.workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text=f"🔍 Ревью: {task.title}")

            review = await self._delegate_review(session, plan, task, bot, context, dest)
            task.review_verdict = "approved" if review.approved else "rejected"
            task.review_comments = review.comments
            save_plan(session.workdir, plan)

            # === ARBITER DECISION ===
            verdict, reasons = await self._make_decision(task, review, workdir=session.workdir)
            if verdict == "approved":
                task.status = "approved"
                task.completed_at = _now_iso()
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    await bot._send_message(context, chat_id=chat_id, text=f"✅ Принято: {task.title}")
                continue

            # rejected
            task.rejection_history.append({
                "attempt": task.attempt,
                "comments": review.comments,
                "timestamp": _now_iso(),
            })
            if task.attempt >= task.max_attempts:
                task.status = "failed"
                task.completed_at = _now_iso()
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    await bot._send_message(context, chat_id=chat_id, text=f"❌ Провал: {task.title} — исчерпаны попытки ({task.max_attempts})")
                # Check if plan is now blocked
                if self._is_plan_blocked(plan):
                    plan.status = "failed"
                    save_plan(session.workdir, plan)
                    if chat_id is not None:
                        await bot._send_message(context, chat_id=chat_id, text="⛔ План остановлен: критическая задача провалена.")
                    break
            else:
                task.status = "pending"  # will be retried
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    reasons_txt = ", ".join(reasons) if reasons else "см. замечания"
                    await bot._send_message(context, chat_id=chat_id, text=f"🔄 Доработка: {task.title} (попытка {task.attempt + 1})\nПричины: {reasons_txt}")

    # -----------------------------------------------------------------------
    # Delegate development to CLI
    # -----------------------------------------------------------------------

    async def _delegate_develop(self, session: Session, plan: ProjectPlan, task: DevTask) -> Tuple[bool, str]:
        timeout = int(self._config.defaults.manager_dev_timeout_sec)
        max_chars = int(self._config.defaults.manager_dev_report_max_chars)
        debug = bool(self._config.defaults.manager_debug_log)

        # Build context
        ctx = ""
        if plan.analysis and plan.analysis.current_state:
            ctx = plan.analysis.current_state

        already_done = ""
        if plan.analysis and plan.analysis.already_done:
            already_done = ", ".join(plan.analysis.already_done)

        completed_tasks = [t for t in plan.tasks if t.status == "approved"]
        completed_summary = ", ".join(t.title for t in completed_tasks) if completed_tasks else "(нет)"

        # Conditional rejection block
        rejection_block = ""
        if task.attempt > 1 and task.review_comments:
            rejection_block = (
                f"### ⚠️ Замечания ревьюера (исправь обязательно):\n"
                f"{task.review_comments}\n\n"
                f"Это попытка {task.attempt} из {task.max_attempts}. Исправь все замечания."
            )

        instr = DEV_INSTRUCTION_TEMPLATE.format(
            task_title=task.title,
            task_description=task.description,
            task_acceptance=_task_acceptance(task),
            rejection_block=rejection_block,
            project_context=ctx or "(контекст не задан)",
            already_done=already_done or "(нет данных)",
            completed_tasks_summary=completed_summary,
        )
        if debug:
            _debug_write(session.workdir, f"manager_dev_prompt_{task.id}",
                         f"Dev Prompt → CLI [{task.id}] (attempt {task.attempt})", instr)
        try:
            out = await asyncio.wait_for(session.run_prompt(instr), timeout=timeout)
            out = strip_ansi(out or "")
            if debug:
                _debug_write(session.workdir, f"cli_dev_response_{task.id}",
                             f"CLI Dev Response [{task.id}] (attempt {task.attempt})", out)
            out = _truncate_report(out, max_chars)
            return True, out
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                pass
            return False, "TIMEOUT: превышено время выполнения"
        except Exception as e:
            return False, f"ERROR: {e}"

    # -----------------------------------------------------------------------
    # Delegate review to Agent (Executor with reviewer profile)
    # -----------------------------------------------------------------------

    async def _delegate_review(self, session: Session, plan: ProjectPlan, task: DevTask, bot, context, dest: dict) -> ReviewResult:
        debug = bool(self._config.defaults.manager_debug_log)
        tool_registry = get_tool_registry(self._config)
        profile = build_reviewer_profile(self._config, tool_registry)
        dev_report = task.dev_report or ""
        instr = REVIEW_INSTRUCTION_TEMPLATE.format(
            task_title=task.title,
            task_description=task.description,
            task_acceptance=_task_acceptance(task),
            dev_report=dev_report,
        )
        if debug:
            _debug_write(session.workdir, f"manager_review_prompt_{task.id}",
                         f"Review Prompt → Agent [{task.id}]", instr)
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

        if debug:
            _debug_write(session.workdir, f"agent_review_response_{task.id}",
                         f"Agent Review Response [{task.id}]", text)

        # Two-phase review result parsing (same as decompose)
        # 1. Try direct parse
        review = self._try_parse_review(text)
        if review:
            if debug:
                _debug_write(session.workdir, f"manager_review_result_{task.id}",
                             f"Review Result [{task.id}] (direct parse)",
                             json.dumps(asdict(review), ensure_ascii=False, indent=2))
            return review

        # 2. Agent normalization
        normalized = await chat_completion(self._config, REVIEW_NORMALIZE_SYSTEM, text, response_format={"type": "json_object"})
        if debug:
            _debug_write(session.workdir, f"agent_review_normalize_{task.id}",
                         f"Agent Review Normalize Response [{task.id}]", normalized or "(empty)")
        review = self._try_parse_review(normalized or "")
        if review:
            if debug:
                _debug_write(session.workdir, f"manager_review_result_{task.id}",
                             f"Review Result [{task.id}] (normalized)",
                             json.dumps(asdict(review), ensure_ascii=False, indent=2))
            return review

        # 3. Fallback
        return ReviewResult(
            approved=False,
            summary="Не удалось определить вердикт",
            comments="Не удалось распарсить ответ ревьюера, требуется доработка.",
        )

    def _try_parse_review(self, text: str) -> Optional[ReviewResult]:
        """Try to parse review text as JSON ReviewResult."""
        try:
            payload = json.loads(_extract_json_object(text))
            if not isinstance(payload, dict):
                return None
            if "approved" not in payload:
                return None
            return ReviewResult(
                approved=bool(payload.get("approved")),
                summary=str(payload.get("summary") or ""),
                comments=str(payload.get("comments") or ""),
                tests_passed=payload.get("tests_passed") if isinstance(payload.get("tests_passed"), bool) else None,
                files_reviewed=list(payload.get("files_reviewed") or []),
            )
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Arbiter decision (always called; decides by acceptance criteria)
    # -----------------------------------------------------------------------

    async def _make_decision(self, task: DevTask, review: ReviewResult, workdir: str = "") -> Tuple[str, List[str]]:
        debug = bool(self._config.defaults.manager_debug_log)
        user_msg = (
            f"### Задача: {task.title}\n\n"
            f"### Описание:\n{task.description}\n\n"
            f"### Критерии приёмки:\n{_task_acceptance(task)}\n\n"
            f"### Отчёт разработчика:\n{task.dev_report or '(пусто)'}\n\n"
            f"### Вердикт ревьюера:\n{json.dumps(asdict(review), ensure_ascii=False)}"
        )
        if debug and workdir:
            _debug_write(workdir, f"manager_decision_prompt_{task.id}",
                         f"Decision Prompt → Arbiter [{task.id}]", user_msg)
        raw = await chat_completion(self._config, DECISION_SYSTEM, user_msg, response_format={"type": "json_object"})
        if debug and workdir:
            _debug_write(workdir, f"agent_decision_response_{task.id}",
                         f"Arbiter Decision Response [{task.id}]", raw or "(empty)")
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

    # -----------------------------------------------------------------------
    # Final report
    # -----------------------------------------------------------------------

    async def _compose_final_report(self, plan: ProjectPlan, workdir: str = "") -> str:
        debug = bool(self._config.defaults.manager_debug_log)
        payload = json.dumps(asdict(plan), ensure_ascii=False, indent=2)
        if debug and workdir:
            _debug_write(workdir, "manager_final_report_prompt", "Final Report Prompt → Agent", payload)
        out = await chat_completion(self._config, FINAL_REPORT_SYSTEM, payload)
        if debug and workdir:
            _debug_write(workdir, "agent_final_report_response", "Agent Final Report Response", out or "(empty)")
        return out or "Отчёт недоступен (пустой ответ модели)."

    # -----------------------------------------------------------------------
    # External controls (UI commands)
    # -----------------------------------------------------------------------

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
