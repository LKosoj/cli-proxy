from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import yaml

from app.mode_dependencies import ModeDependencies
from app.services.session_run_service import ModeScopedPreRunResetService
from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore, is_terminal_status
from modes.analyst import template_service as analyst_templates
from modes.analyst.draft_service import build_draft_text, resolve_draft_document_profile
from modes.analyst.routing_rules import build_template_from_profile, classify_profile_from_text
from modes.analyst.schemas import AnalystIntentInputSchema, AnalystIntentOutputSchema
from modes.analyst.state_store import (
    AnalystContext,
    AnalystStateStore,
    build_context_key,
    resolve_analyst_state_root,
)
from modes.sdk.json_store import read_json_locked
from modes.sdk.runtime.ask_user_generation import (
    ASK_USER_CLARIFICATION_SYSTEM,
    build_validated_ask_payload,
)
from modes.sdk.runtime.ask_user_schema import (
    DEFAULT_ASK_OPTIONS,
    apply_ask_schema,
    normalize_ask_question,
)
from modes.sdk.runtime.json_normalizer import loads_safe, parse_normalize_validate
from modes.sdk.runtime.openai_client import chat_completion
from modes.sdk.runtime.final_qc import runtime_readiness_allows_finalization
from agent.analyst_prompts import build_analyst_prompt
from modes.analyst.ui import build_analyst_menu, build_analyst_status_text
from modes.sdk import BaseMode, CallbackModel, MessageModel, ToolResult, encode_mode_dirs
from modes.sdk.session_busy import is_session_busy
from modes.sdk.services import CodebaseContextService, CodebaseContextText, MessagingService
from session import session_scoped_key
from sessions.session_state_access import set_analyst_mode
from utils.text import strip_ansi

