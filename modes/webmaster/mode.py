from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from app.mode_dependencies import ModeDependencies
from app.services.project_prompts_service import (
    InvalidProjectPromptsError,
    ensure_project_prompts,
    load_mode_learning,
    load_mode_prompt_texts,
    save_mode_learning,
)
from app.services.run_artifact_store import RunArtifactHandle

from modes.sdk.runtime.openai_client import chat_completion
from modes.sdk.runtime.cli_contracts import CLIResponseFormat, wrap_prompt_for_response_format
from modes.sdk.runtime.json_normalizer import loads_safe, parse_normalize_validate
from modes.sdk import BaseMode, CallbackModel, MessageModel, MessagingService, ToolResult
from modes.sdk.run_artifacts_mixin import MergeStrategy, RunArtifactsMixin
from modes.sdk.services import ModeStatusService
from sessions.session_state_access import get_active_mode
from utils.paths import cli_proxy_artifact_path

from .feedback_optimizer import apply_prompt_learning, normalize_general_patch, normalize_learning_payload
from .intent_service import IntentService
from .models import FeedbackDecision, ValidationDecision, WebmasterContext
from .schemas import WebmasterValidationReportSchema
from .state_store import WebmasterStateStore, build_user_key


class _InvalidConfirmationSelection(RuntimeError):
    """Raised when ask_user returns a non-button value."""


