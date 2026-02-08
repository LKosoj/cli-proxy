from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict
from typing import List, Optional, Tuple, Dict

from config import AppConfig
from session import Session
from utils import strip_ansi

from .contracts import DevTask, ProjectAnalysis, ProjectPlan, ReviewResult, ExecutorRequest
from .executor import Executor
from .manager_prompts import (
    DECOMPOSE_INSTRUCTION,
    DECOMPOSE_NORMALIZE_SYSTEM,
    PLAN_VALIDATION_SYSTEM,
    PLAN_FIX_INSTRUCTION,
    DEV_INSTRUCTION_TEMPLATE,
    DEV_REWORK_INSTRUCTION_TEMPLATE,
    REVIEW_INSTRUCTION_TEMPLATE,
    REVIEW_NORMALIZE_SYSTEM,
    DECISION_SYSTEM,
    COMMIT_MESSAGE_SYSTEM,
    PLAN_RECONCILE_SYSTEM,
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
        fname = f"{ts}_{prefix}.md"
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
        return s[i: j + 1]

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


def _task_progress(plan: ProjectPlan, task: DevTask) -> Tuple[int, int]:
    """Return 1-based task position in plan and total tasks count."""
    total = len(plan.tasks)
    for idx, candidate in enumerate(plan.tasks, start=1):
        if candidate is task or candidate.id == task.id:
            return idx, total
    return 0, total


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


def needs_failed_resume_choice(plan: Optional[ProjectPlan], *, auto_resume: bool, user_text: str) -> bool:
    """
    True when we have a failed plan with retryable tasks and auto-resume is disabled.
    Used by the bot to ask whether to continue a stopped plan or start a new one.
    """
    if not plan or plan.status != "failed":
        return False
    if auto_resume:
        return False
    if not ManagerOrchestrator._can_resume_failed(plan):
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
        elif (
            plan
            and plan.status == "failed"
            and self._can_resume_failed(plan)
            and (self._config.defaults.manager_auto_resume or txt == MANAGER_CONTINUE_TOKEN)
        ):
            # Plan was failed (timeout / partial) but has retryable tasks — resume it.
            plan.status = "active"
            plan.updated_at = _now_iso()
            save_plan(workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="🔄 Возобновляю ранее остановленный план...")
        else:
            plan = await self._start_new_plan(session, user_text, bot, context, dest)

        if not plan:
            return "manager: no plan"

        await self._notify_plan(session, plan, bot, context, dest)
        await self._run_loop(session, plan, bot, context, dest)

        # Final report
        if plan.status == "completed":
            report = await self._compose_final_report(plan, workdir=workdir)
            plan.completion_report = report
            save_plan(workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="✅ Готово. Результат ниже.")
                await bot._send_message(context, chat_id=chat_id, text=report)
            archive_plan(workdir, plan.status)
        elif plan.status == "failed":
            report = await self._compose_final_report(plan, workdir=workdir)
            plan.completion_report = report
            save_plan(workdir, plan)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="❌ План провален. Результат ниже.")
                await bot._send_message(context, chat_id=chat_id, text=report)
                # Ask user: retry or archive?
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔄 Повторить", callback_data="manager_failed:retry"),
                        InlineKeyboardButton("📦 В архив", callback_data="manager_failed:archive"),
                    ],
                ])
                await bot._send_message(context, chat_id=chat_id,
                                        text="Что сделать с проваленным планом?",
                                        reply_markup=keyboard)

        return _plan_summary(plan)

    # -----------------------------------------------------------------------
    # Plan creation
    # -----------------------------------------------------------------------

    async def _start_new_plan(self, session: Session, user_text: str, bot, context, dest: dict) -> Optional[ProjectPlan]:
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            await bot._send_message(context, chat_id=chat_id, text="🏗 Manager: декомпозиция задачи и анализ проекта...")
        plan = await self._decompose(session, user_text, bot=bot, context=context, dest=dest)
        if not plan:
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id, text="Не удалось построить план Manager.")
            return None
        save_plan(session.workdir, plan)
        return plan

    # -----------------------------------------------------------------------
    # Decomposition (two-phase: CLI → direct JSON parse → Agent normalization)
    # -----------------------------------------------------------------------

    async def _decompose(
        self, session: Session, user_goal: str, bot=None,
        context=None, dest: Optional[dict] = None,
    ) -> Optional[ProjectPlan]:
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
        else:
            # === Phase 2: Agent normalization (fallback) ===
            _log.info("decompose: direct parse failed, invoking agent normalization")
            plan = await self._normalize_plan(cli_text, user_goal, max_tasks, workdir=workdir)
            if not plan:
                # Retry normalization with strict mode
                _log.warning("decompose phase 2: first normalization failed, retrying strict")
                plan = await self._normalize_plan(cli_text, user_goal, max_tasks, strict=True, workdir=workdir)

        if not plan:
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

        # === Phase 3: Validate plan (up to N correction attempts) ===
        chat_id = (dest or {}).get("chat_id")
        max_fix_attempts = int(self._config.defaults.manager_max_attempts) + 4
        for fix_attempt in range(1, max_fix_attempts + 1):
            if chat_id is not None and bot is not None:
                await bot._send_message(context, chat_id=chat_id,
                                        text=f"🔎 Валидация плана (проверка {fix_attempt}/{max_fix_attempts})...")
            issues = await self._validate_plan(plan, workdir)
            if not issues:
                _log.info("decompose: plan validation passed (attempt %d)", fix_attempt)
                if chat_id is not None and bot is not None:
                    await bot._send_message(context, chat_id=chat_id, text="✅ План прошёл валидацию")
                break

            _log.warning("decompose: plan validation failed (attempt %d/%d): %s",
                         fix_attempt, max_fix_attempts, issues)
            if debug:
                _debug_write(workdir, f"manager_validate_issues_{fix_attempt}",
                             f"Plan Validation Issues (attempt {fix_attempt}/{max_fix_attempts})",
                             "\n".join(f"- {x}" for x in issues))

            issues_short = "; ".join(issues[:3])
            if len(issues) > 3:
                issues_short += f" (+ещё {len(issues) - 3})"

            if fix_attempt >= max_fix_attempts:
                _log.warning("decompose: max fix attempts reached, using plan as-is")
                if chat_id is not None and bot is not None:
                    await bot._send_message(context, chat_id=chat_id,
                                            text=f"⚠️ План содержит замечания, но исчерпаны попытки корректировки: {issues_short}")
                break

            if chat_id is not None and bot is not None:
                await bot._send_message(context, chat_id=chat_id,
                                        text=(
                                            f"⚠️ Проблемы в плане: {issues_short}\n"
                                            f"🔄 Отправляю CLI на корректировку ({fix_attempt}/{max_fix_attempts})..."
                                        ))

            fixed_plan = await self._fix_plan_via_cli(session, plan, issues, user_goal, timeout, workdir)
            if fixed_plan:
                plan = fixed_plan
                _log.info("decompose: plan corrected (attempt %d), re-validating...", fix_attempt)
            else:
                _log.warning("decompose: CLI fix failed (attempt %d), using current plan", fix_attempt)
                if chat_id is not None and bot is not None:
                    await bot._send_message(context, chat_id=chat_id,
                                            text="⚠️ CLI не смог исправить план, используем текущий")
                break

        return plan

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

    async def _fix_plan_via_cli(
        self, session: Session, plan: ProjectPlan, issues: List[str],
        user_goal: str, timeout: int, workdir: str,
    ) -> Optional[ProjectPlan]:
        """Send the plan back to CLI for correction based on validation issues."""
        debug = bool(self._config.defaults.manager_debug_log)
        max_tasks = int(self._config.defaults.manager_max_tasks)
        plan_json = json.dumps(asdict(plan), ensure_ascii=False, indent=2)
        issues_text = "\n".join(f"- {x}" for x in issues)
        instr = PLAN_FIX_INSTRUCTION.format(
            issues=issues_text, plan_json=plan_json, user_goal=user_goal,
        )
        if debug:
            _debug_write(workdir, "manager_fix_prompt", "Plan Fix Prompt → CLI", instr)

        try:
            cli_text = await asyncio.wait_for(session.run_prompt(instr), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                session.interrupt()
            except Exception:
                pass
            _log.warning("fix_plan: CLI timeout")
            return None
        except Exception as exc:
            _log.warning("fix_plan: CLI error: %s", exc)
            return None

        cli_text = strip_ansi(cli_text or "")
        if debug:
            _debug_write(workdir, "cli_fix_response", "CLI Fix Response", cli_text)

        # Try to parse corrected plan
        fixed = self._try_parse_plan(cli_text, user_goal, max_tasks)
        if fixed:
            if debug:
                _debug_write(workdir, "manager_fix_result", "Fixed Plan (direct parse)",
                             json.dumps(asdict(fixed), ensure_ascii=False, indent=2))
            return fixed

        # Agent normalization fallback
        fixed = await self._normalize_plan(cli_text, user_goal, max_tasks, workdir=workdir)
        if fixed and debug:
            _debug_write(workdir, "manager_fix_result", "Fixed Plan (normalized)",
                         json.dumps(asdict(fixed), ensure_ascii=False, indent=2))
        return fixed

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
    # Plan validation
    # -----------------------------------------------------------------------

    @staticmethod
    def _validate_plan_structure(plan: ProjectPlan) -> List[str]:
        """Check plan for structural issues. Returns list of problems (empty = OK)."""
        issues: List[str] = []
        task_ids = set()
        id_list = [t.id for t in plan.tasks]

        for t in plan.tasks:
            # Duplicate IDs
            if t.id in task_ids:
                issues.append(f"Дублирующийся ID задачи: '{t.id}'")
            task_ids.add(t.id)

            # Empty fields
            if not t.title.strip():
                issues.append(f"Задача '{t.id}': пустой title")
            if not t.description.strip():
                issues.append(f"Задача '{t.id}': пустой description")
            if not t.acceptance_criteria:
                issues.append(f"Задача '{t.id}': нет acceptance_criteria")

            # Self-dependency
            if t.id in t.depends_on:
                issues.append(f"Задача '{t.id}' зависит от самой себя")

            # Missing dependencies
            for dep in t.depends_on:
                if dep not in id_list:
                    issues.append(f"Задача '{t.id}' зависит от несуществующей '{dep}'")

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

        return issues

    async def _validate_plan_semantics(self, plan: ProjectPlan, workdir: str) -> List[str]:
        """LLM-based validation: check for logical contradictions between tasks."""
        debug = bool(self._config.defaults.manager_debug_log)
        tasks_text = json.dumps(
            [{"id": t.id, "title": t.title, "description": t.description,
              "acceptance_criteria": t.acceptance_criteria, "depends_on": t.depends_on}
             for t in plan.tasks],
            ensure_ascii=False, indent=2,
        )
        if debug:
            _debug_write(workdir, "manager_validate_prompt", "Plan Validation Prompt", tasks_text)

        raw = await chat_completion(
            self._config, PLAN_VALIDATION_SYSTEM, tasks_text, response_format={"type": "json_object"}
        )
        if debug:
            _debug_write(workdir, "agent_validate_response", "Plan Validation Response", raw or "(empty)")

        if not raw:
            return []
        try:
            payload = json.loads(_extract_json_object(raw))
            if isinstance(payload, dict) and not payload.get("valid", True):
                return [str(x) for x in (payload.get("issues") or []) if x]
        except Exception:
            pass
        return []

    async def _validate_plan(self, plan: ProjectPlan, workdir: str) -> List[str]:
        """Full plan validation: structural + semantic. Returns list of issues."""
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

        # --- Pass 1: normalize interrupted / stale statuses ---
        for t in plan.tasks:
            if t.status == "in_progress":
                # Interrupted during development.
                # If retry budget is exhausted, mark as failed; otherwise restart from pending.
                if t.attempt >= t.max_attempts:
                    t.status = "failed"
                else:
                    t.status = "pending"
            elif t.status == "in_review":
                # Interrupted during review.
                # Same rule as in_progress: retry if attempts remain, otherwise fail.
                if t.attempt >= t.max_attempts:
                    t.status = "failed"
                else:
                    t.status = "pending"
            elif t.status == "rejected":
                if t.attempt >= t.max_attempts:
                    t.status = "failed"
                else:
                    t.status = "pending"
            elif t.status == "failed" and t.attempt < t.max_attempts:
                # Previously failed task can be retried if attempts remain.
                t.status = "pending"

        # --- Pass 2: re-evaluate blocked tasks (they may be unblocked now) ---
        for t in plan.tasks:
            if t.status == "blocked":
                deps = [tasks_by_id[dep_id] for dep_id in t.depends_on if dep_id in tasks_by_id]
                if not any(d.status == "failed" for d in deps):
                    t.status = "pending"

        # --- Pass 3: find next ready task ---
        for t in plan.tasks:
            # Cascade blocking: if any dependency failed → block
            deps = [tasks_by_id[dep_id] for dep_id in t.depends_on if dep_id in tasks_by_id]
            if any(d.status == "failed" for d in deps):
                if t.status not in ("approved", "failed"):
                    t.status = "blocked"
                continue

            if t.status in ("approved", "failed", "blocked"):
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

    @staticmethod
    def _can_resume_failed(plan: ProjectPlan) -> bool:
        """True if a failed plan still has tasks that can be retried."""
        for t in plan.tasks:
            if t.status in ("pending", "rejected", "in_progress", "in_review"):
                return True
            # Blocked tasks may become unblocked after normalization
            if t.status == "blocked":
                return True
            # A failed task with attempts left can be retried
            if t.status == "failed" and t.attempt < t.max_attempts:
                return True
        return False

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
                    await bot._send_message(
                        context, chat_id=chat_id,
                        text=f"⛔ Превышен лимит итераций ({max_iterations}). План остановлен.",
                    )
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
                        await bot._send_message(
                            context, chat_id=chat_id,
                            text="⛔ План остановлен: невозможно продолжить (задачи заблокированы).",
                        )
                save_plan(session.workdir, plan)
                break

            plan.current_task_id = task.id
            skip_dev = task.status == "in_review"  # dev done, review was interrupted

            task.attempt += 1
            task.started_at = task.started_at or _now_iso()

            if skip_dev:
                # Development already completed — go straight to review
                if chat_id is not None:
                    await bot._send_message(
                        context, chat_id=chat_id,
                        text=f"🔍 Продолжаю ревью: {task.title} (попытка {task.attempt}/{task.max_attempts})",
                    )
            else:
                task.status = "in_progress"
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    task_num, task_total = _task_progress(plan, task)
                    await bot._send_message(
                        context, chat_id=chat_id,
                        text=(
                            f"🔧 Разработка ({task_num}/{task_total}): {task.title} "
                            f"(попытка {task.attempt}/{task.max_attempts})"
                        ),
                    )

                # === DEVELOPMENT ===
                dev_ok, dev_report = await self._delegate_develop(session, plan, task)
                task.dev_report = dev_report
                save_plan(session.workdir, plan)
                if not dev_ok:
                    if task.attempt >= task.max_attempts:
                        task.status = "failed"
                        task.completed_at = _now_iso()
                        save_plan(session.workdir, plan)
                        if chat_id is not None:
                            await bot._send_message(
                                context, chat_id=chat_id,
                                text=f"❌ Провал: {task.title} — исчерпаны попытки ({task.max_attempts}). {dev_report[:150]}",
                            )
                        # Check if plan is now blocked
                        if self._is_plan_blocked(plan):
                            plan.status = "failed"
                            save_plan(session.workdir, plan)
                            if chat_id is not None:
                                await bot._send_message(
                                    context, chat_id=chat_id,
                                    text="⛔ План остановлен: критическая задача провалена.",
                                )
                            break
                    else:
                        task.status = "pending"  # will be retried on next iteration
                        save_plan(session.workdir, plan)
                        if chat_id is not None:
                            await bot._send_message(
                                context, chat_id=chat_id,
                                text=(
                                    f"⚠️ Ошибка: {task.title} (попытка {task.attempt}/{task.max_attempts}): "
                                    f"{dev_report[:150]}\n🔄 Повтор..."
                                ),
                            )
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
                # Auto-commit approved changes
                committed = await self._auto_commit(session, task, plan, bot, context, dest)
                # Reconcile plan: CLI may have done more than asked
                if committed:
                    await self._reconcile_plan_after_commit(session, task, plan, bot, context, dest)
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
                    await bot._send_message(
                        context, chat_id=chat_id,
                        text=f"❌ Провал: {task.title} — исчерпаны попытки ({task.max_attempts})",
                    )
                # Check if plan is now blocked
                if self._is_plan_blocked(plan):
                    plan.status = "failed"
                    save_plan(session.workdir, plan)
                    if chat_id is not None:
                        await bot._send_message(
                            context, chat_id=chat_id,
                            text="⛔ План остановлен: критическая задача провалена.",
                        )
                    break
            else:
                task.status = "pending"  # will be retried
                save_plan(session.workdir, plan)
                if chat_id is not None:
                    reasons_txt = ", ".join(reasons) if reasons else "см. замечания"
                    await bot._send_message(
                        context, chat_id=chat_id,
                        text=f"🔄 Доработка: {task.title} (попытка {task.attempt + 1})\nПричины: {reasons_txt}",
                    )

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

            instr = DEV_REWORK_INSTRUCTION_TEMPLATE.format(
                task_title=task.title,
                task_description=task.description,
                dev_report=task.dev_report or "(отчёт отсутствует)",
                review_comments=task.review_comments,
                rejection_history_block=rejection_history_block,
                task_acceptance=_task_acceptance(task),
                partial_work_block=partial_work_block,
                project_context=ctx or "(контекст не задан)",
                already_done=already_done or "(нет данных)",
                completed_tasks_summary=completed_summary,
                attempt=task.attempt,
                max_attempts=task.max_attempts,
            )
        else:
            # First attempt: full task description
            instr = DEV_INSTRUCTION_TEMPLATE.format(
                task_title=task.title,
                task_description=task.description,
                task_acceptance=_task_acceptance(task),
                rejection_block="",
                partial_work_block=partial_work_block,
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
    # Git auto-commit after approved task
    # -----------------------------------------------------------------------

    @staticmethod
    async def _run_git(workdir: str, args: List[str]) -> Tuple[int, str]:
        """Run a git command in *workdir* and return (returncode, output)."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, (out or b"").decode(errors="ignore")

    async def _auto_commit(self, session: Session, task: DevTask, plan: ProjectPlan, bot, context, dest: dict) -> bool:
        """Perform git add -A && git commit after an approved task. Returns True if committed."""
        if not self._config.defaults.manager_auto_commit:
            return False

        chat_id = dest.get("chat_id")
        workdir = session.workdir
        debug = bool(self._config.defaults.manager_debug_log)

        # 1. Check this is a git repo
        code, out = await self._run_git(workdir, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or out.strip() != "true":
            _log.debug("auto_commit: not a git repo, skipping")
            return False

        # 2. Check if there are changes
        code, status_out = await self._run_git(workdir, ["status", "--porcelain"])
        if code != 0 or not status_out.strip():
            _log.debug("auto_commit: no changes to commit")
            return False

        # 3. Get diff stat for commit message context
        code, stat_out = await self._run_git(workdir, ["diff", "--stat"])
        if code != 0:
            stat_out = ""
        # Include staged changes stat too
        code, staged_stat = await self._run_git(workdir, ["diff", "--staged", "--stat"])
        if code == 0 and staged_stat.strip():
            stat_out = f"{stat_out}\n{staged_stat}".strip()

        # 4. Generate commit message via LLM
        user_msg = (
            f"Задача: {task.title}\n"
            f"Описание: {task.description}\n"
            f"Критерии приёмки:\n{_task_acceptance(task)}\n\n"
            f"git status --porcelain:\n{status_out.strip()}\n\n"
            f"git diff --stat:\n{stat_out.strip()}"
        )
        if debug:
            _debug_write(workdir, f"manager_commit_prompt_{task.id}",
                         f"Commit Message Prompt [{task.id}]", user_msg)

        raw = await chat_completion(self._config, COMMIT_MESSAGE_SYSTEM, user_msg[:8000])

        if debug:
            _debug_write(workdir, f"agent_commit_response_{task.id}",
                         f"Commit Message Response [{task.id}]", raw or "(empty)")

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
                await bot._send_message(context, chat_id=chat_id,
                                        text=f"⚠️ Git add failed: {add_out[:200]}")
            return False

        # 6. git commit
        args = ["commit", "-m", summary_line]
        if body:
            args += ["-m", body]
        code, commit_out = await self._run_git(workdir, args)
        if code != 0:
            _log.warning("auto_commit: git commit failed: %s", commit_out)
            if chat_id is not None:
                await bot._send_message(context, chat_id=chat_id,
                                        text=f"⚠️ Git commit failed: {commit_out[:200]}")
            return False

        _log.info("auto_commit: committed for task %s: %s", task.id, summary_line)
        if chat_id is not None:
            await bot._send_message(context, chat_id=chat_id,
                                    text=f"📝 Коммит: {summary_line}")
        return True

    # -----------------------------------------------------------------------
    # Plan reconciliation after commit
    # -----------------------------------------------------------------------

    async def _reconcile_plan_after_commit(
        self, session: Session, task: DevTask, plan: ProjectPlan, bot, context, dest: dict,
    ) -> None:
        """After a commit, check if CLI did more than asked and adjust the plan accordingly."""
        chat_id = dest.get("chat_id")
        workdir = session.workdir
        debug = bool(self._config.defaults.manager_debug_log)

        # Only reconcile if there are remaining non-approved tasks
        remaining = [t for t in plan.tasks if t.status not in ("approved", "failed", "blocked")]
        if not remaining:
            return

        # Get current project state (git diff stat from last commit)
        code, log_out = await self._run_git(workdir, ["log", "-1", "--stat", "--format=%s"])
        if code != 0:
            log_out = ""

        # Build context for reconciliation
        remaining_tasks_info = []
        for t in remaining:
            remaining_tasks_info.append({
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "acceptance_criteria": t.acceptance_criteria,
                "status": t.status,
                "depends_on": t.depends_on,
            })

        user_msg = (
            f"### Выполненная задача:\n"
            f"Название: {task.title}\n"
            f"Описание: {task.description}\n\n"
            f"### Отчёт разработчика:\n{task.dev_report or '(пусто)'}\n\n"
            f"### Последний коммит (git log -1 --stat):\n{log_out.strip()}\n\n"
            f"### Оставшиеся задачи:\n{json.dumps(remaining_tasks_info, ensure_ascii=False, indent=2)}"
        )

        if debug:
            _debug_write(workdir, f"manager_reconcile_prompt_{task.id}",
                         f"Plan Reconcile Prompt [{task.id}]", user_msg)

        raw = await chat_completion(
            self._config, PLAN_RECONCILE_SYSTEM, user_msg[:12000],
            response_format={"type": "json_object"},
        )

        if debug:
            _debug_write(workdir, f"agent_reconcile_response_{task.id}",
                         f"Plan Reconcile Response [{task.id}]", raw or "(empty)")

        if not raw:
            return

        try:
            payload = json.loads(_extract_json_object(raw))
            if not isinstance(payload, dict):
                return
        except Exception:
            return

        tasks_by_id = {t.id: t for t in plan.tasks}
        changes_made = False

        # 1. Mark fully completed tasks
        completed_ids = payload.get("completed_task_ids") or []
        for tid in completed_ids:
            t = tasks_by_id.get(tid)
            if t and t.status not in ("approved", "failed"):
                t.status = "approved"
                t.completed_at = _now_iso()
                t.review_verdict = "approved"
                t.review_comments = "Автоматически закрыта: работа выполнена в рамках предыдущей задачи"
                changes_made = True
                _log.info("reconcile: task %s auto-approved (done by CLI in task %s)", tid, task.id)

        # 2. Apply adjustments to remaining tasks (partial completion)
        adjustments = payload.get("adjustments") or []
        for adj in adjustments:
            if not isinstance(adj, dict):
                continue
            tid = adj.get("task_id")
            t = tasks_by_id.get(tid)
            if not t or t.status in ("approved", "failed"):
                continue
            new_desc = adj.get("updated_description")
            new_criteria = adj.get("updated_acceptance_criteria")
            done_note = adj.get("already_done_note")
            if new_desc and isinstance(new_desc, str) and new_desc.strip():
                t.description = new_desc.strip()
                changes_made = True
            if new_criteria and isinstance(new_criteria, list) and new_criteria:
                t.acceptance_criteria = [str(c) for c in new_criteria if c]
                changes_made = True
            if done_note and isinstance(done_note, str) and done_note.strip():
                # Accumulate partial work notes (task may be adjusted multiple times)
                existing = t.partial_work_note or ""
                if existing:
                    t.partial_work_note = f"{existing}\n{done_note.strip()}"
                else:
                    t.partial_work_note = done_note.strip()
                changes_made = True
            _log.info("reconcile: task %s adjusted — %s", tid, adj.get("reason", ""))

        if changes_made:
            plan.updated_at = _now_iso()
            save_plan(workdir, plan)

            summary = payload.get("summary") or "План скорректирован"
            if chat_id is not None:
                # Build notification
                lines = [f"🔄 Сверка плана после коммита: {summary}"]
                if completed_ids:
                    lines.append(f"✅ Автоматически закрыты: {', '.join(completed_ids)}")
                if adjustments:
                    adj_ids = [a.get("task_id", "?") for a in adjustments if isinstance(a, dict)]
                    lines.append(f"📝 Скорректированы: {', '.join(adj_ids)}")
                await bot._send_message(context, chat_id=chat_id, text="\n".join(lines))

            if debug:
                _debug_write(workdir, f"manager_reconcile_result_{task.id}",
                             f"Plan Reconcile Result [{task.id}]",
                             json.dumps(payload, ensure_ascii=False, indent=2))

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