_TOOL_CALL_BLOCK_RE = re.compile(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", re.IGNORECASE | re.DOTALL)
_TOOL_CALL_TAG_RE = re.compile(r"(?im)^\s*\[/?TOOL_CALL\]\s*$")
_ANALYST_RUN_HANDLE_SESSION_ATTR = "analyst_run_artifact_handle"
_ANALYST_RUN_RESUME_GUARD_SESSION_ATTR = "analyst_run_resume_guard"
_ANALYST_STEP_RESULTS_SYNC_HOOK_SESSION_ATTR = "analyst_step_results_sync_hook"
_ANALYST_FINAL_CANDIDATE_PATH_SESSION_ATTR = "analyst_runtime_final_candidate_path"
logger = logging.getLogger(__name__)


def _sanitize_final_analyst_output(text: Any) -> str:
    cleaned = strip_ansi(str(text or ""))
    if not cleaned:
        return ""
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", cleaned)
    cleaned = _TOOL_CALL_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class AnalystMode(BaseMode):
    mode_id = "analyst"
    display_name = "🧠 Аналитик"
    description = "Формирует ТЗ по запросу и поддерживает аудит-поток"

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)
        self._prompts: Dict[str, str] = {}

    def include_files_in_dirs(self, flow: str) -> bool:
        return str(flow or "").strip() == "audit"

    def pre_run_reset_mode_id(self) -> Optional[str]:
        return self.mode_id

    def framework_sends_output(self) -> bool:
        return False

    def build_runtime(self, config: Any) -> Any:
        from .runner_service import AnalystModeRunnerService
        return AnalystModeRunnerService(config)

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app:
            self._reset_state_on_enable(session=session, bot_app=bot_app)
            self._clear_persisted_last_draft(session=session)
            await self._activate_mode(session=session, bot_app=bot_app, cli_work_type="analytics", executor_profile="analyst")
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app and self._is_mode_active(session):
            self._reset_audit_state(session=session)
            try:
                chat_id = int(ctx.get("chat_id") or getattr(session, "chat_id", 0) or 0)
            except Exception:
                chat_id = 0
            self._clear_audit_dirs_flow(chat_id=chat_id)
            await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        return None

    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        chat_id = self._normalize_callback_chat_id(message.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")
        msg_user_id = int(message.user_id) if getattr(message, "user_id", None) is not None else None

        dest = self._normalize_dest(ctx_dest=ctx.get("dest"), chat_id=chat_id, user_id=msg_user_id)

        ms = self._messaging(bot_app=bot_app, context=context)

        await self._maybe_resolve_unfinished_analysis(
            session=session,
            message=message,
            bot_app=bot_app,
            context=context,
            dest=dest,
            chat_id=chat_id,
            ms=ms,
        )

        if await self._enqueue_if_busy(session=session, bot_app=bot_app, ms=ms, chat_id=chat_id, text=message.text, dest=dest):
            return ToolResult.ok()

        async def _run() -> None:
            pipeline = self._pipeline()
            await pipeline.run_mode_pipeline(
                session,
                message.text,
                dict(dest),
                context,
                mode_id=self.mode_id,
            )

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="run_analyst")
        return ToolResult.ok()

    async def run_pipeline(
        self,
        *,
        session: Any,
        user_text: str,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> str:
        """Analyst pipeline.

        Uses run_directory, gate_service, and profile-based intent classification.
        """
        from .run_directory import AnalystRunDirectory, resolve_analyst_runs_root
        from .gate_service import AnalystGateService

        base_path = resolve_analyst_runs_root(session)
        run_dir = AnalystRunDirectory(base_path)
        run = None
        phase = "intent"
        base_execution_context = self._execution_context(session=session, dest=dest, user_text=user_text)
        run, resume_guard = self._prepare_run_artifacts(session=session, user_text=user_text, dest=dest)

        # ── progress ticker: отправка этапов в Telegram/Desktop каждые 30 с ──
        # начальная фаза — должна быть установлена до старта тикера, иначе первый emit увидит stale-значение
        setattr(session, "analyst_pipeline_phase", "classify")
        progress_ticker_task: Optional[asyncio.Task[None]] = None
        progress_msg_ref: Any = None
        progress_lock = asyncio.Lock()
        try:
            progress_messaging: Optional[MessagingService] = self._messaging(bot_app=bot_app, context=context)
        except Exception as e:
            self._log.warning("analyst progress messaging init failed: %s", e)
            progress_messaging = None
        pipeline_started_at = time.monotonic()
        _phase_labels: Dict[str, str] = {
            "classify": "Классификация задачи",
            "plan": "Планирование шагов",
            "gate1": "Уточняющие вопросы",
            "gather": "Сбор контекста",
            "gate2": "Финальное уточнение",
            "analyze": "Анализ и генерация",
            "deliver": "Подготовка результата",
            "completed": "Готово",
            "failed": "Ошибка",
        }

        async def _emit_progress() -> None:
            async with progress_lock:
                nonlocal progress_msg_ref
                progress_msg_ref = await self._emit_analyst_progress(
                    messaging=progress_messaging,
                    session=session,
                    dest=dest,
                    progress_msg_ref=progress_msg_ref,
                    pipeline_started_at=pipeline_started_at,
                    phase_labels=_phase_labels,
                )

        async def _clear_progress() -> None:
            async with progress_lock:
                nonlocal progress_msg_ref
                progress_msg_ref = await self._clear_analyst_progress(
                    messaging=progress_messaging,
                    bot_app=bot_app,
                    dest=dest,
                    progress_msg_ref=progress_msg_ref,
                )

        async def _progress_ticker() -> None:
            # первое сообщение сразу
            await _emit_progress()
            while True:
                await asyncio.sleep(30)
                await _emit_progress()

        try:
            progress_ticker_task = asyncio.create_task(_progress_ticker())

            # ── 1. CLASSIFY ──────────────────────────────────────────
            setattr(session, "analyst_pipeline_phase", "classify")
            logger.info("run_pipeline: phase=classify session=%s", getattr(session, "id", "?"))

            intent_data = await self._classify_intent(
                session=session,
                user_text=user_text,
                context=context,
                dest=dest,
                bot_app=bot_app,
            )
            analysis_profile: str = intent_data["analysis_profile"]
            document_kind: str = intent_data["document_kind"]
            detail_level: str = intent_data["detail_level"]
            summary: str = intent_data["summary"]

            template_id, template = self._resolve_template(intent_data, user_text)
            clarification_questions = self._filter_gate1_questions(
                analysis_profile=analysis_profile,
                template=template,
                clarification_questions=intent_data.get("clarification_questions") or [],
                user_text=user_text,
            )
            intent_data = dict(intent_data or {})
            intent_data["clarification_questions"] = list(clarification_questions)
            self._persist_intent_context(
                session=session,
                intent_data=intent_data,
                template_id=template_id,
                template=template,
                user_text=user_text,
            )
            self._save_run_state(
                run,
                phase="intent",
                status="running",
                mode_context={
                    "intent_payload": {
                        **dict(intent_data or {}),
                        "template_id": template_id,
                        "effective_template_id": template_id,
                    },
                    "analysis_profile": analysis_profile,
                    "document_kind": document_kind,
                    "detail_level": detail_level,
                    "template_id": template_id,
                    "summary": summary,
                    "resume_guard": dict(resume_guard or {}),
                    "execution_context": dict(base_execution_context),
                },
            )
            self._validate_run_boundary(run, phase="intent")

            run_dir.create(
                analysis_profile=analysis_profile,
                document_kind=document_kind,
                detail_level=detail_level,
                template_id=template_id,
                summary=summary,
                user_request=user_text,
                session_id=str(getattr(session, "id", "") or ""),
            )
            logger.info(
                "run_pipeline: classified profile=%s kind=%s template=%s run=%s",
                analysis_profile, document_kind, template_id, run_dir.run_id,
            )
            phase = "plan"
            self._save_run_plan(
                run,
                {
                    "kind": "analyst_orchestrator",
                    "units": [
                        {
                            "id": "analyst:orchestrator",
                            "step_type": "orchestrator",
                            "title": "Run analyst orchestrator pipeline",
                        }
                    ],
                },
            )
            self._save_run_state(
                run,
                phase="plan",
                status="running",
                mode_context={
                    "analysis_profile": analysis_profile,
                    "document_kind": document_kind,
                    "detail_level": detail_level,
                    "template_id": template_id,
                    "summary": summary,
                    "execution_context": dict(base_execution_context),
                },
            )
            self._sync_analyst_progress_placeholder(
                run_dir=run_dir,
                status="pending",
                summary="Pipeline analyst инициализирован; выполняются классификация и gate-проверки.",
            )
            self._validate_run_boundary(run, phase="plan")

            # ── 2. GATE 1 ───────────────────────────────────────────
            gate_svc = AnalystGateService(run_dir)
            ask_fn = self._build_ask_fn(session=session, bot_app=bot_app, context=context, dest=dest)

            setattr(session, "analyst_pipeline_phase", "gate1")
            if gate_svc.gate1_should_activate(clarification_questions):
                logger.info("run_pipeline: gate1 activated, questions=%d", len(clarification_questions))
                answers = await gate_svc.execute_gate1(
                    gate_svc.gate1_questions(clarification_questions),
                    ask_fn,
                )
                if answers:
                    user_text = user_text + "\n" + "\n".join(f"Уточнение: {a}" for a in answers)
            else:
                logger.info("run_pipeline: gate1 skipped (no clarification questions)")

            # ── 3. GATHER ────────────────────────────────────────────
            setattr(session, "analyst_pipeline_phase", "gather")
            run_dir.update_phase("gather")
            codebase_context = ""

            if analysis_profile in ("codebase", "codebase_plus_research", "audit"):
                codebase_context = self._build_codebase_context(session=session)
                if codebase_context:
                    codebase_ctx_path = run_dir.codebase_context_path()
                    os.makedirs(os.path.dirname(codebase_ctx_path), exist_ok=True)
                    with open(codebase_ctx_path, "w", encoding="utf-8") as f:
                        f.write(codebase_context)
                    logger.info("run_pipeline: codebase context saved to %s", codebase_ctx_path)

            # ── 4. GATE 2 ────────────────────────────────────────────
            setattr(session, "analyst_pipeline_phase", "gate2")
            gate2_answers = await gate_svc.execute_gate2(ask_fn)
            if gate2_answers:
                logger.info("run_pipeline: gate2 answered questions=%d", len(gate2_answers))
                user_text = user_text + "\n" + "\n".join(f"Уточнение gate2: {a}" for a in gate2_answers)
            else:
                logger.info("run_pipeline: gate2 skipped or produced no answers")

            # ── 5. ANALYZE + BUILD ───────────────────────────────────
            setattr(session, "analyst_pipeline_phase", "analyze")
            run_dir.update_phase("analyze")
            logger.info("run_pipeline: phase=analyze run=%s", run_dir.run_id)

            clarification_answers_for_prompt: list[str] = run_dir.load_meta().get("clarification_answers", [])
            self._mark_clarification_resolved(
                session=session,
                answers=clarification_answers_for_prompt,
            )
            gate_svc.lock_questions()
            setattr(session, "analyst_ask_user_locked", True)
            phase = "execute"
            self._save_run_state(
                run,
                phase="execute",
                status="running",
                mode_context={
                    "analysis_profile": analysis_profile,
                    "document_kind": document_kind,
                    "detail_level": detail_level,
                    "template_id": template_id,
                    "clarification_answers": list(clarification_answers_for_prompt),
                    "execution_context": {
                        **dict(base_execution_context),
                        "clarification_answers": list(clarification_answers_for_prompt),
                    },
                },
            )
            self._sync_analyst_progress_placeholder(
                run_dir=run_dir,
                status="in_progress",
                summary="Оркестратор analyst запущен; repo-grounded step-results появятся по мере выполнения.",
            )

            # Build prompt
            prompts = self._load_prompts()

            repo_grounding_text = ""
            if codebase_context:
                repo_grounding_required = bool(
                    analysis_profile in ("codebase", "codebase_plus_research", "audit")
                    or self._template_flag(template, "repo_grounded_required")
                    or self._template_flag(template, "repo_audit_required")
                    or self._template_flag(template, "final_repo_review_required")
                )
                repo_grounding_blocks: list[str] = []
                if repo_grounding_required:
                    for key in ("repo_consistency_review_rules", "affected_surfaces_checklist"):
                        block = str(prompts.get(key) or "").strip()
                        if block:
                            repo_grounding_blocks.append(block)
                if repo_grounding_blocks:
                    repo_grounding_text = (
                        "<REPO_GROUNDING_RULES>\n"
                        + "\n".join(repo_grounding_blocks)
                        + "\n</REPO_GROUNDING_RULES>"
                    )

            codebase_context_text = ""
            if codebase_context:
                tail = str(
                    prompts.get("run_pipeline_codebase_tail")
                    or "Учитывай карту кодовой базы при анализе и формировании ТЗ."
                )
                codebase_context_text = (
                    f"<CODEBASE_MAP>\n{codebase_context}\n</CODEBASE_MAP>\n\n{tail}"
                )

            artifact_instructions = (
                f"\n\n<ARTIFACT_INSTRUCTIONS>\n"
                f"Все результаты работы записывай в файлы внутри каталога: {run_dir.run_path}\n"
                f"Промежуточные результаты анализа — в steps/ (например steps/01_code_analysis.md)\n"
                f"Черновик документа строй инкрементально в draft/v1.md — дописывай секцию за секцией.\n"
                f"Финальный документ запиши в output/final.md\n"
                f"Не задавай вопросов пользователю — работай с имеющимся контекстом.\n"
                f"</ARTIFACT_INSTRUCTIONS>"
            )

            analyst_prompt = build_analyst_prompt(
                user_text,
                template,
                detail_level=detail_level,
                repo_grounding_text=repo_grounding_text,
                codebase_context_text=codebase_context_text,
                clarification_answers=clarification_answers_for_prompt,
            ) + artifact_instructions

            runtime_getter = self._runtime_getter()
            runtime = runtime_getter("run_analyst")
            if runtime is None:
                raise RuntimeError("Analyst runtime is not configured")

            run_handle = getattr(session, _ANALYST_RUN_HANDLE_SESSION_ATTR, None)
            self._set_step_results_sync_hook(
                session,
                lambda step_results, active_run_handle=run_handle: self._sync_analyst_step_results(
                    run_dir=run_dir,
                    run_handle=active_run_handle,
                    step_results=step_results,
                ),
            )
            staged_final_candidate_path = os.path.join(run_dir.run_path, "output", "final.staged.md")
            setattr(session, _ANALYST_FINAL_CANDIDATE_PATH_SESSION_ATTR, staged_final_candidate_path)
            output = await runtime.run(session, analyst_prompt, bot_app, context, dict(dest or {}))
            evidence_summary = self._sync_analyst_step_artifacts(
                session=session,
                run_dir=run_dir,
                run_handle=run_handle,
            )
            if not evidence_summary:
                self._sync_analyst_progress_placeholder(
                    run_dir=run_dir,
                    status="completed",
                    summary="Рантайм analyst завершён без детализированных step_results; см. финальный output.",
                )
            self._ensure_execute_boundary_evidence(run, output=output)
            self._validate_run_boundary(run, phase="execute")

            # ── 6. DELIVER ───────────────────────────────────────────
            setattr(session, "analyst_pipeline_phase", "deliver")
            run_dir.update_phase("deliver")
            logger.info("run_pipeline: phase=deliver run=%s", run_dir.run_id)

            requires_quality_gate = bool(
                analysis_profile in ("codebase", "codebase_plus_research", "audit")
                or self._template_flag(template, "repo_grounded_required")
                or self._template_flag(template, "repo_audit_required")
                or self._template_flag(template, "final_repo_review_required")
            )
            clean_output = _sanitize_final_analyst_output(output)
            quality_gate_context: Dict[str, Any] = {}
            if requires_quality_gate and run_handle is not None:
                quality = self._load_analyst_quality_metrics(run_handle)
                quality_verdict = str(quality.get("runtime_verdict") or quality.get("verdict") or "").strip()
                if not quality_verdict or not runtime_readiness_allows_finalization(quality):
                    quality_gate_payload = self._build_quality_gate_warning_payload(
                        run=run_handle,
                        quality=quality,
                    )
                    logger.warning(
                        "run_pipeline: analyst quality gate did not pass, delivering final output anyway "
                        "run=%s verdict=%s reasons=%s",
                        run_dir.run_id,
                        quality_verdict,
                        list(quality_gate_payload.get("blocking_reasons") or []),
                    )
                    quality_gate_context = {
                        "quality_gate_passed": False,
                        "quality_gate_verdict": str(quality_gate_payload.get("runtime_verdict") or ""),
                        "quality_gate_reasons": list(quality_gate_payload.get("blocking_reasons") or []),
                        "quality_gate_artifact_path": str(quality_gate_payload.get("artifact_path") or ""),
                    }
                    runtime_delivered = bool(getattr(session, "analyst_runtime_final_output_delivered", False))
                    clarification_pending = bool(
                        getattr(session, "analyst_blocking_clarification_runtime", False)
                        and str(getattr(session, "analyst_blocking_clarification_text", "") or "").strip()
                    )
                    if clean_output and not runtime_delivered and not clarification_pending:
                        await self._deliver_analyst_output(
                            session=session,
                            bot_app=bot_app,
                            context=context,
                            dest=dest,
                            output=clean_output,
                        )
                        setattr(session, "analyst_runtime_final_output_delivered", True)
                        setattr(session, "analyst_runtime_final_output_delivered_text", clean_output)
                        logger.info(
                            "run_pipeline: delivered analyst output after quality warning run=%s output_len=%d",
                            run_dir.run_id,
                            len(clean_output),
                        )
            if evidence_summary:
                logger.info(
                    "run_pipeline: evidence trail synced run=%s steps=%d coverage=%.2f",
                    run_dir.run_id,
                    int(evidence_summary.get("step_count") or 0),
                    float(evidence_summary.get("artifact_coverage") or 0.0),
                )

            if clean_output:
                output_path = run_dir.output_path()
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(clean_output)

                draft_path = run_dir.draft_path(1)
                if not os.path.isfile(draft_path):
                    os.makedirs(os.path.dirname(draft_path), exist_ok=True)
                    with open(draft_path, "w", encoding="utf-8") as f:
                        f.write(clean_output)
            else:
                logger.warning("run_pipeline: runtime returned empty output, run=%s", run_dir.run_id)

            run_dir.update_meta(status="completed", current_phase="completed")
            setattr(session, "analyst_pipeline_phase", "completed")
            phase = "complete"
            self._save_run_state(
                run,
                phase="complete",
                status="completed",
                mode_context={
                    "analysis_profile": analysis_profile,
                    "document_kind": document_kind,
                    "detail_level": detail_level,
                    "template_id": template_id,
                    "final_deliverable": clean_output,
                    **quality_gate_context,
                    "execution_context": dict(base_execution_context),
                },
            )
            self._validate_run_boundary(run, phase="complete")
            self._mark_run_finished(run, status="completed", phase="complete")

            phase = "complete"
            logger.info("run_pipeline: completed run=%s output_len=%d", run_dir.run_id, len(clean_output))
            return clean_output

        except Exception as exc:
            logger.exception("run_pipeline: failed run=%s", run_dir.run_id)
            setattr(session, "analyst_pipeline_phase", "failed")
            try:
                run_dir.update_meta(status="failed")
            except Exception:
                logger.exception("run_pipeline: failed to update meta on error")
            self._save_run_state(
                run,
                phase=phase,
                status="failed",
                mode_context={
                    "runtime_error": str(exc or ""),
                    "execution_context": dict(base_execution_context),
                },
            )
            self._mark_run_finished(run, status="failed", phase=phase)
            raise
        finally:
            setattr(session, "analyst_ask_user_locked", False)
            self._clear_step_results_sync_hook(session)
            self._clear_active_run_handle(session)
            if progress_ticker_task and not progress_ticker_task.done():
                progress_ticker_task.cancel()
                try:
                    await progress_ticker_task
                except asyncio.CancelledError:
                    pass
            await _clear_progress()

    def _build_analyst_progress_text(
        self,
        *,
        session: Any,
        pipeline_started_at: float,
        phase_labels: Dict[str, str],
    ) -> str:
        elapsed = int(time.monotonic() - pipeline_started_at)
        mins, secs = divmod(elapsed, 60)
        time_str = f"{mins}:{secs:02d}"
        current_phase = str(getattr(session, "analyst_pipeline_phase", "classify") or "classify")
        label = phase_labels.get(current_phase, current_phase)
        return f"🧠 Аналитик: {label}\n⏱ {time_str}"

    async def _emit_analyst_progress(
        self,
        *,
        messaging: Optional[MessagingService],
        session: Any,
        dest: Dict[str, Any],
        progress_msg_ref: Any,
        pipeline_started_at: float,
        phase_labels: Dict[str, str],
    ) -> Any:
        if messaging is None:
            return progress_msg_ref
        text = self._build_analyst_progress_text(
            session=session,
            pipeline_started_at=pipeline_started_at,
            phase_labels=phase_labels,
        )
        dest_kind = str((dest or {}).get("kind", ""))
        chat_id = (dest or {}).get("chat_id")
        if not chat_id:
            return progress_msg_ref
        if dest_kind == "telegram":
            msg_id = getattr(progress_msg_ref, "message_id", None) if progress_msg_ref else None
            try:
                if msg_id:
                    await messaging.edit_text(chat_id, msg_id, text)
                    return progress_msg_ref
                return await messaging.send_text(chat_id, text)
            except Exception as e:
                self._log.warning("analyst progress delivery failed: %s", e)
                return progress_msg_ref
        if dest_kind == "desktop":
            try:
                return await messaging.send_text(chat_id, text)
            except Exception as e:
                self._log.warning("analyst progress delivery failed: %s", e)
                return progress_msg_ref
        return progress_msg_ref

    async def _clear_analyst_progress(
        self,
        *,
        messaging: Optional[MessagingService],
        bot_app: Any,
        dest: Dict[str, Any],
        progress_msg_ref: Any,
    ) -> None:
        if progress_msg_ref is None:
            return None
        chat_id = (dest or {}).get("chat_id")
        dest_kind = str((dest or {}).get("kind", ""))
        msg_id = getattr(progress_msg_ref, "message_id", None)
        if dest_kind == "telegram" and chat_id and msg_id and messaging is not None:
            try:
                await messaging.remove(chat_id, msg_id)
            except Exception as e:
                self._log.warning("analyst progress delete failed: %s", e)
        elif dest_kind == "desktop":
            notify = getattr(bot_app, "notify", None)
            if callable(notify):
                try:
                    notify("ui:analyst_progress_clear", session_id=str(chat_id))
                except Exception as e:
                    self._log.warning("analyst progress clear (desktop) failed: %s", e)
        return None

    def _build_ask_fn(
        self,
        *,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> Callable[[str], Awaitable[str]]:
        """Build an ask_fn for gate service from available SDK services."""
        tooling = self._optional_tooling()

        def _build_seed_gate_options(seed_question: str) -> list[str]:
            normalized = self._normalize_gate_question(seed_question)
            if not normalized:
                return list(DEFAULT_ASK_OPTIONS)
            if any(token in normalized for token in ("bot", "desktop")):
                return ["Только bot", "Bot и desktop", "Все клиенты"]
            if any(token in normalized for token in ("cli", "клиент", "gemini", "codex", "qwen", "cloud code")):
                return ["Только текущий CLI", "Все 4 CLI", "Есть отдельные исключения"]
            if any(token in normalized for token in ("чтени", "запис", "формат", "конвертер", "сесси")):
                return ["Только чтение", "Чтение и запись", "Полный двусторонний перенос"]
            if any(token in normalized for token in ("референс", "codedash", "адаптир", "что именно брать")):
                return ["Только обязательный минимум", "Паритет по нужному flow", "Нужна почти полная адаптация"]
            if any(token in normalized for token in ("совместим", "нельзя нарушать", "ограничени", "поверхност")):
                return ["Только основной flow", "Все затронутые сценарии", "Есть жёсткие ограничения"]
            return ["Только обязательный минимум", "Полный рабочий сценарий", "Есть особые ограничения"]

        async def _build_gate_ask_payload(seed_question: str) -> tuple[str, list[str]]:
            normalized_question, normalized_options, issues = apply_ask_schema(
                normalize_ask_question(seed_question),
                _build_seed_gate_options(seed_question),
            )
            if not issues:
                return normalized_question, normalized_options

            question_issues = {
                issue
                for issue in issues
                if issue not in {"too_few_options", "options_normalized", "placeholder_options", "option_too_long"}
            }
            if not question_issues:
                fallback_options = normalized_options or list(DEFAULT_ASK_OPTIONS)
                return normalized_question or str(seed_question or "").strip(), fallback_options[:4]

            config = getattr(self, "config", None)
            defaults = getattr(config, "defaults", None) if config is not None else None
            if not defaults or not getattr(defaults, "openai_api_key", None) or not getattr(defaults, "openai_model", None):
                fallback_options = normalized_options or list(DEFAULT_ASK_OPTIONS)
                return normalized_question or str(seed_question or "").strip(), fallback_options[:4]

            user_prompt = "\n".join(
                [
                    f"Запрос пользователя:\n{str(getattr(session, 'analyst_source_user_text_runtime', '') or '').strip() or '(не задан)'}",
                    f"Текущий вопрос analyst gate:\n{str(seed_question or '').strip()}",
                    "",
                    "Сохрани смысл вопроса, но при необходимости перепиши его на языке пользователя.",
                    "Подбери 2-4 коротких варианта ответа. Не подставляй generic control-options.",
                ]
            )
            try:
                return await build_validated_ask_payload(
                    config,
                    user_prompt=user_prompt,
                    system_prompt=ASK_USER_CLARIFICATION_SYSTEM,
                    chat_completion_fn=chat_completion,
                    log=logger,
                    log_prefix="analyst_gate",
                )
            except Exception:
                logger.exception("analyst gate ask_user rewrite fallback failed")
                fallback_options = normalized_options or list(DEFAULT_ASK_OPTIONS)
                return normalized_question or str(seed_question or "").strip(), fallback_options[:4]

        async def _ask(question: str) -> str:
            if tooling is None:
                raise RuntimeError("analyst gate ask_user tooling is not configured")
            tool_ctx = self._tool_ctx(session=session, context=context, dest=dest, bot_app=bot_app)
            tool_ctx["corr_id"] = f"analyst:{getattr(session, 'id', 'unknown')}:gate"
            try:
                ask_question, ask_options = await _build_gate_ask_payload(question)
                response = await tooling.ask_user(
                    question=ask_question,
                    options=ask_options,
                    ctx=tool_ctx,
                    allow_custom=True,
                    system_options=False,
                )
                return str(response or "").strip()
            except Exception as exc:
                logger.exception("_build_ask_fn: ask_user failed for question: %s", question[:100])
                raise RuntimeError(f"analyst gate ask_user failed: {question[:100]}") from exc

        return _ask

    async def _classify_intent(
        self,
        *,
        session: Any,
        user_text: str,
        context: Any,
        dest: Dict[str, Any],
        bot_app: Any,
        clarification_answers: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """Classify user intent using the simplified schema (4 required fields).

        Returns a dict with keys: analysis_profile, document_kind, detail_level,
        summary, clarification_questions, template_hint.
        Falls back to deterministic classification on any error.
        """
        project_root = str(getattr(session, "project_root", "") or "").strip()
        workdir = str(getattr(session, "workdir", "") or "").strip()

        tooling = self._optional_tooling()
        if tooling is None:
            return self._classify_intent_fallback(user_text, project_root, workdir)

        tool_ctx = self._tool_ctx(session=session, context=context, dest=dest, bot_app=bot_app)
        selected_template = str(
            getattr(
                getattr(session, "modes", None),
                "analyst_template_id",
                getattr(session, "analyst_template_id", "default"),
            )
            or "default"
        ).strip() or "default"

        request_payload: Dict[str, Any] = {
            "user_text": str(user_text or ""),
            "clarification_answers": [
                str(item).strip()
                for item in (clarification_answers or [])
                if str(item).strip()
            ][:8],
            "selected_template": selected_template,
            "project_root": project_root,
            "workdir": str(getattr(session, "workdir", "") or ""),
            "has_codebase_map": self._detect_codebase_map(project_root=project_root, workdir=workdir),
        }
        try:
            request_payload = parse_normalize_validate(
                json.dumps(request_payload, ensure_ascii=False),
                AnalystIntentInputSchema,
            )
        except Exception:
            self._log.exception("analyst intent request normalization failed")
            return self._classify_intent_fallback(user_text, project_root, workdir)

        try:
            response = await tooling.execute(
                "analyst_intent_plugin",
                request_payload,
                tool_ctx,
            )
        except Exception:
            self._log.exception("analyst intent plugin execution failed")
            return self._classify_intent_fallback(user_text, project_root, workdir)

        if not bool(response.get("success")):
            self._log.warning(
                "analyst intent plugin failed: %s", response.get("error"),
            )
            return self._classify_intent_fallback(user_text, project_root, workdir)

        raw = str(response.get("output") or "{}").strip()
        try:
            payload = parse_normalize_validate(raw, AnalystIntentOutputSchema)
        except Exception:
            self._log.warning("analyst intent output validation failed, using deterministic fallback")
            return self._classify_intent_fallback(user_text, project_root, workdir)

        if not isinstance(payload, dict):
            return self._classify_intent_fallback(user_text, project_root, workdir)

        # Normalize optional fields
        questions_raw = payload.get("clarification_questions")
        clarification_questions: list[str] = []
        if isinstance(questions_raw, list):
            clarification_questions = [
                str(q).strip() for q in questions_raw if str(q).strip()
            ][:3]

        return {
            "analysis_profile": str(payload.get("analysis_profile") or "").strip(),
            "document_kind": str(payload.get("document_kind") or "").strip(),
            "detail_level": str(payload.get("detail_level") or "standard").strip(),
            "summary": str(payload.get("summary") or "").strip(),
            "clarification_questions": clarification_questions,
            "template_hint": str(payload.get("template_hint") or "").strip(),
        }

    def _classify_intent_fallback(
        self,
        user_text: str,
        project_root: str,
        workdir: str = "",
    ) -> Dict[str, Any]:
        """Deterministic fallback for intent classification."""
        profile = classify_profile_from_text(
            user_text,
            project_root=project_root or None,
            workdir=workdir or None,
        )

        # Infer document_kind from profile
        if profile == "audit":
            document_kind = "audit"
        elif profile in ("greenfield", "codebase", "codebase_plus_research"):
            document_kind = "spec"
        else:
            document_kind = "analysis"

        self._log.info(
            "analyst intent deterministic fallback: profile=%s document_kind=%s",
            profile,
            document_kind,
        )
        return {
            "analysis_profile": profile,
            "document_kind": document_kind,
            "detail_level": "standard",
            "summary": "",
            "clarification_questions": [],
            "template_hint": "",
        }

    def _resolve_template(
        self,
        intent_data: Dict[str, Any],
        user_text: str,
    ) -> tuple[str, Dict[str, Any]]:
        """Resolve template from intent classification result.

        Args:
            intent_data: Result of _classify_intent.
            user_text: Original user request text.

        Returns:
            Tuple of (template_id, template_dict).

        Raises:
            RuntimeError: If the resolved template_id is not found in the registry.
        """
        profile = str(intent_data.get("analysis_profile") or "").strip()
        document_kind = str(intent_data.get("document_kind") or "").strip()
        template_hint = str(intent_data.get("template_hint") or "").strip()

        # If the LLM provided a template_hint, try it first
        if template_hint:
            try:
                registry = analyst_templates.get_analyst_templates_cached()
                template = analyst_templates.resolve_template(registry, template_hint)
                self._log.info(
                    "analyst template resolved from hint: %s", template_hint,
                )
                return template_hint, template
            except Exception:
                self._log.warning(
                    "analyst template_hint=%s not found, falling back to profile routing",
                    template_hint,
                )

        # Profile-based routing with cue-word adjustments
        template_id = build_template_from_profile(profile, document_kind, user_text)

        registry = analyst_templates.get_analyst_templates_cached()
        template = analyst_templates.resolve_template(registry, template_id)
        self._log.info(
            "analyst template resolved from profile routing: profile=%s kind=%s -> %s",
            profile,
            document_kind,
            template_id,
        )
        return template_id, template

    @staticmethod
    def _template_flag(template: Dict[str, Any], key: str) -> bool:
        value = (template or {}).get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _requires_repo_grounding(
        self,
        *,
        analysis_profile: str,
        template: Dict[str, Any],
    ) -> bool:
        return bool(
            str(analysis_profile or "").strip() in {"codebase", "codebase_plus_research", "audit"}
            or self._template_flag(template, "repo_grounded_required")
            or self._template_flag(template, "repo_audit_required")
            or self._template_flag(template, "final_repo_review_required")
        )

    @staticmethod
    def _normalize_gate_question(question: Any) -> str:
        return re.sub(r"\s+", " ", str(question or "").casefold()).strip()

    @staticmethod
    def _detect_codebase_map(*, project_root: str, workdir: str) -> bool:
        for base in (project_root, workdir):
            root = str(base or "").strip()
            if not root:
                continue
            arch_path = os.path.join(root, ".cli-proxy", ".codebase_map", "ARCHITECTURE.md")
            if os.path.isfile(arch_path):
                return True
        return False

    def _sync_analyst_progress_placeholder(
        self,
        *,
        run_dir: Any,
        status: str,
        summary: str,
    ) -> None:
        try:
            run_dir.sync_step_results(
                [
                    {
                        "task_id": "analyst_orchestrator",
                        "status": str(status or "pending"),
                        "summary": str(summary or "").strip(),
                        "title": "Выполнение analyst orchestrator",
                        "step_type": "orchestrator",
                    }
                ]
            )
        except Exception:
            self._log.exception(
                "analyst run artifacts: failed to sync placeholder step run=%s",
                getattr(run_dir, "run_id", ""),
            )

    def _filter_gate1_questions(
        self,
        *,
        analysis_profile: str,
        template: Dict[str, Any],
        clarification_questions: list[str],
        user_text: str,
    ) -> list[str]:
        cleaned = [
            str(question).strip()
            for question in (clarification_questions or [])
            if str(question or "").strip()
        ]
        return cleaned[:3]

    @staticmethod
    def _normalize_required_inputs(template: Dict[str, Any]) -> list[str]:
        return [
            str(item).strip()
            for item in ((template or {}).get("required_inputs") or [])
            if str(item).strip()
        ]

    def _build_session_intent_flags(
        self,
        *,
        intent_data: Dict[str, Any],
        template_id: str,
        template: Dict[str, Any],
    ) -> Dict[str, Any]:
        analysis_profile = str(intent_data.get("analysis_profile") or "").strip()
        document_kind = str(intent_data.get("document_kind") or "").strip().lower()
        clarification_questions = [
            str(item).strip()
            for item in (intent_data.get("clarification_questions") or [])
            if str(item).strip()
        ]
        return {
            "document_kind": document_kind,
            "needs_clarification": bool(clarification_questions),
            "requires_codebase_grounding": self._requires_repo_grounding(
                analysis_profile=analysis_profile,
                template=template,
            ),
            "requires_repo_audit": self._template_flag(template, "repo_audit_required"),
            "requires_final_repo_review": self._template_flag(template, "final_repo_review_required"),
            "clarification_is_blocking": bool(clarification_questions),
            "clarification_topic": clarification_questions[0] if len(clarification_questions) == 1 else "",
            "clarification_question": clarification_questions[0] if len(clarification_questions) == 1 else "",
            "clarification_options": [],
            "template_id": str(template_id or "").strip(),
            "required_inputs": self._normalize_required_inputs(template),
        }

    def _mark_clarification_resolved(
        self,
        *,
        session: Any,
        answers: list[str],
    ) -> None:
        normalized_answers: list[str] = []
        for item in answers or []:
            answer = str(item or "").strip()
            if answer and answer not in normalized_answers:
                normalized_answers.append(answer)
        try:
            store = self._store(session)
            analyst_ctx = self._load_context(session=session, store=store)
            existing_answers = [
                str(item).strip()
                for item in (analyst_ctx.clarification_answers or [])
                if str(item).strip()
            ]
            for answer in normalized_answers:
                if answer not in existing_answers:
                    existing_answers.append(answer)
            analyst_ctx.needs_clarification = False
            analyst_ctx.clarification_is_blocking = False
            analyst_ctx.clarification_topic = ""
            analyst_ctx.clarification_answers = existing_answers
            store.save(analyst_ctx)
        except Exception:
            self._log.exception(
                "analyst clarification state persist failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )
        raw_flags = getattr(session, "analyst_intent_flags", None)
        if not isinstance(raw_flags, dict):
            return
        updated_flags = dict(raw_flags)
        updated_flags["needs_clarification"] = False
        updated_flags["clarification_is_blocking"] = False
        updated_flags["clarification_topic"] = ""
        updated_flags["clarification_question"] = ""
        updated_flags["clarification_options"] = []
        if normalized_answers:
            existing_flag_answers = [
                str(item).strip()
                for item in (updated_flags.get("clarification_answers") or [])
                if str(item).strip()
            ]
            for answer in normalized_answers:
                if answer not in existing_flag_answers:
                    existing_flag_answers.append(answer)
            updated_flags["clarification_answers"] = existing_flag_answers
        try:
            setattr(session, "analyst_intent_flags", updated_flags)
        except Exception:
            self._log.exception(
                "analyst clarification state sync failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )

    def _clear_stale_session_intent_flags(self, session: Any) -> None:
        if not hasattr(session, "analyst_intent_flags"):
            return
        try:
            delattr(session, "analyst_intent_flags")
        except Exception:
            try:
                setattr(session, "analyst_intent_flags", None)
            except Exception:
                self._log.exception(
                    "analyst intent: failed to clear stale analyst_intent_flags session=%s",
                    str(getattr(session, "id", "") or ""),
                )

    def _persist_intent_context(
        self,
        *,
        session: Any,
        intent_data: Dict[str, Any],
        template_id: str,
        template: Dict[str, Any],
        user_text: str,
    ) -> None:
        """Persist resolved intent metadata for the analyst runtime template provider."""
        try:
            self._clear_stale_session_intent_flags(session)

            store = self._store(session)
            analyst_ctx = self._load_context(session=session, store=store)
            analysis_profile = str(intent_data.get("analysis_profile") or "").strip()
            document_kind = str(intent_data.get("document_kind") or "").strip()
            analyst_ctx.mode = "audit" if document_kind == "audit" else "spec"
            analyst_ctx.active_flow = ""
            analyst_ctx.runtime_template_id = str(getattr(session, "analyst_runtime_template_id", "") or "").strip()
            analyst_ctx.effective_template_id = str(template_id or "").strip()
            analyst_ctx.intent_reason = str(intent_data.get("summary") or "").strip()
            analyst_ctx.detail_level = str(intent_data.get("detail_level") or "").strip()
            analyst_ctx.document_kind = document_kind
            analyst_ctx.requires_codebase_grounding = bool(
                analysis_profile in {"codebase", "codebase_plus_research", "audit"}
                or self._template_flag(template, "repo_grounded_required")
            )
            analyst_ctx.requires_repo_audit = self._template_flag(template, "repo_audit_required")
            analyst_ctx.requires_final_repo_review = self._template_flag(template, "final_repo_review_required")
            analyst_ctx.needs_clarification = bool(intent_data.get("clarification_questions"))
            analyst_ctx.clarification_is_blocking = analyst_ctx.needs_clarification
            analyst_ctx.clarification_topic = ""
            analyst_ctx.source_user_text = str(user_text or "")
            analyst_ctx.clarification_answers = []
            store.save(analyst_ctx)
            setattr(
                session,
                "analyst_intent_flags",
                self._build_session_intent_flags(
                    intent_data=intent_data,
                    template_id=template_id,
                    template=template,
                ),
            )
        except Exception:
            self._log.exception(
                "analyst intent context persist failed session_id=%s template_id=%s",
                str(getattr(session, "id", "") or ""),
                str(template_id or ""),
            )

    async def _maybe_resolve_unfinished_analysis(
        self,
        *,
        session: Any,
        message: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        chat_id: int,
        ms: Any,
    ) -> bool:
        """Check for unfinished runs. Abandon them silently (pipeline does not support resume)."""
        from .run_directory import AnalystRunDirectory, resolve_analyst_runs_root

        base_path = resolve_analyst_runs_root(session)
        latest = AnalystRunDirectory.latest_run(
            base_path,
            session_id=str(getattr(session, "id", "") or ""),
        )
        if latest is None:
            return False
        meta = latest.load_meta()
        status = str(meta.get("status") or "").strip()
        if status in ("completed", "failed", ""):
            return False
        # Есть незавершённый прогон — просто сбрасываем его (не предлагаем продолжить,
        # т.к. pipeline не поддерживает resume из середины)
        latest.update_meta(status="abandoned")
        logger.info("analyst: abandoned unfinished run %s", meta.get("run_id", ""))
        return False  # False = продолжить с новым прогоном

    def _tool_ctx(self, *, session: Any, context: Any, dest: Dict[str, Any], bot_app: Any) -> Dict[str, Any]:
        return {
            "cwd": getattr(session, "workdir", None),
            "state_root": getattr(session, "workdir", None),
            "session_id": getattr(session, "id", None),
            "chat_id": dest.get("chat_id"),
            "chat_type": dest.get("chat_type"),
            "bot": bot_app,
            "context": context,
            "session": session,
            "allowed_tools": ["All"],
            "corr_id": f"analyst:{getattr(session, 'id', 'unknown')}:intent",
        }

    def _clear_persisted_last_draft(self, *, session: Any) -> None:
        try:
            store = self._store(session)
            analyst_ctx = self._load_context(session=session, store=store)
            has_draft = bool(str(getattr(analyst_ctx, "last_draft", "") or "").strip())
            has_timestamp = bool(float(getattr(analyst_ctx, "last_draft_updated_at", 0.0) or 0.0))
            if not has_draft and not has_timestamp:
                return
            analyst_ctx.last_draft = ""
            analyst_ctx.last_draft_updated_at = 0.0
            store.save(analyst_ctx)
        except Exception:
            self._log.exception(
                "analyst clear persisted last_draft failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )

    def _reset_state_on_enable(self, *, session: Any, bot_app: Any) -> None:
        reset_service = ModeScopedPreRunResetService(logger=self._log)
        try:
            was_reset = reset_service.apply(
                session=session,
                mode_id=self.mode_id,
                clear_runtime_cache=self._clear_runtime_cache_for_enable(bot_app),
                clear_pending_questions=self._clear_pending_questions_for_enable(bot_app),
            )
        except Exception:
            self._log.exception(
                "analyst enable reset failed session_id=%s",
                str(getattr(session, "id", "") or ""),
            )
            return
        if was_reset:
            self._persist_sessions(bot_app)

    def _clear_runtime_cache_for_enable(self, bot_app: Any) -> Callable[[str], None]:
        def _clear(session_token: str) -> None:
            for runtime in (getattr(bot_app, "iter_mode_runtimes", lambda: [])() or []):
                try:
                    if runtime and hasattr(runtime, "clear_session_cache"):
                        runtime.clear_session_cache(session_token)
                except Exception:
                    self._log.exception(
                        "analyst enable reset: clear runtime cache failed session_token=%s runtime=%s",
                        str(session_token or ""),
                        runtime.__class__.__name__,
                    )

        return _clear

    def _clear_pending_questions_for_enable(self, bot_app: Any) -> Callable[[str], int]:
        clear_fn = getattr(bot_app, "_clear_pending_questions", None)

        def _clear(session_token: str) -> int:
            token = str(session_token or "").strip()
            if not callable(clear_fn) or not token:
                return 0
            for kwargs in (
                {"session_id": token},
                {"session_uid": token},
            ):
                try:
                    return int(clear_fn(**kwargs) or 0)
                except TypeError:
                    continue
            try:
                return int(clear_fn(token) or 0)
            except TypeError:
                return 0

        return _clear

    async def _notify_clarification_fallback(
        self,
        *,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> None:
        try:
            chat_id = int(dest.get("chat_id") or 0)
        except Exception:
            chat_id = 0
        if chat_id <= 0:
            return
        if not self._has_messaging_factory():
            return
        try:
            ms = self._messaging(bot_app=bot_app, context=context)
        except Exception:
            self._log.exception("analyst clarification fallback messaging init failed")
            return
        try:
            await ms.send_text(
                chat_id,
                "Не удалось запросить уточнение. Продолжаю анализ по исходному запросу.",
                md2=True,
            )
        except Exception:
            self._log.exception("analyst clarification fallback notify failed")

    def _build_codebase_context(self, *, session: Any) -> str:
        prompts = self._load_prompts()
        text = CodebaseContextText(
            intro=str(
                prompts.get("codebase_intro")
                or (
                    "В проекте есть артефакты Codebase Mapper. Используй их как "
                    "навигационный индекс по репозиторию, затем проверяй вывод "
                    "по реальным файлам проекта:"
                )
            ),
            stack=str(
                prompts.get("codebase_stack")
                or "- `.cli-proxy/.codebase_map/STACK.md`: стек, языки, зависимости и toolchain."
            ),
            architecture=str(
                prompts.get("codebase_architecture")
                or "- `.cli-proxy/.codebase_map/ARCHITECTURE.md`: слои, модули, ключевые архитектурные зоны."
            ),
            structure=str(
                prompts.get("codebase_structure")
                or "- `.cli-proxy/.codebase_map/STRUCTURE.md`: структура каталогов и навигация по файлам."
            ),
            integrations=str(
                prompts.get("codebase_integrations")
                or "- `.cli-proxy/.codebase_map/INTEGRATIONS.md`: внешние интеграции, API/MCP/миниапп."
            ),
            conventions=str(
                prompts.get("codebase_conventions")
                or "- `.cli-proxy/.codebase_map/CONVENTIONS.md`: соглашения по коду и style-правила."
            ),
            testing=str(
                prompts.get("codebase_testing")
                or "- `.cli-proxy/.codebase_map/TESTING.md`: подход к тестам и test surface."
            ),
            concerns=str(
                prompts.get("codebase_concerns")
                or "- `.cli-proxy/.codebase_map/CONCERNS.md`: риски, техдолг и проблемные места."
            ),
            outro=str(
                prompts.get("codebase_outro")
                or "Если ответ требует деталей, явно опирайся на релевантные файлы карты."
            ),
        )
        return CodebaseContextService.build_context(
            session=session,
            runtime_getter=self._optional_runtime_getter(),
            text=text,
        )

    async def handle_callback(self, callback: CallbackModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        query = ctx.get("query")
        chat_id = self._normalize_callback_chat_id(callback.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        ms = self._messaging(bot_app=bot_app, context=context)
        prompts = self._load_prompts()
        action = str(callback.action or "").strip()
        callback_payload = dict(callback.payload or {})
        handlers = self._build_callback_handlers(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            ms=ms,
            prompts=prompts,
            payload=callback_payload,
        )
        dispatched = await self._dispatch_callback_action(action=action, handlers=handlers)
        if dispatched is not None:
            return dispatched
        return ToolResult.fail("unknown_action")

    def _build_callback_handlers(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        prompts: Dict[str, str],
        payload: Dict[str, Any],
    ) -> Dict[str, Callable[[], Awaitable[ToolResult]]]:
        return {
            "enable": lambda: self._cb_enable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                prompts=prompts,
            ),
            "on": lambda: self._cb_enable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                prompts=prompts,
            ),
            "disable": lambda: self._cb_disable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
            ),
            "off": lambda: self._cb_disable(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
            ),
            "template": lambda: self._cb_template(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                payload=payload,
            ),
            "status": lambda: self._cb_status(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
            ),
            "download": lambda: self._cb_download(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
            ),
            "audit": lambda: self._cb_audit(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                prompts=prompts,
            ),
        }

    async def _cb_enable(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        prompts: Dict[str, str],
    ) -> ToolResult:
        ok = await self._check_enable_requirements(
            bot_app=bot_app,
            session=session,
            ms=ms,
            query=query,
            chat_id=chat_id,
            require_openai=True,
            require_workdir=True,
            openai_error_text=str(
                prompts.get("openai_error_text")
                or "Для работы Аналитика нужен OpenAI API. Настройте openai_api_key и openai_model в config.yaml."
            ),
            workdir_error_text=str(
                prompts.get("workdir_error_text")
                or "Сначала создайте сессию через /sessions."
            ),
        )
        if not ok:
            return ToolResult.ok()
        self._reset_state_on_enable(session=session, bot_app=bot_app)
        self._clear_persisted_last_draft(session=session)
        await self._activate_mode(session=session, bot_app=bot_app, cli_work_type="analytics", executor_profile="analyst")
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="Аналитик включен.")
        return ToolResult.ok()

    async def _cb_disable(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        self._reset_audit_state(session=session)
        self._clear_audit_dirs_flow(chat_id=chat_id)
        await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="Аналитик выключен.")
        return ToolResult.ok()

    async def _cb_template(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        template_id = str((payload or {}).get("value") or "").strip()
        if not template_id:
            return ToolResult.fail("missing_template_id")
        try:
            registry = analyst_templates.get_analyst_templates_cached()
            resolved_id, _template = analyst_templates.resolve_template(registry, template_id, return_id=True)
        except Exception:
            self._log.exception("analyst template resolve failed template_id=%s", template_id)
            await ms.send_or_edit(query=query, chat_id=chat_id, text="Шаблон недоступен.", md2=True)
            return ToolResult.ok()
        resolved_template = str(resolved_id or "default")
        if hasattr(session, "modes") and getattr(session, "modes", None) is not None:
            try:
                session.modes.analyst_template_id = resolved_template
            except Exception:
                setattr(session, "analyst_template_id", resolved_template)
        else:
            setattr(session, "analyst_template_id", resolved_template)
        if hasattr(session, "analyst_template_id"):
            try:
                setattr(session, "analyst_template_id", resolved_template)
            except Exception:
                logger.exception("failed to set analyst_template_id on session fallback attribute")
        # analyst_mode is controlled by guided-flows (e.g. audit), not by template selection.
        self._persist_sessions(bot_app)
        selected_template = str(
            getattr(getattr(session, "modes", None), "analyst_template_id", getattr(session, "analyst_template_id", "default"))
            or "default"
        )
        await self._rerender_menu(bot_app, session, chat_id, context, query, note=f"Шаблон выбран: {selected_template}")
        return ToolResult.ok()

    async def _cb_status(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        running = bool(self._mode_task_names(bot_app=bot_app, session=session))
        analyst_ctx = self._load_context(session=session, store=self._store(session))
        text = build_analyst_status_text(
            session,
            analyst_context=analyst_ctx,
            analyst_running=running,
            pending_questions=bot_app.ui_state.pending_questions,
            mode_id=self.mode_id,
        )
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        return ToolResult.ok()

    async def _cb_download(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        analyst_ctx = self._load_context(session=session, store=self._store(session))
        draft_profile = resolve_draft_document_profile(session=session, analyst_context=analyst_ctx)
        draft_text = await self._build_draft_text(bot_app, session, chat_id=chat_id)
        if not draft_text:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=draft_profile["missing_text"],
                md2=True,
            )
            return ToolResult.ok()
        file_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix=f"{draft_profile['file_prefix']}{session.id}-",
                delete=False,
            ) as f:
                f.write(draft_text)
                file_path = f.name
            with open(file_path, "rb") as fdoc:
                ok = bool(await ms.send_doc(chat_id=chat_id, document=fdoc))
            if ok:
                await self._rerender_menu(
                    bot_app,
                    session,
                    chat_id,
                    context,
                    query,
                    note=draft_profile["sent_text"],
                )
            else:
                await self._rerender_menu(
                    bot_app,
                    session,
                    chat_id,
                    context,
                    query,
                    note=draft_profile["failed_text"],
                )
        finally:
            if file_path:
                try:
                    os.remove(file_path)
                except Exception:
                    self._log.exception("analyst download temp cleanup failed: %s", file_path)
        return ToolResult.ok()

    async def _cb_audit(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        prompts: Dict[str, str],
    ) -> ToolResult:
        if is_session_busy(session, getattr(session, "run_lock", None)):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="Сессия занята. Запуск аудита доступен только когда сессия свободна.",
                md2=True,
            )
            return ToolResult.ok()

        ok = await self._check_enable_requirements(
            bot_app=bot_app,
            session=session,
            ms=ms,
            query=query,
            chat_id=chat_id,
            require_openai=True,
            require_workdir=True,
            openai_error_text=str(
                prompts.get("audit_openai_error_text")
                or "Для работы Аудита нужен OpenAI API. Настройте openai_api_key и openai_model в config.yaml."
            ),
            workdir_error_text=str(
                prompts.get("workdir_error_text")
                or "Сначала создайте сессию через /sessions."
            ),
        )
        if not ok:
            return ToolResult.ok()
        store = self._store(session)
        analyst_ctx = self._load_context(session=session, store=store)
        analyst_ctx.mode = "awaiting_input"
        analyst_ctx.active_flow = "audit"
        store.save(analyst_ctx)
        base_root = session.workdir if not bot_app.is_admin(chat_id) else bot_app.config.defaults.workdir
        try:
            if query and getattr(query, "message", None):
                await ms.edit_text(
                    query.message.chat_id,
                    query.message.message_id,
                    str(prompts.get("audit_pick_path_hint") or "Выберите файл или папку для аудита."),
                    md2=True,
                )
            else:
                await ms.send_text(
                    chat_id,
                    str(prompts.get("audit_pick_path_hint") or "Выберите файл или папку для аудита."),
                    md2=True,
                )
        except Exception:
            self._log.exception("analyst audit: failed to send picker hint")
        dirs_flow = self._dirs_flow()
        try:
            await dirs_flow.start_flow(
                chat_id=chat_id,
                context=context,
                root=base_root,
                mode_token=encode_mode_dirs("analyst", "audit"),
            )
        except Exception:
            self._log.exception("analyst audit: dirs flow start failed")
            analyst_ctx.mode = "spec"
            analyst_ctx.active_flow = ""
            analyst_ctx.runtime_template_id = ""
            store.save(analyst_ctx)
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=(
                    str(prompts.get("audit_start_error_text") or "").strip()
                    or "Не удалось запустить выбор пути для аудита. Попробуйте снова."
                ),
                md2=True,
            )
        return ToolResult.ok()

    def _reset_audit_state(self, *, session: Any) -> None:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if workdir and os.path.isdir(workdir):
            try:
                store = self._store(session)
                analyst_ctx = self._load_context(session=session, store=store)
                analyst_ctx.mode = "spec"
                analyst_ctx.active_flow = ""
                analyst_ctx.runtime_template_id = ""
                store.save(analyst_ctx)
            except Exception:
                self._log.exception("analyst disable: failed to reset persisted audit context")

        if hasattr(session, "analyst_mode") or hasattr(session, "modes"):
            set_analyst_mode(session, "spec")
        if hasattr(session, "analyst_active_flow"):
            session.analyst_active_flow = ""
        if hasattr(session, "analyst_runtime_template_id"):
            session.analyst_runtime_template_id = ""

    def _clear_audit_dirs_flow(self, *, chat_id: int) -> None:
        if int(chat_id) <= 0:
            return
        dirs_flow = self._optional_dirs_flow()
        if dirs_flow is None or not hasattr(dirs_flow, "clear_flow"):
            return
        try:
            dirs_flow.clear_flow(chat_id=int(chat_id), mode_id=self.mode_id, flow="audit")
        except Exception:
            self._log.exception("analyst disable: failed to clear audit dirs-flow chat_id=%s", chat_id)

    async def handle_dirs_selection(self, *, flow: str, event: str, path: str, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        if str(flow or "").strip() != "audit":
            return None
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        chat_id = int(ctx.get("chat_id"))
        if not bot_app or not session:
            return ToolResult.fail("missing_context")
        store = self._store(session)
        analyst_ctx = self._load_context(session=session, store=store)
        event_name = str(event or "").strip().lower()
        if event_name in {"cancelled", "canceled", "cancel"}:
            analyst_ctx.mode = "spec"
            analyst_ctx.active_flow = ""
            analyst_ctx.runtime_template_id = ""
            store.save(analyst_ctx)
            return ToolResult.ok("Выбор пути для аудита отменен.")
        selected_path = str(path or "").strip()
        if not selected_path:
            analyst_ctx.mode = "spec"
            analyst_ctx.active_flow = ""
            analyst_ctx.runtime_template_id = ""
            store.save(analyst_ctx)
            return ToolResult.ok("Путь для аудита не выбран.")
        previous_analyst_mode = str(getattr(analyst_ctx, "mode", "spec") or "spec")
        restore_mode = "spec" if previous_analyst_mode == "awaiting_input" else previous_analyst_mode
        previous_runtime_override = str(getattr(session, "analyst_runtime_template_id", "") or "")
        analyst_ctx.mode = "audit"
        analyst_ctx.active_flow = "audit"
        analyst_ctx.runtime_template_id = "audit"
        if hasattr(session, "analyst_runtime_template_id"):
            session.analyst_runtime_template_id = "audit"
            self._persist_sessions(bot_app)
        store.save(analyst_ctx)
        prompt = f"Проанализировать содержимое по пути {selected_path} и составить отчет согласно шаблону Audit."
        dest = {"kind": "telegram", "chat_id": chat_id}

        async def _run_audit() -> None:
            await self._activate_mode(
                session=session,
                bot_app=bot_app,
                cli_work_type="analytics",
                executor_profile="analyst",
            )
            pipeline = self._pipeline()
            try:
                await pipeline.run_mode_pipeline(
                    session,
                    prompt,
                    dict(dest),
                    context,
                    mode_id=self.mode_id,
                )
            finally:
                final_ctx = self._load_context(session=session, store=store)
                final_ctx.mode = restore_mode or "spec"
                final_ctx.active_flow = ""
                final_ctx.runtime_template_id = ""
                store.save(final_ctx)
                if hasattr(session, "analyst_runtime_template_id"):
                    session.analyst_runtime_template_id = previous_runtime_override
                    self._persist_sessions(bot_app)

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run_audit(), name="audit")
        return ToolResult.ok(f"Запускаю аудит для пути: {selected_path}")

    async def _build_draft_text(self, bot_app: Any, session: Any, *, chat_id: int) -> str:
        analyst_ctx = self._load_context(session=session, store=self._store(session))
        return await build_draft_text(
            bot_app,
            session,
            chat_id=str(chat_id),
            analyst_context=analyst_ctx,
        )

    async def _rerender_menu(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any, *, note: str = "") -> None:
        await self._rerender_menu_common(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            note=note,
            back_callback="sess_active",
            back_text="⬅️ Назад",
        )

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
        menu_visibility: Any = None,
    ) -> tuple[str, Any]:
        analyst_ctx = self._load_context(session=session, store=self._store(session))
        return build_analyst_menu(
            session,
            back_callback=back_callback,
            back_text=back_text,
            analyst_context=analyst_ctx,
            mode_id=self.mode_id,
            menu_visibility=menu_visibility,
        )

    def _mode_root(self) -> str:
        return os.path.dirname(__file__)

    def _state_root(self, session: Any) -> str:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            self._log.warning("analyst session workdir missing; using cwd for state store")
        return resolve_analyst_state_root(session)

    def _store(self, session: Any) -> AnalystStateStore:
        return AnalystStateStore(self._state_root(session))

    def _context_key(self, session: Any) -> str:
        return session_scoped_key(session) or build_context_key(
            getattr(session, "chat_id", None),
            str(getattr(session, "id", "") or "").strip() or "default",
        )

    def _load_context(self, *, session: Any, store: Optional[AnalystStateStore] = None) -> AnalystContext:
        active_store = store or self._store(session)
        return active_store.load(self._context_key(session))

    def _artifact_store(self) -> Optional[RunArtifactStore]:
        config = getattr(self, "config", None)
        if config is None:
            return None
        return RunArtifactStore(config)

    def _is_run_artifacts_enabled(self) -> bool:
        service = self._optional_run_artifacts()
        if service is None:
            return False
        try:
            return bool(service.is_enabled())
        except Exception:
            self._log.exception("analyst run artifacts: failed to resolve enabled flag")
            return False

    @staticmethod
    def _prompt_hash(prompt: str) -> str:
        digest = hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _is_terminal_run_status(status: Any) -> bool:
        return is_terminal_status(status)

    def _diagnose_resume_boundary(self, run: RunArtifactHandle) -> Any:
        doctor = self._optional_run_doctor()
        if doctor is None or not doctor.is_enabled():
            return None
        artifact_store = self._artifact_store()
        try:
            state = artifact_store.load_state(run) if artifact_store is not None else {}
            phase = str((state or {}).get("phase") or "execute")
            return doctor.diagnose(run, mode_id=self.mode_id, phase=phase)
        except Exception:
            self._log.exception("analyst run artifacts: doctor resume diagnosis failed run_id=%s", run.run_id)
            return None

    def _save_run_state(
        self,
        run: Optional[RunArtifactHandle],
        *,
        phase: str,
        status: str,
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            current = artifact_store.load_state(run)
            merged_mode_context = dict(current.get("mode_context") or {})
            incoming_mode_context = dict(mode_context or {})
            existing_execution_context = merged_mode_context.get("execution_context")
            incoming_execution_context = incoming_mode_context.get("execution_context")
            if isinstance(existing_execution_context, dict) and isinstance(incoming_execution_context, dict):
                incoming_mode_context["execution_context"] = self._deep_merge_execution_context(
                    existing_execution_context,
                    incoming_execution_context,
                )
            merged_mode_context.update(incoming_mode_context)
            payload = dict(current)
            payload["phase"] = str(phase or payload.get("phase") or "intent")
            payload["status"] = str(status or payload.get("status") or "running")
            payload["mode_context"] = merged_mode_context
            artifact_store.save_state(run, payload)
        except Exception:
            self._log.exception("analyst run artifacts: save_state failed phase=%s run_id=%s", phase, run.run_id)

    @staticmethod
    def _deep_merge_execution_context(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(existing)
        for key, value in incoming.items():
            current_value = merged.get(key)
            if isinstance(current_value, dict) and isinstance(value, dict):
                merged[key] = AnalystMode._deep_merge_execution_context(current_value, value)
                continue
            merged[key] = value
        return merged

    def _save_run_plan(self, run: Optional[RunArtifactHandle], plan: Dict[str, Any]) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            artifact_store.save_plan(run, dict(plan or {}))
        except Exception:
            self._log.exception("analyst run artifacts: save_plan failed run_id=%s", run.run_id)

    def _load_analyst_quality_metrics(self, run: Optional[RunArtifactHandle]) -> Dict[str, Any]:
        if run is None:
            return {}
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return {}
        try:
            metrics = artifact_store.load_metrics(run)
        except Exception:
            self._log.exception("analyst run artifacts: load_metrics failed run_id=%s", run.run_id)
            return {}
        quality = metrics.get("analyst_quality") if isinstance(metrics, dict) else {}
        return dict(quality or {}) if isinstance(quality, dict) else {}

    def _build_quality_gate_warning_payload(
        self,
        *,
        run: Optional[RunArtifactHandle],
        quality: Dict[str, Any],
    ) -> Dict[str, Any]:
        review_payload = self._load_quality_gate_review_payload(run)
        verdict = str(
            quality.get("runtime_verdict")
            or quality.get("verdict")
            or review_payload.get("verdict")
            or "Не готово к реализации"
        ).strip() or "Не готово к реализации"
        blocking_reasons = self._quality_gate_blocking_reasons(
            quality=quality,
            review_payload=review_payload,
        )
        correction_items = [
            str(item).strip()
            for item in (review_payload.get("required_corrections") or [])
            if str(item).strip()
        ]
        return {
            "runtime_verdict": verdict,
            "blocking_reasons": blocking_reasons,
            "required_corrections": correction_items,
            "artifact_path": str(review_payload.get("_path") or ""),
        }

    def _load_quality_gate_review_payload(self, run: Optional[RunArtifactHandle]) -> Dict[str, Any]:
        if run is None:
            return {}
        artifacts_dir = str(getattr(run, "artifacts_dir", "") or "").strip()
        if not artifacts_dir or not os.path.isdir(artifacts_dir):
            return {}
        candidates: list[tuple[int, str]] = []
        for name in os.listdir(artifacts_dir):
            lower = str(name or "").strip().lower()
            if not lower.endswith(".json"):
                continue
            score = 0
            if "obligation" in lower:
                score += 3
            if "review" in lower:
                score += 2
            if "followup" in lower:
                score += 2
            if score <= 0:
                continue
            candidates.append((score, name))
        for _score, name in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True):
            path = os.path.join(artifacts_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = loads_safe(fh.read())
            except Exception:
                self._log.exception("analyst quality gate: failed to load review artifact path=%s", path)
                continue
            if isinstance(payload, dict) and any(
                payload.get(key) is not None
                for key in ("final_text", "open_blocking_obligations", "required_corrections", "verdict")
            ):
                enriched = dict(payload)
                enriched["_path"] = path
                return enriched
        return {}

    @staticmethod
    def _quality_gate_blocking_reasons(
        *,
        quality: Dict[str, Any],
        review_payload: Dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        seen: set[str] = set()

        def _append(value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in seen:
                return
            reasons.append(text)
            seen.add(text)

        for item in (quality.get("blocking_reasons") or []):
            _append(item)
        for item in (review_payload.get("open_blocking_obligations") or []):
            if isinstance(item, dict):
                _append(item.get("statement"))
            else:
                _append(item)
        return reasons

    async def _deliver_analyst_output(
        self,
        *,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        output: str,
    ) -> None:
        text = _sanitize_final_analyst_output(output)
        if not text:
            return
        send_output = getattr(bot_app, "send_output", None)
        if callable(send_output):
            try:
                await send_output(session, dict(dest or {}), text, context, send_header=False)
                return
            except Exception:
                self._log.exception("analyst delivery via send_output failed")
        chat_id = (dest or {}).get("chat_id")
        send_message = getattr(bot_app, "_send_message", None)
        if not callable(send_message) or chat_id is None:
            return
        kwargs: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        message_thread_id = (dest or {}).get("message_thread_id")
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id
        try:
            await send_message(context, **kwargs)
        except Exception:
            self._log.exception("analyst delivery fallback via _send_message failed")

    def _sync_analyst_step_artifacts(
        self,
        *,
        session: Any,
        run_dir: Any,
        run_handle: Optional[RunArtifactHandle],
    ) -> Dict[str, Any]:
        session_workdir = str(getattr(session, "workdir", "") or "").strip()
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_workdir or not session_id:
            return self._load_existing_step_evidence(run_dir=run_dir)
        session_log_path = os.path.join(session_workdir, "SESSION.json")
        payload = read_json_locked(session_log_path, default={})
        if not isinstance(payload, dict):
            return self._load_existing_step_evidence(run_dir=run_dir)
        orchestrator_runs = payload.get("orchestrator_by_task") or {}
        if not isinstance(orchestrator_runs, dict):
            return self._load_existing_step_evidence(run_dir=run_dir)
        latest_entries = orchestrator_runs.get(session_id) or []
        if not isinstance(latest_entries, list) or not latest_entries:
            return self._load_existing_step_evidence(run_dir=run_dir)
        last_entry = next((item for item in reversed(latest_entries) if isinstance(item, dict)), None)
        if not isinstance(last_entry, dict):
            return self._load_existing_step_evidence(run_dir=run_dir)
        step_results = last_entry.get("step_results") or []
        evidence_summary = self._sync_analyst_step_results(
            run_dir=run_dir,
            run_handle=run_handle,
            step_results=step_results,
        )
        return evidence_summary or self._load_existing_step_evidence(run_dir=run_dir)

    def _sync_analyst_step_results(
        self,
        *,
        run_dir: Any,
        run_handle: Optional[RunArtifactHandle],
        step_results: Any,
    ) -> Dict[str, Any]:
        if not isinstance(step_results, list) or not step_results:
            return {}
        evidence_summary = run_dir.sync_step_results(step_results)
        if run_handle is not None:
            artifact_store = self._artifact_store()
            if artifact_store is not None:
                try:
                    current_metrics = artifact_store.load_metrics(run_handle)
                    payload = dict(current_metrics or {}) if isinstance(current_metrics, dict) else {}
                    payload["analyst_evidence_trail"] = evidence_summary
                    quality = payload.get("analyst_quality")
                    if isinstance(quality, dict):
                        warning_reasons = [
                            str(item).strip()
                            for item in (quality.get("warning_reasons") or [])
                            if str(item).strip()
                        ]
                        for item in evidence_summary.get("warning_reasons") or []:
                            text = str(item).strip()
                            if text and text not in warning_reasons:
                                warning_reasons.append(text)
                        if warning_reasons:
                            quality["warning_reasons"] = warning_reasons
                    artifact_store.save_metrics(run_handle, payload)
                except Exception:
                    self._log.exception(
                        "analyst run artifacts: save evidence trail metrics failed run_id=%s",
                        run_handle.run_id,
                    )
        return evidence_summary

    def _load_existing_step_evidence(self, *, run_dir: Any) -> Dict[str, Any]:
        try:
            meta = run_dir.load_meta()
        except Exception:
            self._log.exception("analyst run artifacts: failed to load existing step evidence")
            return {}
        steps = meta.get("steps") if isinstance(meta, dict) else []
        if not isinstance(steps, list):
            return {}
        has_real_steps = any(
            isinstance(item, dict)
            and str(item.get("id") or "").strip()
            and str(item.get("id") or "").strip() != "analyst_orchestrator"
            for item in steps
        )
        if not has_real_steps:
            return {}
        evidence = meta.get("evidence_trail") if isinstance(meta, dict) else {}
        return dict(evidence or {}) if isinstance(evidence, dict) else {}

    def _validate_run_boundary(self, run: Optional[RunArtifactHandle], *, phase: str) -> None:
        if run is None:
            return
        validator = self._optional_run_boundary_validation()
        if validator is None or not validator.is_enabled():
            return
        report = validator.validate(run, mode_id=self.mode_id, phase=phase)
        if str(report.status or "") == "ok":
            return
        if str(phase or "").strip() == "complete" and report.issues and all(
            str(issue.code or "") == "analyst_quality_gate_not_passed"
            for issue in report.issues
        ):
            self._log.warning(
                "analyst run boundary quality gate warning is non-blocking run_id=%s",
                run.run_id,
            )
            return
        issues = ", ".join(issue.code for issue in report.issues)
        raise RuntimeError(f"Analyst run boundary validation failed phase={phase}: {issues}")

    def _ensure_execute_boundary_evidence(self, run: Optional[RunArtifactHandle], *, output: Any) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            checkpoints = read_json_locked(run.checkpoints_path, default={})
            checkpoint_items = checkpoints.get("items") if isinstance(checkpoints, dict) else []
            if isinstance(checkpoint_items, list) and checkpoint_items:
                return
            metrics = read_json_locked(run.metrics_path, default={})
            metric_units = metrics.get("units") if isinstance(metrics, dict) else []
            if isinstance(metric_units, list) and metric_units:
                return
            artifact_store.append_checkpoint(
                run,
                {
                    "phase": "execute",
                    "unit_id": "analyst:execute",
                    "status": "ok",
                    "message": str(output or "")[:500],
                    "synthetic": True,
                },
            )
        except Exception:
            self._log.exception("analyst run artifacts: failed to ensure execute boundary evidence run_id=%s", run.run_id)

    def _mark_run_finished(self, run: Optional[RunArtifactHandle], *, status: str, phase: str) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            artifact_store.mark_finished(run, status=status, phase=phase)
        except Exception:
            self._log.exception("analyst run artifacts: mark_finished failed run_id=%s", run.run_id)

    def _prepare_run_artifacts(
        self,
        *,
        session: Any,
        user_text: str,
        dest: Dict[str, Any],
    ) -> tuple[Optional[RunArtifactHandle], Dict[str, Any]]:
        self._clear_active_run_handle(session)
        if not self._is_run_artifacts_enabled():
            return None, {}
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None, {}

        resume_guard: Dict[str, Any] = {}
        latest = self._latest_mode_run(session)
        if latest is not None:
            latest_state = artifact_store.load_state(latest)
            if not self._is_terminal_run_status(latest_state.get("status")):
                report = self._diagnose_resume_boundary(latest)
                if report is not None:
                    resume_guard = {
                        "previous_run_id": latest.run_id,
                        "report": report.to_dict(),
                    }
                artifact_store.mark_finished(
                    latest,
                    status="failed",
                    phase=str(latest_state.get("phase") or "execute"),
                )
                resume_guard["previous_run_repaired"] = True

        run = artifact_store.start_run(
            session=session,
            mode_id=self.mode_id,
            phase="intent",
            source_prompt_hash=self._prompt_hash(user_text),
            mode_context={
                "dest_kind": str((dest or {}).get("kind") or "telegram"),
                "run_scope": "mode_pipeline",
                "analyst_context_key": self._context_key(session),
                "resume_guard": dict(resume_guard or {}),
                "execution_context": self._execution_context(session=session, dest=dest, user_text=user_text),
            },
        )
        self._set_active_run_handle(session, run)
        try:
            setattr(session, _ANALYST_RUN_RESUME_GUARD_SESSION_ATTR, dict(resume_guard or {}))
        except Exception:
            self._log.exception("analyst run artifacts: failed to set resume guard session attr")
        return run, resume_guard

    @staticmethod
    def _set_active_run_handle(session: Any, run: RunArtifactHandle) -> None:
        setattr(session, _ANALYST_RUN_HANDLE_SESSION_ATTR, run)

    @staticmethod
    def _clear_active_run_handle(session: Any) -> None:
        if hasattr(session, _ANALYST_RUN_HANDLE_SESSION_ATTR):
            setattr(session, _ANALYST_RUN_HANDLE_SESSION_ATTR, None)
        if hasattr(session, _ANALYST_RUN_RESUME_GUARD_SESSION_ATTR):
            setattr(session, _ANALYST_RUN_RESUME_GUARD_SESSION_ATTR, {})
        if hasattr(session, _ANALYST_FINAL_CANDIDATE_PATH_SESSION_ATTR):
            setattr(session, _ANALYST_FINAL_CANDIDATE_PATH_SESSION_ATTR, "")

    @staticmethod
    def _set_step_results_sync_hook(session: Any, hook: Callable[[list[dict[str, Any]]], Dict[str, Any]]) -> None:
        setattr(session, _ANALYST_STEP_RESULTS_SYNC_HOOK_SESSION_ATTR, hook)

    @staticmethod
    def _clear_step_results_sync_hook(session: Any) -> None:
        if not hasattr(session, _ANALYST_STEP_RESULTS_SYNC_HOOK_SESSION_ATTR):
            return
        try:
            delattr(session, _ANALYST_STEP_RESULTS_SYNC_HOOK_SESSION_ATTR)
        except Exception:
            logger.exception("analyst run artifacts: failed to clear step-results sync hook")
            setattr(session, _ANALYST_STEP_RESULTS_SYNC_HOOK_SESSION_ATTR, None)

    def _execution_context(self, *, session: Any, dest: Dict[str, Any], user_text: str) -> Dict[str, Any]:
        return {
            "dest_kind": str((dest or {}).get("kind") or "telegram"),
            "chat_id": (dest or {}).get("chat_id"),
            "project_root": str(getattr(session, "project_root", "") or "").strip() or None,
            "workdir": str(getattr(session, "workdir", "") or "").strip() or None,
            "session_scoped_key": self._context_key(session) or None,
            "source_user_text": str(user_text or ""),
            "user_text_preview": str(user_text or "")[:500],
        }

    def _latest_mode_run(self, session: Any) -> Optional[RunArtifactHandle]:
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None
        latest = artifact_store.latest_run(session=session, mode_id=self.mode_id)
        if latest is not None and self._is_top_level_mode_run(artifact_store.load_state(latest)):
            return latest
        try:
            root_dir = artifact_store._resolve_root_dir(session)
            session_uid = artifact_store._resolve_session_uid(session)
            mode_root = os.path.join(root_dir, ".cli-proxy", "runs", session_uid, self.mode_id)
            if not os.path.isdir(mode_root):
                return None
            candidates = sorted(
                [
                    name
                    for name in os.listdir(mode_root)
                    if os.path.isdir(os.path.join(mode_root, name))
                    and os.path.exists(os.path.join(mode_root, name, "STATE.json"))
                ],
                reverse=True,
            )
            for run_id in candidates:
                handle = artifact_store._build_handle(session=session, mode_id=self.mode_id, run_id=run_id)
                state = artifact_store.load_state(handle)
                if self._is_top_level_mode_run(state):
                    return handle
        except Exception:
            self._log.exception("analyst run artifacts: latest top-level run lookup failed")
        return None

    @staticmethod
    def _is_top_level_mode_run(state: Dict[str, Any]) -> bool:
        mode_context = state.get("mode_context") if isinstance(state, dict) else {}
        if not isinstance(mode_context, dict):
            return False
        return str(mode_context.get("run_scope") or "").strip() == "mode_pipeline"

    def _load_prompts(self) -> Dict[str, str]:
        if self._prompts:
            return self._prompts
        path = os.path.join(self._mode_root(), "prompts.yaml")
        raw: Dict[str, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            self._log.exception("analyst prompts read failed: %s", path)
            raw = {}
        prompts = raw.get("prompts") if isinstance(raw, dict) else {}
        if not isinstance(prompts, dict):
            prompts = {}
        self._prompts = {str(k): str(v) for k, v in prompts.items()}
        return self._prompts
