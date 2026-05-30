from __future__ import annotations

import json
import logging
from typing import Any, Dict, TYPE_CHECKING

from modes.sdk.runtime.contracts import DevTask, ProjectPlan
from modes.sdk.runtime.json_normalizer import loads_safe

if TYPE_CHECKING:
    from agent.manager_core import ManagerOrchestrator

_log = logging.getLogger(__name__)


class GitReconcileService:
    """
    Git snapshot / plan-reconcile operations extracted from ManagerOrchestrator.

    Accepts *orchestrator* as a dependency to access git helpers, messaging,
    services and plan-save logic without duplicating them.
    """

    def __init__(self, orchestrator: "ManagerOrchestrator") -> None:
        self._orch = orchestrator

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def snapshot_workdir(
        self,
        workdir: str,
        *,
        max_files: int = 50_000,
        hash_max_bytes: int = 256 * 1024,
    ) -> Dict[str, Dict[str, object]]:
        return self._orch._review_service.snapshot_workdir(
            workdir,
            max_files=max_files,
            hash_max_bytes=hash_max_bytes,
        )

    @staticmethod
    def diff_snapshots(
        before: Dict[str, Dict[str, object]],
        after: Dict[str, Dict[str, object]],
    ) -> Dict[str, object]:
        from modes.manager.services.review_service import ReviewAndMergeService
        return ReviewAndMergeService.diff_snapshots(before, after)

    # ------------------------------------------------------------------
    # Baseline auto-commit
    # ------------------------------------------------------------------

    async def auto_commit_baseline_before_first_step(
        self,
        session: Any,
        plan: ProjectPlan,
        bot: Any,
        context: Any,
        dest: dict,
    ) -> bool:
        """Create a rollback baseline commit before the first executable step."""
        orch = self._orch
        if not bool(getattr(orch._config.defaults, "manager_auto_commit", False)):
            return False

        workdir = session.workdir
        chat_id = dest.get("chat_id")
        if not orch._git_is_usable(workdir):
            return False

        code, status_out = await orch._run_git(workdir, ["status", "--porcelain"])
        if code != 0:
            _log.warning("baseline_commit: git status failed: %s", status_out)
            if chat_id is not None:
                await orch._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⚠️ Baseline commit skipped: git status failed: {(status_out or '')[:200]}",
                )
            return False
        if not (status_out or "").strip():
            return False

        code, add_out = await orch._run_git(workdir, ["add", "-A"])
        if code != 0:
            _log.warning("baseline_commit: git add failed: %s", add_out)
            if chat_id is not None:
                await orch._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⚠️ Baseline commit skipped: git add failed: {(add_out or '')[:200]}",
                )
            return False

        first_task = ""
        for t in plan.tasks:
            if t.status not in ("approved", "failed", "blocked"):
                first_task = str(t.title or "").strip()
                break
        summary_line = "[Manager] Baseline before first step"
        if first_task:
            summary_line = f"[Manager] Baseline before: {first_task}"
        if len(summary_line) > 100:
            summary_line = summary_line[:100].rstrip()

        code, commit_out = await orch._run_git(workdir, ["commit", "-m", summary_line])
        if code != 0:
            _log.warning("baseline_commit: git commit failed: %s", commit_out)
            if chat_id is not None:
                await orch._send_adapter_message(
                    bot,
                    context,
                    chat_id=chat_id,
                    text=f"⚠️ Baseline commit skipped: {(commit_out or '')[:200]}",
                )
            return False

        _log.info("baseline_commit: committed: %s", summary_line)
        if chat_id is not None:
            await orch._send_adapter_message(bot, context, chat_id=chat_id, text=f"🧷 Бейзлайн-коммит: {summary_line}")
        return True

    # ------------------------------------------------------------------
    # Plan reconciliation after commit
    # ------------------------------------------------------------------

    async def reconcile_plan_after_commit(
        self,
        session: Any,
        task: DevTask,
        plan: ProjectPlan,
        bot: Any,
        context: Any,
        dest: dict,
    ) -> None:
        """After a commit, check if CLI did more than asked and adjust the plan accordingly."""
        import agent.manager_core as _mc
        from agent.manager_core import _archive_response_write, _now_iso, manager_run_phase_for_plan

        orch = self._orch
        chat_id = dest.get("chat_id")
        workdir = session.workdir
        archive_enabled = bool(orch._config.defaults.manager_response_archive)

        remaining_tasks_info = orch._review_service.remaining_tasks_info(plan)
        if not remaining_tasks_info:
            return

        code, log_out = await orch._run_git(workdir, ["log", "-1", "--stat", "--format=%s (%h)"])
        if code != 0:
            log_out = ""

        analysis_payload = orch._plan_service.serialize_analysis(plan.analysis)
        user_msg = (
            f"### Выполненная задача:\n"
            f"Название: {task.title}\n"
            f"Описание: {task.description}\n\n"
            f"### Отчёт разработчика:\n{task.dev_report or '(пусто)'}\n\n"
            f"### Последний коммит (git log -1 --stat):\n{log_out.strip()}\n\n"
            f"### Текущий анализ проекта:\n{json.dumps(analysis_payload, ensure_ascii=False, indent=2)}\n\n"
            f"### Оставшиеся задачи:\n{json.dumps(remaining_tasks_info, ensure_ascii=False, indent=2)}"
        )

        if archive_enabled:
            _archive_response_write(
                workdir,
                f"manager_reconcile_prompt_{task.id}",
                f"Plan Reconcile Prompt [{task.id}]",
                user_msg,
            )

        raw = await _mc.chat_completion(
            orch._config,
            orch._manager_prompt(workdir, "plan_reconcile_system"),
            user_msg[:12000],
            response_format={"type": "json_object"},
        )

        if archive_enabled:
            _archive_response_write(
                workdir,
                f"agent_reconcile_response_{task.id}",
                f"Plan Reconcile Response [{task.id}]",
                raw or "(empty)",
            )

        if not raw:
            return

        try:
            payload = loads_safe(raw, strict_first=False)
            if not isinstance(payload, dict):
                return
        except Exception:
            return

        before_state = plan.analysis.current_state or "" if plan.analysis else ""
        apply_result = orch._review_service.apply_reconcile_payload(
            plan,
            payload,
            now_iso=_now_iso,
        )
        changes_made = bool(apply_result.get("changes_made"))
        analysis_changed = bool(apply_result.get("analysis_changed"))
        completed_ids = list(apply_result.get("completed_ids") or [])
        adjusted_ids = list(apply_result.get("adjusted_ids") or [])
        adjustment_log = list(apply_result.get("adjustment_log") or [])

        if analysis_changed:
            after_state = plan.analysis.current_state or "" if plan.analysis else ""

            def _p(s: str, n: int = 120) -> str:
                s = s or ""
                return (s[:n] + "…") if len(s) > n else s

            _log.info(
                "reconcile: global analysis updated current_state: %r -> %r",
                _p(before_state),
                _p(after_state),
            )

        for tid in completed_ids:
            _log.info("reconcile: task %s auto-approved (done by CLI in task %s)", tid, task.id)

        for item in adjustment_log:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("task_id") or "").strip()
            if not tid:
                continue
            _log.info("reconcile: task %s adjusted — %s", tid, item.get("reason", ""))

        if changes_made:
            plan.updated_at = _now_iso()
            plan = orch._save_plan_with_run_artifacts(
                session, plan, phase=manager_run_phase_for_plan(plan, fallback="develop")
            )

            summary = payload.get("summary") or "План скорректирован"
            if chat_id is not None:
                lines = [f"🔄 Сверка плана после коммита: {summary}"]
                if completed_ids:
                    lines.append(f"✅ Автоматически закрыты: {', '.join(completed_ids)}")
                if adjusted_ids:
                    lines.append(f"📝 Скорректированы: {', '.join(adjusted_ids)}")
                await orch._send_adapter_message(bot, context, chat_id=chat_id, text="\n".join(lines))

            if archive_enabled:
                _archive_response_write(
                    workdir,
                    f"manager_reconcile_result_{task.id}",
                    f"Plan Reconcile Result [{task.id}]",
                    json.dumps(payload, ensure_ascii=False, indent=2),
                )

    # ------------------------------------------------------------------
    # Plan reconciliation after filesystem change audit (no-git path)
    # ------------------------------------------------------------------

    async def reconcile_plan_after_change_audit(
        self,
        session: Any,
        task: DevTask,
        plan: ProjectPlan,
        bot: Any,
        context: Any,
        dest: dict,
    ) -> None:
        """
        If git isn't used, detect 'CLI did more than asked' via a filesystem change audit
        captured during the dev step, and adjust remaining tasks accordingly.
        """
        import agent.manager_core as _mc
        from agent.manager_core import _archive_response_write, _now_iso, manager_run_phase_for_plan

        orch = self._orch
        chat_id = dest.get("chat_id")
        workdir = session.workdir
        archive_enabled = bool(getattr(orch._config.defaults, "manager_response_archive", False))

        remaining_tasks_info = orch._review_service.remaining_tasks_info(plan)
        if not remaining_tasks_info:
            return

        audit_text = str(task.manager_change_audit or "").strip()
        if not audit_text:
            return

        analysis_payload = orch._plan_service.serialize_analysis(plan.analysis)
        user_msg = (
            f"### Выполненная задача:\n"
            f"Название: {task.title}\n"
            f"Описание: {task.description}\n\n"
            f"### Отчёт разработчика:\n{task.dev_report or '(пусто)'}\n\n"
            f"### Последние изменения (аудит без git):\n{audit_text}\n\n"
            f"### Текущий анализ проекта:\n{json.dumps(analysis_payload, ensure_ascii=False, indent=2)}\n\n"
            f"### Оставшиеся задачи:\n{json.dumps(remaining_tasks_info, ensure_ascii=False, indent=2)}"
        )

        if archive_enabled:
            _archive_response_write(
                workdir,
                f"manager_reconcile_nogit_prompt_{task.id}",
                f"Plan Reconcile Prompt (no-git) [{task.id}]",
                user_msg,
            )

        raw = await _mc.chat_completion(
            orch._config,
            orch._manager_prompt(workdir, "plan_reconcile_system"),
            user_msg[:12000],
            response_format={"type": "json_object"},
        )

        if archive_enabled:
            _archive_response_write(
                workdir,
                f"agent_reconcile_nogit_response_{task.id}",
                f"Plan Reconcile Response (no-git) [{task.id}]",
                raw or "(empty)",
            )

        if not raw:
            return

        try:
            payload = loads_safe(raw, strict_first=False)
            if not isinstance(payload, dict):
                return
        except Exception:
            return

        apply_result = orch._review_service.apply_reconcile_payload(
            plan,
            payload,
            now_iso=_now_iso,
        )
        changes_made = bool(apply_result.get("changes_made"))
        completed_ids = list(apply_result.get("completed_ids") or [])
        adjusted_ids = list(apply_result.get("adjusted_ids") or [])

        if not changes_made:
            return

        plan.updated_at = _now_iso()
        plan = orch._save_plan_with_run_artifacts(
            session, plan, phase=manager_run_phase_for_plan(plan, fallback="develop")
        )

        summary = payload.get("summary") or "План скорректирован"
        if chat_id is not None:
            lines = [f"🔄 Сверка плана (без git): {summary}"]
            if completed_ids:
                lines.append(f"✅ Автоматически закрыты: {', '.join(completed_ids)}")
            if adjusted_ids:
                lines.append(f"📝 Скорректированы: {', '.join(adjusted_ids)}")
            await orch._send_adapter_message(bot, context, chat_id=chat_id, text="\n".join(lines))

        if archive_enabled:
            _archive_response_write(
                workdir,
                f"manager_reconcile_nogit_result_{task.id}",
                f"Plan Reconcile Result (no-git) [{task.id}]",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