class WebmasterMode(BaseMode, RunArtifactsMixin):
    _RUN_HANDLE_SESSION_ATTR = "_webmaster_run_handle"

    mode_id = "webmaster"
    display_name = "🌐 Вебмастер"
    description = "Управление web-сайтом через use_cli с подтверждением намерений"

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)
        self._log = logging.getLogger(__name__)
        self._intent = IntentService(self)
        self._invalid_prompts_enable_text = (
            "❌ Не удалось включить Вебмастер: повреждён файл project prompts "
            "`.cli-proxy/.webmaster/prompt/prompts.yaml`. "
            "Исправьте YAML и повторите включение."
        )

    def build_runtime(self, config: Any) -> Any:
        from .runner_service import WebmasterModeRunnerService
        return WebmasterModeRunnerService(config)

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app:
            context = ctx.get("context")
            query = ctx.get("query")
            raw_chat_id = (
                ctx.get("chat_id")
                or (ctx.get("dest") or {}).get("chat_id")
                or getattr(getattr(query, "message", None), "chat_id", None)
            )
            try:
                chat_id = int(raw_chat_id) if raw_chat_id is not None else None
            except (TypeError, ValueError):
                chat_id = None
            prompts_ok = await self._ensure_project_prompts_ready(
                session=session,
                bot_app=bot_app,
                context=context,
                chat_id=chat_id,
                query=query,
            )
            if not prompts_ok:
                return ToolResult.fail("invalid_project_prompts")
            await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile=None)
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app and self._is_mode_active(session):
            await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        return None

    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        chat_id = self._normalize_callback_chat_id(message.chat_id)
        msg_user_id = int(message.user_id) if getattr(message, "user_id", None) is not None else None
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        dest = self._normalize_dest(ctx_dest=ctx.get("dest"), chat_id=chat_id, user_id=msg_user_id)
        if dest.get("kind") == "telegram":
            dest["chat_id"] = int(dest.get("chat_id") or chat_id)
            resolved_user_id = self._resolve_user_id(
                user_id=dest.get("user_id"),
                chat_id=int(dest.get("chat_id") or chat_id),
                chat_type=str(dest.get("chat_type") or ""),
                context=context,
                session=session,
            )
            if resolved_user_id is not None:
                dest["user_id"] = resolved_user_id

        ms = self._messaging(bot_app=bot_app, context=context)
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

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="run_webmaster")
        return ToolResult.ok()

    async def handle_callback(self, callback: CallbackModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        query = ctx.get("query")
        chat_id = self._normalize_callback_chat_id(callback.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        ms = self._messaging(bot_app=bot_app, context=context)
        action = str(callback.action or "").strip()
        handlers = {
            "menu": lambda: self._cb_menu(bot_app, session, chat_id, context, query),
            "open": lambda: self._cb_menu(bot_app, session, chat_id, context, query),
            "show": lambda: self._cb_menu(bot_app, session, chat_id, context, query),
            "enable": lambda: self._cb_enable(bot_app, session, chat_id, context, query),
            "on": lambda: self._cb_enable(bot_app, session, chat_id, context, query),
            "disable": lambda: self._cb_disable(bot_app, session, chat_id, context, query),
            "off": lambda: self._cb_disable(bot_app, session, chat_id, context, query),
            "status": lambda: self._cb_status(bot_app, session, chat_id, callback.user_id, context, ms, query),
            "reset": lambda: self._cb_reset(bot_app, session, chat_id, callback.user_id, context, ms, query),
        }
        dispatched = await self._dispatch_callback_action(action=action, handlers=handlers)
        if dispatched is not None:
            return dispatched
        return ToolResult.fail("unknown_action")

    async def _cb_menu(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any) -> ToolResult:
        await self._rerender_menu(bot_app, session, chat_id, context, query)
        return ToolResult.ok()

    async def _cb_enable(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any) -> ToolResult:
        ms = self._messaging(bot_app=bot_app, context=context)
        ok = await self._check_enable_requirements(
            bot_app=bot_app,
            session=session,
            ms=ms,
            query=query,
            chat_id=chat_id,
            require_openai=True,
            require_workdir=True,
            openai_error_text=(
                "Для работы Вебмастера нужен OpenAI API. "
                "Настройте openai_api_key и openai_model в config.yaml."
            ),
            workdir_error_text="Сначала создайте сессию через /sessions.",
        )
        if not ok:
            return ToolResult.ok()
        prompts_ok = await self._ensure_project_prompts_ready(
            session=session,
            bot_app=bot_app,
            context=context,
            chat_id=chat_id,
            query=query,
        )
        if not prompts_ok:
            return ToolResult.ok()
        await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile=None)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="Вебмастер включен.")
        return ToolResult.ok()

    async def _cb_disable(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any) -> ToolResult:
        await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="Вебмастер выключен.")
        return ToolResult.ok()

    async def _cb_status(
        self,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        ms: MessagingService,
        query: Any,
    ) -> ToolResult:
        text = self._build_status_text(bot_app, session, chat_id, user_id, context=context)
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        return ToolResult.ok()

    async def _cb_reset(
        self,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        context: Any,
        ms: MessagingService,
        query: Any,
    ) -> ToolResult:
        store = self._store(session)
        resolved_user_id = self._resolve_user_id(
            user_id=user_id,
            chat_id=chat_id,
            chat_type="",
            context=context,
            session=session,
        )
        key = build_user_key(chat_id, resolved_user_id, str(getattr(session, "id", "") or "").strip() or None)
        store.reset(key)
        await self._rerender_menu(
            bot_app,
            session,
            chat_id,
            context,
            query,
            note="Контекст Вебмастера сброшен для текущего пользователя.",
        )
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
        try:
            chat_id = int(dest.get("chat_id") or 0)
        except (TypeError, ValueError):
            chat_id = 0
        resolved_user_id = self._resolve_user_id(
            user_id=dest.get("user_id"),
            chat_id=chat_id,
            chat_type=str(dest.get("chat_type") or ""),
            context=context,
            session=session,
        )
        if resolved_user_id is not None:
            dest["user_id"] = resolved_user_id
        key = build_user_key(chat_id, resolved_user_id, str(getattr(session, "id", "") or "").strip() or None)
        store = self._store(session)
        wm_ctx = store.load(key)
        wm_ctx.last_user_text = str(user_text or "")
        run = self._restore_active_run_for_resume(
            session=session,
            webmaster_user_key=key,
            wm_ctx=wm_ctx,
        )
        try:
            resume_out = await self._resume_interrupted_pipeline(
                session=session,
                bot_app=bot_app,
                context=context,
                dest=dest,
                store=store,
                wm_ctx=wm_ctx,
                run=run,
            )
            if resume_out is not None:
                return resume_out

            run = self._prepare_run_artifacts(
                session=session,
                user_text=user_text,
                dest=dest,
                webmaster_user_key=key,
                wm_ctx=wm_ctx,
            )
            self._save_run_state(
                run,
                phase="intent",
                status="running",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                ),
            )

            try:
                feedback = await self._classify_feedback_llm(bot_app, user_text, wm_ctx, session=session)
            except Exception:
                self._log.exception("webmaster classify feedback failed")
                wm_ctx.stage = "await_intent_update"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        runtime_error="classify_feedback_failed",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Не удалось обработать обратную связь. Уточните задачу и попробуем снова.",
                    checkpoint_label="before_response_feedback_error",
                )
            wm_ctx.last_feedback_class = feedback.kind
            feedback_kind = str(feedback.kind or "").strip()
            initial_stage = str(wm_ctx.stage or "").strip()
            has_resume_context = bool(
                str(getattr(wm_ctx, "last_cli_report", "") or "").strip()
                or str(getattr(wm_ctx, "last_cli_task", "") or "").strip()
            )
            force_new_task = (
                feedback_kind == "continue_task"
                and initial_stage in {"idle", "await_intent_update"}
                and not has_resume_context
            )
            if feedback_kind == "unclear":
                wm_ctx.stage = "await_intent_update"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="completed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                    ),
                )
                self._mark_run_finished(run, status="completed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Запрос пока неясен. Уточните цель и ожидаемый результат одним сообщением.",
                    checkpoint_label="before_response_unclear",
                )
            if feedback_kind in ("new_task", "requirement_change") or force_new_task:
                wm_ctx = store.reset(key)
                wm_ctx.task_kind = "new_task"
                wm_ctx.last_feedback_class = feedback.kind
                wm_ctx.last_user_text = str(user_text or "")
            else:
                wm_ctx.task_kind = "continue_task"

            if feedback.kind == "wrong_execution" and wm_ctx.last_cli_report:
                try:
                    learning = self._load_prompt_learning(session=session)
                    patch = await self._build_prompt_patch_llm(
                        bot_app,
                        user_text,
                        wm_ctx.last_cli_report,
                        session=session,
                    )
                    if patch is None:
                        raise RuntimeError("webmaster prompt patch candidate has no generalized rules")
                    patches = learning.get("patches") if isinstance(learning.get("patches"), list) else []
                    patches.append(patch)
                    learning["patches"] = patches
                    learning = await self._maybe_compact_prompt_learning(bot_app, learning, session=session)
                    learning["active_version"] = int(learning.get("active_version", 1) or 1) + 1
                    self._save_prompt_learning(learning, session=session)
                    wm_ctx.prompt_patches = [
                        x for x in (learning.get("patches") or []) if isinstance(x, dict)
                    ]
                    wm_ctx.active_prompt_version = int(learning["active_version"])
                except Exception:
                    self._log.exception("webmaster prompt learning update failed")

            try:
                intent_data = await self._analyze_intent(
                    bot_app=bot_app,
                    session=session,
                    context=context,
                    dest=dest,
                    user_text=user_text,
                    wm_ctx=wm_ctx,
                )
            except Exception:
                self._log.exception("webmaster analyze intent failed")
                wm_ctx.stage = "await_intent_update"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        runtime_error="analyze_intent_failed",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Не удалось разобрать намерение. Уточните задачу одним сообщением и попробуем снова.",
                    checkpoint_label="before_response_intent_error",
                )
            wm_ctx.goal = str(intent_data.get("goal") or user_text).strip()
            wm_ctx.actions = [str(x).strip() for x in (intent_data.get("actions") or []) if str(x).strip()]
            wm_ctx.constraints = [str(x).strip() for x in (intent_data.get("constraints") or []) if str(x).strip()]
            wm_ctx.acceptance_criteria = [
                str(x).strip() for x in (intent_data.get("acceptance_criteria") or []) if str(x).strip()
            ]
            wm_ctx.ambiguities = [str(x).strip() for x in (intent_data.get("ambiguities") or []) if str(x).strip()]
            wm_ctx.assumptions = [str(x).strip() for x in (intent_data.get("assumptions") or []) if str(x).strip()]
            wm_ctx.stage = "await_user_confirmation"
            wm_ctx.confirmation_attempts += 1
            store.save(wm_ctx)

            intent_payload = {
                "task_kind": str(wm_ctx.task_kind or "").strip() or "new_task",
                "goal": str(wm_ctx.goal or "").strip(),
                "actions": list(wm_ctx.actions or []),
                "constraints": list(wm_ctx.constraints or []),
                "acceptance_criteria": list(wm_ctx.acceptance_criteria or []),
                "ambiguities": list(wm_ctx.ambiguities or []),
                "assumptions": list(wm_ctx.assumptions or []),
            }
            self._save_run_plan(
                run,
                {
                    "kind": "webmaster_pipeline",
                    "goal": str(wm_ctx.goal or "").strip(),
                    "task_kind": str(wm_ctx.task_kind or "").strip() or "new_task",
                    "units": [
                        {
                            "id": "webmaster:dev",
                            "step_type": "dev",
                            "title": "Run webmaster implementation loop",
                        },
                        {
                            "id": "webmaster:validation",
                            "step_type": "validation",
                            "title": "Validate webmaster implementation loop",
                        },
                    ],
                    "boundary_map": [
                        {"run_phase": "intent"},
                        {"run_phase": "dev"},
                        {"run_phase": "validation"},
                        {"run_phase": "complete"},
                    ],
                    "validation_contracts": ["legacy_webmaster_gate"],
                },
            )
            self._save_run_state(
                run,
                phase="intent",
                status="running",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    intent_payload=intent_payload,
                ),
            )
            self._validate_run_boundary(run, phase="intent")

            try:
                confirmation = await self._confirm_intent(bot_app, session, context, dest, wm_ctx)
            except _InvalidConfirmationSelection:
                self._log.exception("webmaster confirm intent invalid selection")
                wm_ctx.stage = "await_user_confirmation"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        intent_payload=intent_payload,
                        runtime_error="confirm_intent_invalid_selection",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Нужно выбрать один из вариантов кнопкой: Подтвердить, Уточнить или Новая задача.",
                    checkpoint_label="before_response_confirm_invalid",
                )
            except Exception:
                self._log.exception("webmaster confirm intent failed")
                wm_ctx.stage = "await_intent_update"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        intent_payload=intent_payload,
                        runtime_error="confirm_intent_failed",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Не удалось запросить подтверждение. Уточните задачу и повторим.",
                    checkpoint_label="before_response_confirm_error",
                )
            if confirmation == "Новая задача":
                store.reset(key)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="completed",
                    mode_context={
                        "webmaster_user_key": key,
                        "task_kind": "new_task",
                        "intent_payload": dict(intent_payload),
                    },
                )
                self._mark_run_finished(run, status="completed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Контекст очищен. Пришлите новую задачу.",
                    checkpoint_label="before_response_new_task",
                )
            if confirmation == "Уточнить":
                wm_ctx.stage = "await_intent_update"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="completed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        intent_payload=intent_payload,
                    ),
                )
                self._mark_run_finished(run, status="completed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Пришлите уточнение текстом, я переформулирую намерения и снова попрошу подтверждение.",
                    checkpoint_label="before_response_refine",
                )
            if confirmation != "Подтвердить":
                self._log.error("webmaster confirm intent returned unexpected option: %s", confirmation)
                wm_ctx.stage = "await_intent_update"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="intent",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        intent_payload=intent_payload,
                        runtime_error=f"unexpected_confirmation:{confirmation}",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="intent")
                return await self._finalize_pipeline_response(
                    session,
                    "Не удалось подтвердить намерение. Уточните задачу и повторим.",
                    checkpoint_label="before_response_confirm_unexpected",
                )

            dev_task_text = self._build_cli_task(wm_ctx, session=session)
            await self._silent_git_checkpoint(session, "before_start")
            return await self._run_dev_validation_loop(
                session=session,
                bot_app=bot_app,
                context=context,
                dest=dest,
                store=store,
                wm_ctx=wm_ctx,
                run=run,
                feedback_kind=str(feedback.kind or "").strip() or "continue_task",
                dev_task_text=dev_task_text,
                fresh_run=(str(wm_ctx.task_kind or "").strip() == "new_task"),
                fix_iteration=0,
                resumed_cli_output=None,
            )
        except Exception as exc:
            phase = self._phase_for_stage(getattr(wm_ctx, "stage", "") or "intent")
            self._save_run_state(
                run,
                phase=phase,
                status="failed",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    runtime_error=str(exc or ""),
                ),
            )
            self._mark_run_finished(run, status="failed", phase=phase)
            raise
        finally:
            self._clear_active_run_handle(session)

    async def _resume_interrupted_pipeline(
        self,
        *,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        store: WebmasterStateStore,
        wm_ctx: WebmasterContext,
        run: Optional[RunArtifactHandle],
    ) -> Optional[str]:
        stage = str(wm_ctx.stage or "").strip()
        if stage not in {"running_dev_cli", "running_validation_cli"}:
            return None

        now_ts = time.time()
        ttl_sec = self._resume_ttl_sec(bot_app)
        updated_at = float(getattr(wm_ctx, "updated_at", 0.0) or 0.0)
        age_sec = max(0, int(now_ts - updated_at)) if updated_at > 0 else ttl_sec + 1
        if age_sec > ttl_sec:
            wm_ctx.stage = "idle"
            wm_ctx.task_kind = "new_task"
            wm_ctx.fix_iteration_count = 0
            wm_ctx.last_feedback_class = "resume_expired"
            store.save(wm_ctx)
            self._save_run_state(
                run,
                phase=self._phase_for_stage(stage),
                status="failed",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    runtime_error="resume_expired",
                ),
            )
            self._mark_run_finished(run, status="failed", phase=self._phase_for_stage(stage))
            return await self._finalize_pipeline_response(
                session,
                (
                    "Обнаружено прерванное выполнение Вебмастера, но контекст устарел по TTL. "
                    "Состояние сброшено в idle, отправьте задачу заново."
                ),
                checkpoint_label="before_response_resume_expired",
            )

        feedback_kind = str(wm_ctx.last_feedback_class or "").strip() or "continue_task"
        fix_iteration = max(0, int(getattr(wm_ctx, "fix_iteration_count", 0) or 0))
        if stage == "running_dev_cli":
            dev_task_text = str(wm_ctx.last_cli_task or "").strip()
            if not dev_task_text:
                wm_ctx.stage = "idle"
                wm_ctx.fix_iteration_count = 0
                wm_ctx.last_feedback_class = "resume_missing_task"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="dev",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        runtime_error="resume_missing_task",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="dev")
                return await self._finalize_pipeline_response(
                    session,
                    (
                        "Обнаружено прерванное выполнение Вебмастера, но контекст восстановления неполный. "
                        "Состояние сброшено в idle, отправьте задачу заново."
                    ),
                    checkpoint_label="before_response_resume_incomplete",
                )
            return await self._run_dev_validation_loop(
                session=session,
                bot_app=bot_app,
                context=context,
                dest=dest,
                store=store,
                wm_ctx=wm_ctx,
                run=run,
                feedback_kind=feedback_kind,
                dev_task_text=dev_task_text,
                fresh_run=False,
                fix_iteration=fix_iteration,
                resumed_cli_output=None,
            )

        resumed_cli_output = str(wm_ctx.last_cli_report or "")
        if not resumed_cli_output.strip():
            wm_ctx.stage = "idle"
            wm_ctx.fix_iteration_count = 0
            wm_ctx.last_feedback_class = "resume_missing_report"
            store.save(wm_ctx)
            self._save_run_state(
                run,
                phase="validation",
                status="failed",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    runtime_error="resume_missing_report",
                ),
            )
            self._mark_run_finished(run, status="failed", phase="validation")
            return await self._finalize_pipeline_response(
                session,
                (
                    "Обнаружено прерванное выполнение Вебмастера, но отсутствует отчет разработчика для валидации. "
                    "Состояние сброшено в idle, отправьте задачу заново."
                ),
                checkpoint_label="before_response_resume_incomplete",
            )
        return await self._run_dev_validation_loop(
            session=session,
            bot_app=bot_app,
            context=context,
            dest=dest,
            store=store,
            wm_ctx=wm_ctx,
            run=run,
            feedback_kind=feedback_kind,
            dev_task_text=str(wm_ctx.last_cli_task or "").strip(),
            fresh_run=False,
            fix_iteration=fix_iteration,
            resumed_cli_output=resumed_cli_output,
        )

    async def _run_dev_validation_loop(
        self,
        *,
        session: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
        store: WebmasterStateStore,
        wm_ctx: WebmasterContext,
        run: Optional[RunArtifactHandle],
        feedback_kind: str,
        dev_task_text: str,
        fresh_run: bool,
        fix_iteration: int,
        resumed_cli_output: Optional[str],
    ) -> str:
        max_fix_iterations = self._max_fix_iterations(bot_app)
        current_task = str(dev_task_text or "").strip()
        current_fresh_run = bool(fresh_run)
        current_fix_iteration = max(0, int(fix_iteration or 0))
        buffered_cli_output = str(resumed_cli_output or "").strip() if resumed_cli_output is not None else None

        while True:
            if buffered_cli_output is None:
                wm_ctx.last_cli_task = current_task
                wm_ctx.stage = "running_dev_cli"
                wm_ctx.fix_iteration_count = current_fix_iteration
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="dev",
                    status="running",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                    ),
                )
                self._append_checkpoint(
                    run,
                    self._build_checkpoint_payload(
                        phase="dev",
                        status="started",
                        iteration=current_fix_iteration,
                        stage=wm_ctx.stage,
                        task_text=current_task,
                    ),
                )
                try:
                    cli_output = await self._run_use_cli(
                        bot_app,
                        session,
                        context,
                        dest,
                        current_task,
                        fresh_run=current_fresh_run,
                    )
                except Exception:
                    self._log.exception("webmaster run use_cli failed")
                    wm_ctx.stage = "await_intent_update"
                    wm_ctx.last_feedback_class = feedback_kind
                    store.save(wm_ctx)
                    self._append_checkpoint(
                        run,
                        self._build_checkpoint_payload(
                            phase="dev",
                            status="error",
                            iteration=current_fix_iteration,
                            stage=wm_ctx.stage,
                            task_text=current_task,
                            report_text="use_cli_failed",
                        ),
                    )
                    self._save_run_state(
                        run,
                        phase="dev",
                        status="failed",
                        mode_context=self._mode_context_payload(
                            session=session,
                            dest=dest,
                            wm_ctx=wm_ctx,
                            runtime_error="dev_cli_failed",
                        ),
                    )
                    self._mark_run_finished(run, status="failed", phase="dev")
                    return await self._finalize_pipeline_response(
                        session,
                        "Не удалось выполнить задачу в CLI. Уточните задачу и попробуем снова.",
                        checkpoint_label="before_response_dev_cli_error",
                    )
            else:
                cli_output = buffered_cli_output
                buffered_cli_output = None
                self._save_run_state(
                    run,
                    phase="dev",
                    status="running",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        developer_report=cli_output,
                    ),
                )

            self._append_checkpoint(
                run,
                self._build_checkpoint_payload(
                    phase="dev",
                    status="ok",
                    iteration=current_fix_iteration,
                    stage="running_dev_cli",
                    task_text=current_task,
                    report_text=cli_output,
                ),
            )
            self._save_run_state(
                run,
                phase="dev",
                status="running",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    developer_report=cli_output,
                ),
            )
            self._validate_run_boundary(run, phase="dev")

            wm_ctx.last_cli_report = cli_output
            wm_ctx.stage = "running_validation_cli"
            store.save(wm_ctx)
            self._save_run_state(
                run,
                phase="validation",
                status="running",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    developer_report=cli_output,
                ),
            )

            validation_task = self._build_validation_task(wm_ctx, cli_output, session=session)
            self._append_checkpoint(
                run,
                self._build_checkpoint_payload(
                    phase="validation",
                    status="started",
                    iteration=current_fix_iteration,
                    stage=wm_ctx.stage,
                    task_text=validation_task,
                    report_text=cli_output,
                ),
            )
            try:
                validation_task_for_cli = wrap_prompt_for_response_format(
                    validation_task,
                    CLIResponseFormat.JSON_OBJECT,
                )
                validation_output = await self._run_use_cli(
                    bot_app,
                    session,
                    context,
                    dest,
                    validation_task_for_cli,
                    fresh_run=True,
                )
                decision = self._parse_validation_report(validation_output)
            except Exception:
                self._log.exception("webmaster validation failed")
                wm_ctx.stage = "await_intent_update"
                wm_ctx.last_feedback_class = "validation_failed"
                store.save(wm_ctx)
                self._append_checkpoint(
                    run,
                    self._build_checkpoint_payload(
                        phase="validation",
                        status="error",
                        iteration=current_fix_iteration,
                        stage=wm_ctx.stage,
                        task_text=validation_task,
                        report_text="validation_use_cli_failed",
                    ),
                )
                self._save_run_state(
                    run,
                    phase="validation",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        developer_report=cli_output,
                        runtime_error="validation_cli_failed",
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="validation")
                return await self._finalize_pipeline_response(
                    session,
                    (
                        "Не удалось завершить валидацию результата. "
                        "Повторите запрос и уточните критерии приемки."
                    ),
                    checkpoint_label="before_response_validation_error",
                )

            wm_ctx.last_validation_report = validation_output
            wm_ctx.last_validation_json = dict(decision.raw)
            wm_ctx.last_fix_pack = list(decision.defects)
            gate_payload = self._evaluate_validation_gate(decision, cli_output)
            if bool(self._gate_passed(decision, cli_output)):
                gate_payload.update(
                    {
                        "passed": True,
                        "reasons": [],
                        "checklist_table_present": True,
                        "blocking_issue_count": 0,
                        "invalid_rows": [],
                        "non_pass_rows": [],
                        "missing_evidence_rows": [],
                    }
                )
            validation_report = self._serialize_validation_report(decision, gate_payload)
            self._append_checkpoint(
                run,
                self._build_checkpoint_payload(
                    phase="validation",
                    status="ok" if gate_payload["passed"] else "failed",
                    iteration=current_fix_iteration,
                    stage=wm_ctx.stage,
                    task_text=validation_task,
                    report_text=validation_output,
                    validation_status=str(decision.status or ""),
                    gate_passed=bool(gate_payload["passed"]),
                ),
            )
            self._save_run_state(
                run,
                phase="validation",
                status="running",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    developer_report=cli_output,
                    validation_report=validation_report,
                ),
            )
            boundary_report = self._boundary_report(run, phase="validation")
            if boundary_report is not None:
                self._save_run_state(
                    run,
                    phase="validation",
                    status="running",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        developer_report=cli_output,
                        validation_report=validation_report,
                        boundary_report=boundary_report.to_dict(),
                    ),
                )
                if bool(gate_payload["passed"]) and str(boundary_report.status or "") != "ok":
                    issues = ", ".join(issue.code for issue in boundary_report.issues)
                    raise RuntimeError(f"webmaster validation boundary drift: {issues}")

            if bool(gate_payload["passed"]):
                wm_ctx.stage = "await_feedback"
                wm_ctx.last_feedback_class = feedback_kind
                wm_ctx.fix_iteration_count = current_fix_iteration
                store.save(wm_ctx)
                structured_report = {
                    "status": "PASS",
                    "summary": str(decision.summary or "").strip(),
                    "developer_report": str(cli_output or ""),
                    "validation_report": dict(validation_report),
                    "fix_iteration_count": current_fix_iteration,
                }
                self._save_run_state(
                    run,
                    phase="complete",
                    status="running",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        developer_report=cli_output,
                        validation_report=validation_report,
                        structured_report=structured_report,
                    ),
                )
                self._validate_run_boundary(run, phase="complete")
                self._mark_run_finished(run, status="completed", phase="complete")
                return await self._finalize_pipeline_response(
                    session,
                    self._build_success_message(cli_output),
                    checkpoint_label="before_response_success",
                )

            current_fix_iteration += 1
            wm_ctx.fix_iteration_count = current_fix_iteration
            wm_ctx.last_feedback_class = "validation_failed"
            if current_fix_iteration > max_fix_iterations:
                wm_ctx.stage = "failed"
                store.save(wm_ctx)
                self._save_run_state(
                    run,
                    phase="validation",
                    status="failed",
                    mode_context=self._mode_context_payload(
                        session=session,
                        dest=dest,
                        wm_ctx=wm_ctx,
                        developer_report=cli_output,
                        validation_report=validation_report,
                        boundary_report=boundary_report.to_dict() if boundary_report is not None else None,
                    ),
                )
                self._mark_run_finished(run, status="failed", phase="validation")
                return await self._finalize_pipeline_response(
                    session,
                    self._build_failure_message(decision),
                    checkpoint_label="before_response_failed",
                )
            wm_ctx.stage = "await_fix_iteration"
            store.save(wm_ctx)
            self._save_run_state(
                run,
                phase="validation",
                status="running",
                mode_context=self._mode_context_payload(
                    session=session,
                    dest=dest,
                    wm_ctx=wm_ctx,
                    developer_report=cli_output,
                    validation_report=validation_report,
                    boundary_report=boundary_report.to_dict() if boundary_report is not None else None,
                ),
            )
            current_task = self._build_fix_task(
                wm_ctx,
                decision,
                current_fix_iteration,
                max_fix_iterations,
                session=session,
            )
            current_fresh_run = True

    async def execute_recovery_action(
        self,
        *,
        session: Any,
        action: str,
        run: Optional[RunArtifactHandle],
        state: Dict[str, Any],
        report: Any,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> Dict[str, Any]:
        _ = report
        resolved_action = str(action or "").strip()
        if resolved_action == "replay_finalize":
            return await self._execute_replay_finalize(
                session=session,
                run=run,
                state=state,
                dest=dest,
            )
        if resolved_action not in {"rollback_to_checkpoint", "restart_from_phase"}:
            return {
                "status": "blocked",
                "message": f"Recovery action `{resolved_action}` не поддерживается Webmaster hook.",
                "executed_operation": resolved_action,
            }
        prompt_text = self._recovery_prompt_from_state(action=resolved_action, state=state)
        if not prompt_text:
            return {
                "status": "blocked",
                "message": "Webmaster recovery не может быть выполнен: отсутствует исходный prompt.",
                "executed_operation": resolved_action,
            }
        latest_before = self._latest_mode_run(session)
        output = await self.run_pipeline(
            session=session,
            user_text=prompt_text,
            bot_app=bot_app,
            context=context,
            dest=dest,
        )
        latest_after = self._latest_mode_run(session)
        payload: Dict[str, Any] = {
            "status": "ok",
            "message": str(output or "").strip() or f"Операция `{resolved_action}` выполнена.",
            "executed_operation": resolved_action,
            "executed_via": f"webmaster_recovery_hook:{resolved_action}",
        }
        if latest_after is not None:
            before_run_id = str(getattr(latest_before, "run_id", "") or "")
            after_run_id = str(getattr(latest_after, "run_id", "") or "")
            if after_run_id and after_run_id not in {before_run_id, str(getattr(run, "run_id", "") or "")}:
                payload["spawned_run_id"] = after_run_id
                self._annotate_recovery_run(
                    latest_after,
                    source_run_id=str(getattr(run, "run_id", "") or ""),
                    action=resolved_action,
                    prompt_text=prompt_text,
                )
        return payload

    @staticmethod
    def _recovery_prompt_from_state(*, action: str, state: Dict[str, Any]) -> str:
        mode_context = state.get("mode_context") if isinstance(state, dict) else {}
        mode_context = mode_context if isinstance(mode_context, dict) else {}
        execution_context = mode_context.get("execution_context")
        execution_context = execution_context if isinstance(execution_context, dict) else {}
        prompt = ""
        if action == "restart_from_phase":
            prompt = str(mode_context.get("last_cli_task") or execution_context.get("last_cli_task_preview") or "").strip()
        if not prompt:
            prompt = str(mode_context.get("last_user_text") or "").strip()
        if not prompt:
            prompt = str(execution_context.get("last_user_text_preview") or "").strip()
        if not prompt:
            intent_payload = mode_context.get("intent_payload")
            if isinstance(intent_payload, dict):
                prompt = str(intent_payload.get("goal") or "").strip()
        if not prompt:
            prompt = str(mode_context.get("last_cli_task") or execution_context.get("last_cli_task_preview") or "").strip()
        return prompt

    def _annotate_recovery_run(
        self,
        run: Optional[RunArtifactHandle],
        *,
        source_run_id: str,
        action: str,
        prompt_text: str,
    ) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        state = artifact_store.load_state(run)
        self._save_run_state(
            run,
            phase=str(state.get("phase") or "intent"),
            status=str(state.get("status") or "running"),
            mode_context={
                "recovery_request": {
                    "source_run_id": str(source_run_id or "").strip() or None,
                    "action": str(action or "").strip(),
                    "prompt_preview": str(prompt_text or "").strip()[:500],
                }
            },
        )

    async def _execute_replay_finalize(
        self,
        *,
        session: Any,
        run: Optional[RunArtifactHandle],
        state: Dict[str, Any],
        dest: Dict[str, Any],
    ) -> Dict[str, Any]:
        if run is None:
            return {
                "status": "blocked",
                "message": "Webmaster replay finalize недоступен без исходного run.",
                "executed_operation": "replay_finalize",
            }
        mode_context = state.get("mode_context") if isinstance(state, dict) else {}
        mode_context = mode_context if isinstance(mode_context, dict) else {}
        webmaster_user_key = str(mode_context.get("webmaster_user_key") or "").strip()
        if not webmaster_user_key:
            return {
                "status": "blocked",
                "message": "Webmaster replay finalize не может определить user context.",
                "executed_operation": "replay_finalize",
            }
        developer_report = str(mode_context.get("developer_report") or "").strip()
        validation_report = mode_context.get("validation_report")
        structured_report = mode_context.get("structured_report")
        if not developer_report or not isinstance(validation_report, dict) or not validation_report:
            return {
                "status": "blocked",
                "message": "Webmaster replay finalize требует сохранённые developer_report и validation_report.",
                "executed_operation": "replay_finalize",
            }
        if not isinstance(structured_report, dict) or not structured_report:
            structured_report = {
                "status": str(validation_report.get("status") or "").strip().upper() or "PASS",
                "summary": str(validation_report.get("summary") or "").strip(),
                "developer_report": developer_report,
                "validation_report": dict(validation_report),
            }
        wm_ctx = self._recovery_snapshot_context(
            webmaster_user_key=webmaster_user_key,
            mode_context=mode_context,
            developer_report=developer_report,
            validation_report=validation_report,
        )

        user_text = str(mode_context.get("last_user_text") or wm_ctx.goal or wm_ctx.last_cli_task or "").strip()
        run_handle = self._prepare_run_artifacts(
            session=session,
            user_text=user_text,
            dest=dest,
            webmaster_user_key=webmaster_user_key,
            wm_ctx=wm_ctx,
        )
        if run_handle is None:
            return {
                "status": "blocked",
                "message": "Webmaster replay finalize не может создать новый run artifact.",
                "executed_operation": "replay_finalize",
            }
        artifact_store = self._artifact_store()
        assert artifact_store is not None
        try:
            existing_plan = artifact_store.load_plan(run)
        except Exception:
            existing_plan = {}
        if not isinstance(existing_plan, dict) or not existing_plan:
            existing_plan = {
                "kind": "webmaster_pipeline",
                "goal": str(wm_ctx.goal or "").strip(),
                "task_kind": str(wm_ctx.task_kind or "").strip() or "new_task",
                "units": [
                    {"id": "webmaster:dev", "step_type": "dev", "title": "Run webmaster implementation loop"},
                    {"id": "webmaster:validation", "step_type": "validation", "title": "Validate webmaster implementation loop"},
                ],
                "boundary_map": [
                    {"run_phase": "intent"},
                    {"run_phase": "dev"},
                    {"run_phase": "validation"},
                    {"run_phase": "complete"},
                ],
                "validation_contracts": ["legacy_webmaster_gate"],
            }
        self._save_run_plan(run_handle, existing_plan)
        self._save_run_state(
            run_handle,
            phase="validation",
            status="running",
            mode_context=self._mode_context_payload(
                session=session,
                dest=dest,
                wm_ctx=wm_ctx,
                developer_report=developer_report,
                validation_report=dict(validation_report),
            ),
        )
        self._validate_run_boundary(run_handle, phase="validation")
        self._save_run_state(
            run_handle,
            phase="complete",
            status="running",
            mode_context=self._mode_context_payload(
                session=session,
                dest=dest,
                wm_ctx=wm_ctx,
                developer_report=developer_report,
                validation_report=dict(validation_report),
                structured_report=dict(structured_report),
            ),
        )
        self._validate_run_boundary(run_handle, phase="complete")
        try:
            self._mark_run_finished(run_handle, status="completed", phase="complete")
        finally:
            self._clear_active_run_handle(session)
        return {
            "status": "ok",
            "message": "Webmaster replay finalize выполнен по сохранённым artifact state.",
            "executed_operation": "replay_finalize",
            "executed_via": "webmaster_replay_finalize",
            "spawned_run_id": run_handle.run_id,
        }

    @staticmethod
    def _recovery_snapshot_context(
        *,
        webmaster_user_key: str,
        mode_context: Dict[str, Any],
        developer_report: str,
        validation_report: Dict[str, Any],
    ) -> WebmasterContext:
        payload = {
            "task_kind": str(mode_context.get("task_kind") or "new_task").strip() or "new_task",
            "stage": "await_feedback",
            "goal": str(mode_context.get("goal") or "").strip(),
            "actions": list(mode_context.get("actions") or []),
            "constraints": list(mode_context.get("constraints") or []),
            "acceptance_criteria": list(mode_context.get("acceptance_criteria") or []),
            "ambiguities": list(mode_context.get("ambiguities") or []),
            "assumptions": list(mode_context.get("assumptions") or []),
            "last_cli_task": str(mode_context.get("last_cli_task") or "").strip(),
            "last_cli_report": str(developer_report or "").strip(),
            "last_feedback_class": str(mode_context.get("last_feedback_class") or "").strip(),
            "last_user_text": str(mode_context.get("last_user_text") or "").strip(),
            "last_validation_report": json.dumps(validation_report, ensure_ascii=False),
            "last_validation_json": dict(validation_report),
            "last_fix_pack": list(mode_context.get("last_fix_pack") or []),
            "active_prompt_version": int(mode_context.get("active_prompt_version") or 1),
            "confirmation_attempts": int(mode_context.get("confirmation_attempts") or 0),
            "fix_iteration_count": int(mode_context.get("fix_iteration_count") or 0),
        }
        return WebmasterContext.from_dict(payload, webmaster_user_key)

    # Override: лог-сообщение не содержит mode_id= (расхождение с RunArtifactsMixin).
    def _is_run_artifacts_enabled(self) -> bool:
        service = self._optional_run_artifacts()
        if service is None:
            return False
        try:
            return bool(service.is_enabled())
        except Exception:
            self._log.exception("webmaster run artifacts: failed to resolve enabled flag")
            return False

    # Override: оборачивает вызов artifact_store.latest_run в try/except (расхождение с RunArtifactsMixin).
    def _latest_mode_run(self, session: Any) -> Optional[RunArtifactHandle]:
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None
        try:
            return artifact_store.latest_run(session=session, mode_id=self.mode_id)
        except Exception:
            self._log.exception("webmaster run artifacts: latest_run failed")
            return None

    def _prepare_run_artifacts(
        self,
        *,
        session: Any,
        user_text: str,
        dest: Dict[str, Any],
        webmaster_user_key: str,
        wm_ctx: WebmasterContext,
    ) -> Optional[RunArtifactHandle]:
        self._clear_active_run_handle(session)
        if not self._is_run_artifacts_enabled():
            return None
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None

        latest = self._latest_mode_run(session)
        if latest is not None:
            latest_state = artifact_store.load_state(latest)
            if not self._is_terminal_run_status(latest_state.get("status")):
                artifact_store.mark_finished(
                    latest,
                    status="failed",
                    phase=str(latest_state.get("phase") or "validation"),
                )

        run = artifact_store.start_run(
            session=session,
            mode_id=self.mode_id,
            phase="intent",
            source_prompt_hash=self._prompt_hash(user_text),
            mode_context={
                "webmaster_user_key": webmaster_user_key,
                "task_kind": str(getattr(wm_ctx, "task_kind", "") or "new_task"),
                "stage": str(getattr(wm_ctx, "stage", "") or "idle"),
                "execution_context": self._execution_context(session=session, dest=dest, wm_ctx=wm_ctx),
            },
        )
        self._set_active_run_handle(session, run)
        return run

    def _restore_active_run_for_resume(
        self,
        *,
        session: Any,
        webmaster_user_key: str,
        wm_ctx: WebmasterContext,
    ) -> Optional[RunArtifactHandle]:
        if not self._is_run_artifacts_enabled():
            return None
        stage = str(getattr(wm_ctx, "stage", "") or "").strip()
        if stage not in {"running_dev_cli", "running_validation_cli"}:
            return None
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None
        latest = self._latest_mode_run(session)
        if latest is None:
            return None
        latest_state = artifact_store.load_state(latest)
        if self._is_terminal_run_status(latest_state.get("status")):
            return None
        stored_key = str((latest_state.get("mode_context") or {}).get("webmaster_user_key") or "").strip()
        if stored_key and stored_key != str(webmaster_user_key or "").strip():
            return None
        self._set_active_run_handle(session, latest)
        return latest

    def _webmaster_runtime(self) -> Any:
        runtime_getter = self._optional_runtime_getter()
        if not callable(runtime_getter):
            return None
        try:
            return runtime_getter("webmaster_artifact_checkpoints")
        except Exception:
            self._log.exception("webmaster runtime getter failed for artifact checkpoints")
            return None

    def _build_checkpoint_payload(
        self,
        *,
        phase: str,
        status: str,
        iteration: int,
        stage: str,
        task_text: str = "",
        report_text: str = "",
        validation_status: str = "",
        gate_passed: Optional[bool] = None,
    ) -> Dict[str, Any]:
        runtime = self._webmaster_runtime()
        builder = getattr(runtime, "build_checkpoint_payload", None)
        if callable(builder):
            try:
                payload = builder(
                    phase=phase,
                    status=status,
                    iteration=iteration,
                    stage=stage,
                    task_text=task_text,
                    report_text=report_text,
                    validation_status=validation_status,
                    gate_passed=gate_passed,
                )
                if isinstance(payload, dict) and payload:
                    return payload
            except Exception:
                self._log.exception("webmaster run artifacts: runtime checkpoint builder failed")
        unit_phase = str(phase or "").strip() or "dev"
        return {
            "phase": unit_phase,
            "unit_id": f"webmaster:{unit_phase}:{max(0, int(iteration or 0)) + 1}",
            "status": str(status or "").strip() or "started",
            "iteration": max(0, int(iteration or 0)),
            "stage": str(stage or "").strip() or "idle",
            "task_preview": str(task_text or "").strip()[:500],
            "report_preview": str(report_text or "").strip()[:500],
            "validation_status": str(validation_status or "").strip().upper(),
            "gate_passed": bool(gate_passed) if gate_passed is not None else None,
        }

    @staticmethod
    def _phase_for_stage(stage: Any) -> str:
        token = str(stage or "").strip()
        if token == "running_dev_cli":
            return "dev"
        if token in {"running_validation_cli", "await_fix_iteration", "failed"}:
            return "validation"
        if token == "await_feedback":
            return "complete"
        return "intent"

    def _execution_context(self, *, session: Any, dest: Dict[str, Any], wm_ctx: WebmasterContext) -> Dict[str, Any]:
        return {
            "dest_kind": str((dest or {}).get("kind") or "telegram"),
            "chat_id": (dest or {}).get("chat_id"),
            "user_id": (dest or {}).get("user_id"),
            "chat_type": str((dest or {}).get("chat_type") or ""),
            "task_kind": str(getattr(wm_ctx, "task_kind", "") or ""),
            "stage": str(getattr(wm_ctx, "stage", "") or ""),
            "last_user_text_preview": str(getattr(wm_ctx, "last_user_text", "") or "")[:500],
            "last_cli_task_preview": str(getattr(wm_ctx, "last_cli_task", "") or "")[:500],
        }

    def _mode_context_payload(
        self,
        *,
        session: Any,
        dest: Dict[str, Any],
        wm_ctx: WebmasterContext,
        intent_payload: Optional[Dict[str, Any]] = None,
        developer_report: Optional[str] = None,
        validation_report: Optional[Dict[str, Any]] = None,
        structured_report: Optional[Dict[str, Any]] = None,
        runtime_error: str = "",
        boundary_report: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "webmaster_user_key": str(wm_ctx.key or "").strip(),
            "task_kind": str(wm_ctx.task_kind or "").strip() or "new_task",
            "stage": str(wm_ctx.stage or "").strip() or "idle",
            "goal": str(wm_ctx.goal or "").strip(),
            "actions": list(wm_ctx.actions or []),
            "constraints": list(wm_ctx.constraints or []),
            "acceptance_criteria": list(wm_ctx.acceptance_criteria or []),
            "ambiguities": list(wm_ctx.ambiguities or []),
            "assumptions": list(wm_ctx.assumptions or []),
            "fix_iteration_count": int(getattr(wm_ctx, "fix_iteration_count", 0) or 0),
            "confirmation_attempts": int(getattr(wm_ctx, "confirmation_attempts", 0) or 0),
            "last_feedback_class": str(getattr(wm_ctx, "last_feedback_class", "") or "").strip(),
            "last_user_text": str(getattr(wm_ctx, "last_user_text", "") or "").strip(),
            "last_cli_task": str(getattr(wm_ctx, "last_cli_task", "") or "").strip(),
            "active_prompt_version": int(getattr(wm_ctx, "active_prompt_version", 1) or 1),
            "execution_context": self._execution_context(session=session, dest=dest, wm_ctx=wm_ctx),
        }
        if isinstance(intent_payload, dict) and intent_payload:
            payload["intent_payload"] = dict(intent_payload)
        if developer_report is None:
            developer_report = str(getattr(wm_ctx, "last_cli_report", "") or "").strip()
        if developer_report:
            payload["developer_report"] = developer_report
        if validation_report is not None and validation_report:
            payload["validation_report"] = dict(validation_report)
            gate_payload = validation_report.get("gate")
            if isinstance(gate_payload, dict):
                payload["validation_gate"] = dict(gate_payload)
        if isinstance(getattr(wm_ctx, "last_validation_json", None), dict) and wm_ctx.last_validation_json:
            payload["validation_json"] = dict(wm_ctx.last_validation_json)
        if isinstance(getattr(wm_ctx, "last_fix_pack", None), list) and wm_ctx.last_fix_pack:
            payload["last_fix_pack"] = list(wm_ctx.last_fix_pack)
        if structured_report is not None and structured_report:
            payload["structured_report"] = dict(structured_report)
        if runtime_error:
            payload["runtime_error"] = str(runtime_error or "")
        if isinstance(boundary_report, dict) and boundary_report:
            payload["boundary_report"] = dict(boundary_report)
        return payload

    # Override: нет параметра merge_execution_context (всегда shallow); phase fallback — "intent" vs "complete" в миксине;
    # run.run_id в логе используется напрямую (расхождения с RunArtifactsMixin).
    def _save_run_state(
        self,
        run: Optional[RunArtifactHandle],
        *,
        phase: str,
        status: str,
        mode_context: Optional[Dict[str, Any]] = None,
        merge_execution_context: MergeStrategy = "shallow",
    ) -> None:
        # merge_execution_context присутствует для совместимости сигнатуры с RunArtifactsMixin (LSP);
        # webmaster всегда выполняет shallow-merge с phase-fallback "intent", поэтому параметр игнорируется.
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
                merged_execution_context = dict(existing_execution_context)
                merged_execution_context.update(incoming_execution_context)
                incoming_mode_context["execution_context"] = merged_execution_context
            merged_mode_context.update(incoming_mode_context)
            artifact_store.save_state(
                run,
                {
                    "phase": str(phase or current.get("phase") or "intent"),
                    "status": str(status or current.get("status") or "running"),
                    "mode_context": merged_mode_context,
                },
            )
        except Exception:
            self._log.exception("webmaster run artifacts: save_state failed phase=%s run_id=%s", phase, run.run_id)

    # Override: run.run_id в логе напрямую (расхождение с RunArtifactsMixin, где getattr(run, "run_id", "")).
    def _save_run_plan(self, run: Optional[RunArtifactHandle], plan: Dict[str, Any]) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            artifact_store.save_plan(run, dict(plan or {}))
        except Exception:
            self._log.exception("webmaster run artifacts: save_plan failed run_id=%s", run.run_id)

    # Override: run.run_id в логе напрямую (расхождение с RunArtifactsMixin, где getattr(run, "run_id", "")).
    def _append_checkpoint(self, run: Optional[RunArtifactHandle], checkpoint: Dict[str, Any]) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            artifact_store.append_checkpoint(run, dict(checkpoint or {}))
        except Exception:
            self._log.exception("webmaster run artifacts: append_checkpoint failed run_id=%s", run.run_id)

    def _boundary_report(self, run: Optional[RunArtifactHandle], *, phase: str) -> Any:
        if run is None:
            return None
        validator = self._optional_run_boundary_validation()
        if validator is None or not validator.is_enabled():
            return None
        return validator.validate(run, mode_id=self.mode_id, phase=phase)

    # Override: использует вспомогательный _boundary_report (вызываемый также из run_pipeline напрямую)
    # и иной текст RuntimeError; RunArtifactsMixin вызывает validator.validate напрямую.
    def _validate_run_boundary(self, run: Optional[RunArtifactHandle], *, phase: str) -> None:
        report = self._boundary_report(run, phase=phase)
        if report is None or str(report.status or "") == "ok":
            return
        issues = ", ".join(issue.code for issue in report.issues)
        raise RuntimeError(f"Webmaster run boundary validation failed phase={phase}: {issues}")

    # Override: run.run_id в логе напрямую (расхождение с RunArtifactsMixin, где getattr(run, "run_id", "")).
    def _mark_run_finished(self, run: Optional[RunArtifactHandle], *, status: str, phase: str) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            artifact_store.mark_finished(run, status=status, phase=phase)
        except Exception:
            self._log.exception("webmaster run artifacts: mark_finished failed run_id=%s", run.run_id)

    def _state_root(self, session: Any) -> str:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if workdir:
            return cli_proxy_artifact_path(workdir, ".webmaster_data")
        self._log.warning("webmaster session workdir missing; using cwd for state store")
        return cli_proxy_artifact_path(os.getcwd(), ".webmaster_data")

    def _prompt_learning_path(self, session: Any) -> str:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if workdir:
            return cli_proxy_artifact_path(workdir, ".webmaster/prompt/learning.yaml")
        return cli_proxy_artifact_path(os.getcwd(), ".webmaster/prompt/learning.yaml")

    def _store(self, session: Any) -> WebmasterStateStore:
        return WebmasterStateStore(
            self._state_root(session),
            prompt_learning_path=self._prompt_learning_path(session),
        )

    async def _ensure_project_prompts_ready(
        self,
        *,
        session: Any,
        bot_app: Any,
        context: Any,
        chat_id: Optional[int],
        query: Any,
    ) -> bool:
        try:
            ensure_project_prompts(getattr(session, "workdir", ""))
            self._load_prompts(session=session)
            return True
        except InvalidProjectPromptsError:
            self._log.exception(
                "webmaster project prompts validation failed session_id=%s workdir=%s",
                getattr(session, "id", None),
                getattr(session, "workdir", None),
            )
            if chat_id is not None:
                try:
                    ms = self._messaging(bot_app=bot_app, context=context)
                    await ms.send_or_edit(
                        query=query,
                        chat_id=str(chat_id),
                        text=self._invalid_prompts_enable_text,
                        md2=True,
                    )
                except Exception:
                    self._log.exception("webmaster invalid prompts notification failed")
            return False
        except Exception:
            self._log.exception(
                "webmaster project prompts unexpected load failure session_id=%s workdir=%s",
                getattr(session, "id", None),
                getattr(session, "workdir", None),
            )
            if chat_id is not None:
                try:
                    ms = self._messaging(bot_app=bot_app, context=context)
                    await ms.send_or_edit(
                        query=query,
                        chat_id=str(chat_id),
                        text=self._invalid_prompts_enable_text,
                        md2=True,
                    )
                except Exception:
                    self._log.exception("webmaster prompts unexpected error notification failed")
            return False

    def _resolve_prompts_workdir(self, *, session: Any = None, workdir: Optional[str] = None) -> str:
        workdir = str(workdir or "").strip()
        if not workdir and session is not None:
            workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            raise RuntimeError("webmaster prompts workdir is empty")
        return workdir

    def _load_prompts(self, *, session: Any = None, workdir: Optional[str] = None) -> Dict[str, str]:
        workdir = self._resolve_prompts_workdir(session=session, workdir=workdir)
        ensure_project_prompts(workdir)
        return load_mode_prompt_texts(workdir, self.mode_id)

    def _load_prompt_learning(self, *, session: Any = None, workdir: Optional[str] = None) -> Dict[str, object]:
        workdir = self._resolve_prompts_workdir(session=session, workdir=workdir)
        ensure_project_prompts(workdir)
        return load_mode_learning(workdir, self.mode_id)

    def _save_prompt_learning(
        self,
        learning: Dict[str, object],
        *,
        session: Any = None,
        workdir: Optional[str] = None,
    ) -> None:
        workdir = self._resolve_prompts_workdir(session=session, workdir=workdir)
        ensure_project_prompts(workdir)
        save_mode_learning(workdir, self.mode_id, learning)

    async def _analyze_intent(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        dest: Dict[str, Any],
        user_text: str,
        wm_ctx: WebmasterContext,
    ) -> Dict[str, Any]:
        return await self._intent.analyze_intent(
            bot_app=bot_app,
            session=session,
            context=context,
            dest=dest,
            user_text=user_text,
            wm_ctx=wm_ctx,
        )

    def _normalize_intent_payload(
        self,
        *,
        payload: Dict[str, Any],
        user_text: str,
        wm_ctx: WebmasterContext,
    ) -> Dict[str, Any]:
        return self._intent.normalize_intent_payload(payload=payload, user_text=user_text, wm_ctx=wm_ctx)

    def _fallback_intent_payload(
        self,
        *,
        user_text: str,
        wm_ctx: WebmasterContext,
    ) -> Dict[str, Any]:
        return self._intent.fallback_intent_payload(user_text=user_text, wm_ctx=wm_ctx)

    async def _confirm_intent(self, bot_app: Any, session: Any, context: Any, dest: Dict[str, Any], wm_ctx: WebmasterContext) -> str:
        tooling = self._tooling()
        prompts = self._load_prompts(session=session)
        confirmation_prompt = prompts["confirmation"]
        actions = "\n".join(f"- {a}" for a in wm_ctx.actions) or "- (пусто)"
        acceptance = "\n".join(f"- {a}" for a in wm_ctx.acceptance_criteria) or "- (не заданы)"
        question = (
            f"{confirmation_prompt}\n\n"
            f"Цель:\n{wm_ctx.goal or '(не указана)'}\n\n"
            f"Действия:\n{actions}\n\n"
            f"Критерии приемки:\n{acceptance}"
        )
        try:
            return await tooling.ask_user(
                question=question,
                options=["Подтвердить", "Уточнить", "Новая задача"],
                allow_custom=False,
                system_options=False,
                ctx=self._tool_ctx(session, context, dest, bot_app),
            )
        except ValueError as exc:
            raise _InvalidConfirmationSelection(str(exc)) from exc

    async def _run_use_cli(
        self,
        bot_app: Any,
        session: Any,
        context: Any,
        dest: Dict[str, Any],
        task_text: str,
        *,
        fresh_run: bool,
    ) -> str:
        tooling = self._tooling()
        resp = await tooling.execute(
            "use_cli",
            {"task_text": task_text, "fresh_run": bool(fresh_run)},
            self._tool_ctx(session, context, dest, bot_app),
        )
        if not resp.get("success"):
            raise RuntimeError(f"use_cli failed: {resp.get('error')}")
        return str(resp.get("output") or "")

    async def _classify_feedback_llm(
        self,
        bot_app: Any,
        user_text: str,
        wm_ctx: WebmasterContext,
        *,
        session: Any,
    ) -> FeedbackDecision:
        return await self._intent.classify_feedback_llm(bot_app, user_text, wm_ctx, session=session)

    async def _build_prompt_patch_llm(
        self,
        bot_app: Any,
        user_feedback: str,
        cli_report: str,
        *,
        session: Any,
    ) -> Optional[Dict[str, Any]]:
        prompts = self._load_prompts(session=session)
        system = (
            "Сформируй patch промпта. Верни только JSON с полями: "
            "added_rules, changed_rules, removed_rules, reason, expected_effect. "
            "Формулируй только ОБОБЩЕННЫЕ правила без привязки к конкретным RQ/task/номерам подпунктов."
        )
        user = json.dumps(
            {
                "patch_prompt": prompts["prompt_patch"],
                "user_feedback": user_feedback,
                "cli_report": cli_report[:3000],
            },
            ensure_ascii=False,
        )
        out = await self._chat_completion(
            bot_app,
            system,
            user,
            response_format={"type": "json_object"},
        )
        parsed = self._parse_llm_json(
            out,
            required_fields=("added_rules", "changed_rules", "removed_rules", "reason", "expected_effect"),
        )
        return normalize_general_patch(parsed)

    async def _maybe_compact_prompt_learning(
        self,
        bot_app: Any,
        learning: Dict[str, object],
        *,
        session: Any,
    ) -> Dict[str, object]:
        learning = normalize_learning_payload(learning)
        patches = learning.get("patches")
        if not isinstance(patches, list) or len(patches) <= 20:
            return learning
        valid = [x for x in patches if isinstance(x, dict)]
        if len(valid) <= 20:
            learning["patches"] = valid
            return learning
        try:
            compact = await self._compact_prompt_patches_llm(bot_app, valid, session=session)
            normalized_compact = normalize_general_patch(compact)
            if normalized_compact is None:
                raise RuntimeError("webmaster compact patch has no generalized rules")
            learning["patches"] = [normalized_compact]
            return learning
        except Exception:
            self._log.exception("webmaster prompt patch compaction failed")
            learning["patches"] = valid
            return learning

    async def _compact_prompt_patches_llm(
        self,
        bot_app: Any,
        patches: List[Dict[str, Any]],
        *,
        session: Any,
    ) -> Dict[str, Any]:
        prompts = self._load_prompts(session=session)
        system = (
            "Сверни список patch-коррекций промпта в один итоговый patch. "
            "Верни только JSON с полями: "
            "added_rules, changed_rules, removed_rules, reason, expected_effect. "
            "Формулируй только ОБОБЩЕННЫЕ правила без привязки к конкретным RQ/task/номерам подпунктов."
        )
        user = json.dumps(
            {
                "compact_prompt": prompts["prompt_compact"],
                "patches": patches,
            },
            ensure_ascii=False,
        )
        out = await self._chat_completion(
            bot_app,
            system,
            user,
            response_format={"type": "json_object"},
        )
        parsed = self._parse_llm_json(
            out,
            required_fields=(
                "added_rules",
                "changed_rules",
                "removed_rules",
                "reason",
                "expected_effect",
            ),
        )
        compact = normalize_general_patch(parsed)
        if compact is None:
            raise RuntimeError("LLM compact patch has no generalized rules")
        return compact

    def _parse_llm_json(self, text: str, *, required_fields: tuple[str, ...]) -> Dict[str, Any]:
        try:
            data = loads_safe(str(text or ""), strict_first=False)
        except Exception:
            self._log.exception("webmaster json parse failed")
            raise RuntimeError("LLM returned invalid JSON")
        if not isinstance(data, dict):
            raise RuntimeError("LLM returned non-object JSON")
        for field in required_fields:
            if field not in data:
                raise RuntimeError(f"LLM JSON missing field: {field}")
        return data

    def _tool_ctx(self, session: Any, context: Any, dest: Dict[str, Any], bot_app: Any) -> Dict[str, Any]:
        defaults = getattr(getattr(bot_app, "config", None), "defaults", None)
        use_cli_timeout_sec = int(getattr(defaults, "webmaster_use_cli_timeout_sec", 3600))
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
            "tool_timeouts_ms": {"use_cli": use_cli_timeout_sec * 1000},
            "corr_id": f"webmaster:{getattr(session, 'id', 'unknown')}",
        }

    def _resolve_user_id(
        self,
        *,
        user_id: Any,
        chat_id: int,
        chat_type: str,
        context: Any,
        session: Any,
    ) -> Optional[int]:
        try:
            uid = int(user_id) if user_id is not None else 0
        except Exception:
            uid = 0
        if uid > 0:
            setattr(session, "webmaster_last_user_id", uid)
            return uid
        for attr in ("effective_user", "user", "from_user"):
            obj = getattr(context, attr, None)
            ctx_uid = getattr(obj, "id", None)
            try:
                resolved = int(ctx_uid) if ctx_uid is not None else 0
            except Exception:
                resolved = 0
            if resolved > 0:
                setattr(session, "webmaster_last_user_id", resolved)
                return resolved
        try:
            remembered = int(getattr(session, "webmaster_last_user_id", 0) or 0)
        except Exception:
            remembered = 0
        if remembered > 0:
            return remembered
        if int(chat_id or 0) > 0:
            # Fallback to chat scope when explicit user id is unavailable.
            # This avoids unstable `user_id=0` bucket collisions.
            return int(chat_id)
        return None

    async def _chat_completion(
        self,
        bot_app: Any,
        system: str,
        user: str,
        *,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        runtime_getter = self._optional_runtime_getter()
        runtime = runtime_getter("webmaster_chat_completion") if callable(runtime_getter) else None
        if runtime is not None and hasattr(runtime, "chat_completion"):
            return str(
                await runtime.chat_completion(
                    bot_app.config,
                    str(system or ""),
                    str(user or ""),
                    response_format=response_format,
                )
                or ""
            )
        return str(
            await chat_completion(
                bot_app.config,
                str(system or ""),
                str(user or ""),
                response_format=response_format,
            )
            or ""
        )

    @staticmethod
    async def _run_git(workdir: str, args: List[str]) -> tuple[int, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_PAGER"] = "cat"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            return int(proc.returncode or 0), (out or b"").decode(errors="ignore")
        except FileNotFoundError:
            return 127, "git: command not found"
        except Exception as exc:
            return 1, f"git: failed to run: {exc}"

    def _git_is_usable(self, workdir: str) -> bool:
        wd = os.path.abspath(str(workdir or "."))
        if not os.path.isdir(wd):
            return False
        if not shutil.which("git"):
            return False
        d = wd
        while True:
            if os.path.exists(os.path.join(d, ".git")):
                return True
            parent = os.path.dirname(d)
            if parent == d:
                return False
            d = parent

    async def _silent_git_checkpoint(self, session: Any, label: str) -> bool:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir or not self._git_is_usable(workdir):
            return False
        code, status_out = await self._run_git(workdir, ["status", "--porcelain"])
        if code != 0 or not (status_out or "").strip():
            return False
        code, add_out = await self._run_git(workdir, ["add", "-A"])
        if code != 0:
            self._log.warning("webmaster checkpoint add failed (%s): %s", label, add_out[:200])
            return False
        msg = f"[Webmaster] checkpoint: {str(label or 'checkpoint').strip()}"
        if len(msg) > 100:
            msg = msg[:100].rstrip()
        code, commit_out = await self._run_git(workdir, ["commit", "-m", msg])
        if code != 0:
            self._log.warning("webmaster checkpoint commit failed (%s): %s", label, commit_out[:200])
            return False
        return True

    async def _finalize_pipeline_response(self, session: Any, text: str, *, checkpoint_label: str) -> str:
        try:
            await self._silent_git_checkpoint(session, checkpoint_label)
        except Exception:
            self._log.exception("webmaster finalize checkpoint failed")
        return str(text or "")

    def _build_cli_task(self, wm_ctx: WebmasterContext, *, session: Any) -> str:
        prompts = self._load_prompts(session=session)
        learning = self._load_prompt_learning(session=session)
        system_base, version = apply_prompt_learning(prompts["system_base"], learning)
        wm_ctx.active_prompt_version = version
        cli_task_prompt = prompts["cli_task"]
        actions = "\n".join(f"- {x}" for x in wm_ctx.actions) or "- (не указаны)"
        constraints = "\n".join(f"- {x}" for x in wm_ctx.constraints) or "- (не указаны)"
        acceptance = "\n".join(f"- {x}" for x in wm_ctx.acceptance_criteria) or "- (не указаны)"
        if str(wm_ctx.task_kind or "").strip() == "new_task":
            run_policy = "Требование: запуск fresh (без resume)."
        else:
            run_policy = "Требование: продолжение диалога через resume текущей CLI-сессии (не fresh)."
        return (
            f"{system_base}\n\n"
            f"{cli_task_prompt}\n\n"
            f"Цель:\n{wm_ctx.goal}\n\n"
            f"Подтвержденные действия:\n{actions}\n\n"
            f"Ограничения:\n{constraints}\n\n"
            f"Критерии приемки:\n{acceptance}\n\n"
            f"{run_policy}"
        )

    def _build_validation_task(
        self,
        wm_ctx: WebmasterContext,
        developer_report: str,
        *,
        session: Any = None,
    ) -> str:
        prompts = self._load_prompts(session=session)
        validation_prompt = prompts["validation_task"]
        actions = "\n".join(f"- {x}" for x in wm_ctx.actions) or "- (не указаны)"
        constraints = "\n".join(f"- {x}" for x in wm_ctx.constraints) or "- (не указаны)"
        acceptance = "\n".join(f"- {x}" for x in wm_ctx.acceptance_criteria) or "- (не указаны)"
        checklist_items = [
            "Семантический HTML",
            "ARIA/доступность",
            "Tab-навигация и focus states",
            "Контраст",
            "Минимальный размер кликабельных зон",
            "Responsive mobile/tablet/desktop",
            "Ограничение длины/ширины текста",
            "Понятность API/пропсов/структуры",
        ]
        checklist = "\n".join(f"- {x}" for x in checklist_items)
        return (
            f"{validation_prompt}\n\n"
            f"Цель:\n{wm_ctx.goal}\n\n"
            f"Подтвержденные действия:\n{actions}\n\n"
            f"Ограничения:\n{constraints}\n\n"
            f"Критерии приемки:\n{acceptance}\n\n"
            f"Обязательный чеклист:\n{checklist}\n\n"
            "Сохранять изменения в файлах запрещено. Только проверка.\n\n"
            f"Отчет разработчика для валидации:\n{developer_report}"
        )

    def _build_fix_task(
        self,
        wm_ctx: WebmasterContext,
        decision: ValidationDecision,
        iteration: int,
        max_iterations: int,
        *,
        session: Any = None,
    ) -> str:
        prompts = self._load_prompts(session=session)
        fix_prompt = prompts["fix_task"]
        defects = "\n".join(
            (
                f"- severity={d.get('severity') or 'unknown'}; "
                f"title={d.get('title') or 'без названия'}; "
                f"location={d.get('location') or 'не указано'}; "
                f"why={d.get('why') or 'не указано'}; "
                f"fix_hint={d.get('fix_hint') or 'не указано'}"
            )
            for d in decision.defects
        ) or "- (дефекты не перечислены)"
        checklist_issues: List[str] = []
        for row in decision.checklist_rows:
            item = str(row.get("item") or "без названия").strip()
            status = str(row.get("status") or "UNKNOWN").strip().upper()
            evidence = str(row.get("evidence") or "").strip()
            fixed = str(row.get("fixed") or "").strip()
            why_not_done = str(row.get("why_not_done") or "").strip()
            is_problem = status in {"PARTIAL", "FAIL"} or not evidence or bool(why_not_done)
            if not is_problem:
                continue
            checklist_issues.append(
                (
                    f"- item={item}; status={status}; "
                    f"evidence={evidence or 'не указано'}; "
                    f"fixed={fixed or 'не указано'}; "
                    f"why_not_done={why_not_done or 'не указано'}"
                )
            )
        checklist_issues_text = "\n".join(checklist_issues) or "- (проблемные пункты не перечислены)"
        blockers = "\n".join(f"- {x}" for x in decision.blocking_issues) or "- (нет)"
        return (
            f"{fix_prompt}\n\n"
            f"Цель:\n{wm_ctx.goal}\n\n"
            f"Итерация исправления: {iteration}/{max_iterations}\n\n"
            f"Blocking issues:\n{blockers}\n\n"
            f"Проблемные пункты чеклиста:\n{checklist_issues_text}\n\n"
            f"Fix-пакет дефектов:\n{defects}\n\n"
            "После исправлений сформируй отчет и таблицу чеклиста."
        )

    def _max_fix_iterations(self, bot_app: Any) -> int:
        defaults = getattr(getattr(bot_app, "config", None), "defaults", None)
        raw = int(getattr(defaults, "webmaster_validation_max_fix_iterations", 2) or 2)
        return max(0, min(raw, 10))

    def _resume_ttl_sec(self, bot_app: Any) -> int:
        defaults = getattr(getattr(bot_app, "config", None), "defaults", None)
        raw = getattr(defaults, "webmaster_resume_ttl_sec", None)
        if raw is None:
            raw = getattr(defaults, "webmaster_use_cli_timeout_sec", 3600)
        try:
            ttl = int(raw or 0)
        except Exception:
            ttl = 3600
        return max(60, min(ttl, 24 * 3600))

    def _build_success_message(self, developer_report: str) -> str:
        report = str(developer_report or "").strip()
        if not report:
            return "✅ Задача выполнена и валидация пройдена."
        return f"✅ Задача выполнена и валидация пройдена.\n\n{report}"

    def _build_failure_message(self, decision: ValidationDecision) -> str:
        lines = ["❌ Не удалось пройти валидацию после нескольких итераций."]
        if decision.blocking_issues:
            lines.append("Блокирующие проблемы:")
            lines.extend(f"- {x}" for x in decision.blocking_issues[:8])
        elif decision.defects:
            lines.append("Критичные дефекты:")
            for defect in decision.defects[:8]:
                title = str(defect.get("title") or "без названия")
                why = str(defect.get("why") or "без описания")
                lines.append(f"- {title}: {why}")
        else:
            lines.append("Причина: валидатор не подтвердил соответствие чеклисту.")
        lines.append("Уточните ограничения/критерии и запустите задачу заново.")
        return "\n".join(lines)

    def _evaluate_validation_gate(self, decision: ValidationDecision, developer_report: str) -> Dict[str, Any]:
        reasons: List[str] = []
        invalid_rows: List[Dict[str, str]] = []
        non_pass_rows: List[Dict[str, str]] = []
        missing_evidence_rows: List[Dict[str, str]] = []

        if str(decision.status or "").strip().upper() != "PASS":
            reasons.append("status_not_pass")
        if decision.blocking_issues:
            reasons.append("blocking_issues_present")
        checklist_table_present = self._has_checklist_table(developer_report)
        if not checklist_table_present:
            reasons.append("checklist_table_missing")
        if not decision.checklist_rows:
            reasons.append("checklist_missing")
        for row in decision.checklist_rows:
            item = str(row.get("item") or "").strip()
            status = str(row.get("status") or "").strip().upper()
            evidence = str(row.get("evidence") or "").strip()
            row_details = {
                "item": item,
                "status": status,
                "evidence": evidence,
            }
            if not item or status not in {"PASS", "PARTIAL", "FAIL"}:
                invalid_rows.append(dict(row_details))
                continue
            if status != "PASS":
                non_pass_rows.append(dict(row_details))
            if not evidence:
                missing_evidence_rows.append(dict(row_details))
        if invalid_rows:
            reasons.append("checklist_row_invalid")
        if non_pass_rows:
            reasons.append("checklist_non_pass")
        if missing_evidence_rows:
            reasons.append("checklist_evidence_missing")
        return {
            "passed": not reasons,
            "reasons": reasons,
            "checklist_table_present": checklist_table_present,
            "blocking_issue_count": len(decision.blocking_issues),
            "invalid_rows": invalid_rows,
            "non_pass_rows": non_pass_rows,
            "missing_evidence_rows": missing_evidence_rows,
        }

    def _serialize_validation_report(
        self,
        decision: ValidationDecision,
        gate_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "status": str(decision.status or "").strip().upper(),
            "summary": str(decision.summary or "").strip(),
            "blocking_issues": list(decision.blocking_issues or []),
            "checklist_rows": [dict(row or {}) for row in (decision.checklist_rows or []) if isinstance(row, dict)],
            "defects": [dict(defect or {}) for defect in (decision.defects or []) if isinstance(defect, dict)],
            "raw": dict(decision.raw or {}),
            "gate": dict(gate_payload or {}),
        }

    def _gate_passed(self, decision: ValidationDecision, developer_report: str) -> bool:
        gate_payload = self._evaluate_validation_gate(decision, developer_report)
        return bool(gate_payload.get("passed"))

    def _has_checklist_table(self, text: str) -> bool:
        raw = str(text or "")
        if "|" not in raw:
            return False
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return False
        header_idx = -1
        for idx, line in enumerate(lines):
            lowered = line.lower()
            if "пункт" in lowered and "статус" in lowered and "|" in line:
                header_idx = idx
                break
        if header_idx < 0:
            return False
        for row in lines[header_idx + 1:]:
            if "|" not in row:
                continue
            upper = row.upper()
            if "PASS" in upper or "PARTIAL" in upper or "FAIL" in upper:
                return True
        return False

    def _parse_validation_report(self, text: str) -> ValidationDecision:
        raw = str(text or "").strip()
        try:
            data = parse_normalize_validate(raw, WebmasterValidationReportSchema)
        except Exception:
            self._log.exception("webmaster validation report normalize/parse failed")
            data = self._smart_validation_fallback(raw)
            if data is None:
                raise RuntimeError("validation output JSON parse failed")
        if not isinstance(data, dict):
            raise RuntimeError("validation result is not JSON object")

        status = str(data.get("status") or "").strip().upper()
        if status not in {"PASS", "PARTIAL", "FAIL"}:
            raise RuntimeError("validation status must be PASS|PARTIAL|FAIL")
        blocking = [str(x).strip() for x in (data.get("blocking_issues") or []) if str(x).strip()]
        checklist_rows: List[Dict[str, str]] = []
        for item in (data.get("checklist_results") or []):
            if not isinstance(item, dict):
                continue
            checklist_rows.append(
                {
                    "item": str(item.get("item") or "").strip(),
                    "status": str(item.get("status") or "").strip().upper(),
                    "evidence": str(item.get("evidence") or "").strip(),
                    "fixed": str(item.get("fixed") or "").strip(),
                    "why_not_done": str(item.get("why_not_done") or "").strip(),
                }
            )
        defects: List[Dict[str, str]] = []
        for item in (data.get("defects") or []):
            if not isinstance(item, dict):
                continue
            defects.append(
                {
                    "severity": str(item.get("severity") or "").strip().lower(),
                    "title": str(item.get("title") or "").strip(),
                    "location": str(item.get("location") or "").strip(),
                    "why": str(item.get("why") or "").strip(),
                    "fix_hint": str(item.get("fix_hint") or "").strip(),
                }
            )
        return ValidationDecision(
            status=status,
            summary=str(data.get("summary") or "").strip(),
            blocking_issues=blocking,
            checklist_rows=checklist_rows,
            defects=defects,
            raw=data,
        )

    def _smart_validation_fallback(self, raw: str) -> Optional[Dict[str, Any]]:
        """Tolerant parser for partially valid validation reports."""
        try:
            data = loads_safe(str(raw or ""), strict_first=False)
        except Exception:
            self._log.exception("webmaster validation fallback parse failed")
            return None
        if not isinstance(data, dict):
            return None

        status = str(data.get("status") or "").strip().upper()
        if status not in {"PASS", "PARTIAL", "FAIL"}:
            return None

        def _to_str_list(value: Any) -> List[str]:
            if not isinstance(value, list):
                return []
            return [str(x).strip() for x in value if str(x).strip()]

        checklist_results: List[Dict[str, str]] = []
        for row in list(data.get("checklist_results") or []):
            if not isinstance(row, dict):
                continue
            row_status = str(row.get("status") or "").strip().upper()
            if row_status not in {"PASS", "PARTIAL", "FAIL"}:
                continue
            checklist_results.append(
                {
                    "item": str(row.get("item") or "").strip(),
                    "status": row_status,
                    "evidence": str(row.get("evidence") or "").strip(),
                    "fixed": str(row.get("fixed") or "").strip(),
                    "why_not_done": str(row.get("why_not_done") or row.get("why_not") or "").strip(),
                }
            )

        defects: List[Dict[str, str]] = []
        for row in list(data.get("defects") or []):
            if not isinstance(row, dict):
                continue
            defects.append(
                {
                    "severity": str(row.get("severity") or "").strip().lower(),
                    "title": str(row.get("title") or "").strip(),
                    "location": str(row.get("location") or "").strip(),
                    "why": str(row.get("why") or "").strip(),
                    "fix_hint": str(row.get("fix_hint") or "").strip(),
                }
            )

        return {
            "status": status,
            "summary": str(data.get("summary") or "").strip(),
            "blocking_issues": _to_str_list(data.get("blocking_issues")),
            "checklist_results": checklist_results,
            "defects": defects,
        }

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
        menu_visibility: Any = None,
    ) -> tuple[str, Any]:
        from .ui import build_webmaster_menu
        return build_webmaster_menu(
            session,
            back_callback,
            back_text,
            self.mode_id,
            menu_visibility=menu_visibility,
        )

    def _build_status_text(
        self,
        bot_app: Any,
        session: Any,
        chat_id: int,
        user_id: Any,
        *,
        context: Any,
    ) -> str:
        resolved_user_id = self._resolve_user_id(
            user_id=user_id,
            chat_id=chat_id,
            chat_type="",
            context=context,
            session=session,
        )
        wm_ctx = self._store(session).load(
            build_user_key(chat_id, resolved_user_id, str(getattr(session, "id", "") or "").strip() or None)
        )
        running = bool(self._mode_task_names(bot_app=bot_app, session=session))
        enabled = str(get_active_mode(session, "") or "").strip() == self.mode_id
        stage = ModeStatusService.build_webmaster_mode_stage(
            enabled=enabled,
            running=running,
            busy=bool(getattr(session, "busy", False)),
            queue_len=ModeStatusService.get_session_queue_len(session),
            wm_stage=str(wm_ctx.stage or "idle"),
        )
        return ModeStatusService.build_mode_status_text(
            session,
            title="🌐 Статус Вебмастера",
            stage=stage,
            enabled=enabled,
            task_suffix=f"Задача: {'активна' if running else 'нет'}",
            extra_sections=[
                ("Тип задачи", str(wm_ctx.task_kind or "unknown")),
                ("Версия промпта", str(wm_ctx.active_prompt_version)),
                ("Последняя классификация", str(wm_ctx.last_feedback_class or "нет")),
            ],
        )

    async def _rerender_menu(
        self,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        *,
        note: str = "",
    ) -> None:
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
