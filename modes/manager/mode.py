from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any, Awaitable, Callable, Dict, Optional

from app.mode_dependencies import ModeDependencies
from app.services.run_artifact_store import RunArtifactHandle
from app.services.project_prompts_service import (
    InvalidProjectPromptsError,
    ensure_project_prompts,
    load_mode_prompt_texts,
)
from agent.manager_core import (
    _MANAGER_RUN_RESUME_GUARD_SESSION_ATTR,
    manager_apply_persisted_plan_metadata,
    manager_legacy_phase_for_run_phase,
    manager_legacy_plan_sync_payload,
    manager_run_phase_for_plan,
    manager_run_plan_payload,
    manager_run_state_context_from_plan,
)

from modes.sdk.planning import (
    MANAGER_CONTINUE_TOKEN,
    ManagerDecomposeNormalizationError,
    archive_plan,
    format_manager_status_brief,
    needs_failed_resume_choice,
    needs_resume_choice,
    load_plan,
    save_plan,
)
from modes.sdk.services import (
    CodebaseContextService,
    CodebaseContextText,
    ErrorMessageService,
    ModeStatusService,
)
from modes.sdk.runtime.contracts import DevTask, ProjectAnalysis, ProjectPlan

from modes.manager.ui import build_manager_menu_with_back
from modes.sdk import BaseMode, CallbackModel, MessageModel, ToolResult
from modes.sdk.run_artifacts_mixin import MergeStrategy, RunArtifactsMixin
from modes.sdk.session_busy import is_session_busy
from session import session_runtime_uid, session_scoped_key
from sessions.session_state_access import get_active_mode
from utils.lang import resolve_user_lang
from utils.text import strip_ansi


class ManagerMode(BaseMode, RunArtifactsMixin):
    mode_id = "manager"
    _RUN_HANDLE_SESSION_ATTR = "_manager_mode_active_run_handle"
    display_name = "🏗 Менеджер"
    description = "Декомпозиция плана, управление фазами, тихий режим"
    _RESUME_OPT_CONTINUE = "Продолжить текущий план"
    _RESUME_OPT_CONTINUE_FAILED = "Продолжить остановленный план"
    _RESUME_OPT_NEW = "Начать новый план"
    _RESUME_OPT_CANCEL = "Отмена"
    _PENDING_TTL_SEC = 900
    _INVALID_PROMPTS_ENABLE_TEXT = (
        "❌ Не удалось включить Manager: повреждён файл project prompts "
        "`.cli-proxy/.manager/prompt/prompts.yaml`. "
        "Исправьте YAML и повторите включение."
    )
    _DECOMPOSE_RETRY_FALLBACK = (
        "Не удалось построить план: ответ декомпозиции не распознан как валидный JSON-план. "
        "Пришлите задачу заново в формате: outcome, ограничения, проверки."
    )

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)

    @staticmethod
    def _set_status(entity: Any, status: str) -> None:
        setter = getattr(entity, "set_status", None)
        if callable(setter):
            setter(status)
            return
        setattr(entity, "status", str(status or "").strip())

    @staticmethod
    def _plan_scoped_key(session: Any) -> str:
        return str(session_scoped_key(session) or "").strip()

    def _load_live_plan(self, session: Any) -> Optional[ProjectPlan]:
        return load_plan(getattr(session, "workdir", ""), scoped_key=self._plan_scoped_key(session))

    def _save_live_plan(self, session: Any, plan: ProjectPlan) -> None:
        save_plan(getattr(session, "workdir", ""), plan, scoped_key=self._plan_scoped_key(session))

    def _archive_live_plan(self, session: Any, status: str) -> Optional[str]:
        return archive_plan(getattr(session, "workdir", ""), status, scoped_key=self._plan_scoped_key(session))

    def framework_sends_output(self) -> bool:
        return False

    def build_runtime(self, config: Any) -> Any:
        from .runner_service import ManagerModeRunnerService
        return ManagerModeRunnerService(config)

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if not session or not bot_app:
            return None
        context = ctx.get("context")
        query = ctx.get("query")
        raw_chat_id = (
            ctx.get("chat_id")
            or (ctx.get("dest") or {}).get("chat_id")
            or getattr(getattr(query, "message", None), "chat_id", None)
        )
        chat_id = int(raw_chat_id) if raw_chat_id is not None else None
        ok = await self._ensure_project_prompts_ready(
            session=session,
            bot_app=bot_app,
            context=context,
            chat_id=chat_id,
            query=query,
        )
        if not ok:
            return ToolResult.fail("invalid_project_prompts")

        await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile=None)
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if not session or not bot_app:
            return None

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
        pending_store = self._manager_pending()

        ms = self._messaging(bot_app=bot_app, context=context)

        if await self._enqueue_if_busy(session=session, bot_app=bot_app, ms=ms, chat_id=chat_id, text=message.text, dest=dest):
            return ToolResult.ok()

        try:
            plan = self._load_live_plan(session)
        except Exception:
            plan = None
        auto_resume = bool(bot_app.config.defaults.manager_auto_resume)
        if needs_resume_choice(plan, auto_resume=auto_resume, user_text=message.text):
            session_uid = session_runtime_uid(session)
            pending_store.set(
                session_uid,
                {
                    "prompt": message.text,
                    "dest": dict(dest),
                    "created_at": time.time(),
                },
            )
            prompts = self._load_prompts(session=session)
            status = str(getattr(plan, "status", "") or "").strip()
            if status == "paused":
                header = str(prompts["resume_header_paused"])
            else:
                header = str(prompts["resume_header_active"])
            resume_question_tpl = str(prompts["resume_question_template"])
            try:
                choice = await self._request_resume_choice(
                    bot_app=bot_app,
                    session=session,
                    context=context,
                    dest=dest,
                    chat_id=chat_id,
                    question=resume_question_tpl.format(header=header),
                    continue_label=self._RESUME_OPT_CONTINUE,
                )
            except Exception:
                await ms.send_text(
                    chat_id,
                    "Не удалось обработать выбор продолжения плана. Повторите выбор.",
                    md2=True,
                )
                return ToolResult.ok()

            result, pending_consumed = await self._apply_resume_choice(
                choice=choice,
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                ms=ms,
                prompt=message.text,
                dest=dest,
            )
            if pending_consumed:
                try:
                    pending_store.delete(session_uid)
                except Exception:
                    self._log.exception("manager pending cleanup after resume failed")
            return result
        if needs_failed_resume_choice(plan, auto_resume=auto_resume, user_text=message.text):
            try:
                archived_path = self._archive_live_plan(session, "failed")
            except Exception:
                self._log.exception("manager archive failed on text fallback")
                await ms.send_text(chat_id, ErrorMessageService.manager_archive_failed(), md2=True)
                return ToolResult.ok()
            if not archived_path:
                self._log.error(
                    "manager archive returned empty path on text fallback session_id=%s workdir=%s",
                    getattr(session, "id", None),
                    getattr(session, "workdir", None),
                )
                await ms.send_text(chat_id, ErrorMessageService.manager_archive_failed(), md2=True)
                return ToolResult.ok()
            pending_store.pop(session_runtime_uid(session), None)

            await ms.send_text(
                chat_id,
                "📦 Текст получен вместо выбора кнопкой. План перенесён в архив, запускаю новый план...",
                md2=True,
            )

            async def _run_new_after_archive() -> None:
                pipeline = self._pipeline()
                await pipeline.run_mode_pipeline(
                    session,
                    message.text,
                    dict(dest),
                    context,
                    mode_id=self.mode_id,
                )

            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=_run_new_after_archive(),
                name="failed_archive_new_plan",
            )
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

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="run_manager")
        return ToolResult.ok()

    async def _request_resume_choice(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        dest: Dict[str, Any],
        chat_id: int,
        question: str,
        continue_label: str,
    ) -> str:
        tooling = self._tooling()
        try:
            return await tooling.ask_user(
                question=question,
                options=[continue_label, self._RESUME_OPT_NEW, self._RESUME_OPT_CANCEL],
                allow_custom=False,
                system_options=False,
                ctx=self._tool_ctx(
                    bot_app=bot_app,
                    session=session,
                    context=context,
                    dest=dest,
                    chat_id=chat_id,
                ),
            )
        except Exception:
            self._log.exception("manager ask_user resume choice failed")
            raise

    async def _apply_resume_choice(
        self,
        *,
        choice: str,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        ms: Any,
        prompt: str,
        dest: Dict[str, Any],
    ) -> tuple[ToolResult, bool]:
        picked = str(choice or "").strip()
        if picked in (self._RESUME_OPT_CONTINUE, self._RESUME_OPT_CONTINUE_FAILED):
            await ms.send_text(chat_id, "▶️ Продолжаю план...", md2=True)

            async def _run_continue() -> None:
                pipeline = self._pipeline()
                await pipeline.run_mode_pipeline(
                    session,
                    MANAGER_CONTINUE_TOKEN,
                    dict(dest),
                    context,
                    mode_id=self.mode_id,
                )

            self._start_mode_task(bot_app=bot_app, session=session, coro=_run_continue(), name="resume_continue")
            await self._rerender_menu(bot_app, session, chat_id, context, None, note="▶️ Продолжаю план...")
            return ToolResult.ok(), True

        if picked == self._RESUME_OPT_NEW:
            runtime_getter = self._runtime_getter()
            runtime = runtime_getter("manager_control")
            if runtime is None:
                await ms.send_text(chat_id, ErrorMessageService.manager_runtime_unavailable(), md2=True)
                return ToolResult.ok(), False
            try:
                runtime.reset(session)
            except Exception:
                self._log.exception("manager reset before resume_new failed")
            await ms.send_text(chat_id, "🆕 Начинаю новый план...", md2=True)

            async def _run_new() -> None:
                pipeline = self._pipeline()
                await pipeline.run_mode_pipeline(
                    session,
                    str(prompt or ""),
                    dict(dest),
                    context,
                    mode_id=self.mode_id,
                )

            self._start_mode_task(bot_app=bot_app, session=session, coro=_run_new(), name="resume_new")
            await self._rerender_menu(bot_app, session, chat_id, context, None, note="🆕 Начинаю новый план...")
            return ToolResult.ok(), True

        await ms.send_text(chat_id, "Ок, оставляю текущий план без изменений.", md2=True)
        return ToolResult.ok(), True

    async def run_pipeline(
        self,
        *,
        session: Any,
        user_text: str,
        bot_app: Any,
        context: Any,
        dest: Dict[str, Any],
    ) -> str:
        codebase_context = self._build_codebase_context(session=session)
        runtime_getter = self._runtime_getter()
        runtime = runtime_getter("run_manager")
        if runtime is None:
            raise RuntimeError("Manager runtime is not configured")
        prompt_text = str(user_text or "")
        if codebase_context:
            prompts = self._load_prompts(session=session)
            map_tail = str(prompts["run_pipeline_codebase_tail"])
            prompt_text = (
                f"{prompt_text}\n\n"
                "<CODEBASE_MAP>\n"
                f"{codebase_context}\n"
                "</CODEBASE_MAP>\n\n"
                f"{map_tail}"
            )
        run, resume_guard = self._prepare_run_artifacts(session=session, user_text=user_text, dest=dest)
        current_plan = None
        try:
            current_plan = self._load_live_plan(session)
        except Exception:
            current_plan = None
        current_phase = manager_run_phase_for_plan(current_plan, fallback="plan")
        self._save_run_state(
            run,
            phase=current_phase,
            status="running",
            mode_context={
                "execution_context": self._execution_context(session=session, dest=dest),
                "resume_guard": dict(resume_guard or {}),
                "source_user_text_preview": str(user_text or "")[:500],
                "prompt_preview": prompt_text[:500],
            },
        )
        self._sync_run_from_legacy_plan(run, plan=current_plan, phase=current_phase)
        try:
            result = await runtime.run(session, prompt_text, bot_app, context, dict(dest or {}))
        except ManagerDecomposeNormalizationError as exc:
            message = str(exc or "").strip() or self._DECOMPOSE_RETRY_FALLBACK
            try:
                prompts = self._load_prompts(session=session)
                prompt_message = str(prompts["decompose_retry_message"]).strip()
                if prompt_message:
                    message = prompt_message
            except Exception:
                self._log.exception(
                    "manager prompts load failed while handling decompose error session_id=%s workdir=%s",
                    getattr(session, "id", None),
                    getattr(session, "workdir", None),
                )
            self._log.warning("manager decompose normalization failed: %s", exc)
            chat_id = (dest or {}).get("chat_id")
            if chat_id is not None:
                ms = self._messaging(bot_app=bot_app, context=context)
                try:
                    await ms.send_text(chat_id, message, md2=True)
                except Exception:
                    self._log.exception("manager decompose error notification failed chat_id=%s", chat_id)
            self._save_run_state(
                run,
                phase=current_phase,
                status="failed",
                mode_context={"error_message": message},
            )
            self._mark_run_finished(run, status="failed", phase=current_phase)
            self._clear_active_run_handle(session)
            return message
        except asyncio.CancelledError:
            try:
                current_plan = self._load_live_plan(session)
            except Exception:
                current_plan = None
            if current_plan is None and run is not None:
                artifact_store = self._artifact_store()
                if artifact_store is not None:
                    current_phase = str(artifact_store.load_state(run).get("phase") or current_phase)
            else:
                current_phase = manager_run_phase_for_plan(current_plan, fallback=current_phase)
            self._sync_run_from_legacy_plan(run, plan=current_plan, phase=current_phase)
            cancelled_status = "cancelled" if str(getattr(current_plan, "status", "") or "").strip().lower() == "paused" else "failed"
            self._save_run_state(
                run,
                phase=current_phase,
                status=cancelled_status,
                mode_context={"execution_context": self._execution_context(session=session, dest=dest)},
            )
            self._mark_run_finished(run, status=cancelled_status, phase=current_phase)
            self._clear_active_run_handle(session)
            raise
        except Exception as exc:
            try:
                current_plan = self._load_live_plan(session)
            except Exception:
                current_plan = None
            if current_plan is None and run is not None:
                artifact_store = self._artifact_store()
                if artifact_store is not None:
                    current_phase = str(artifact_store.load_state(run).get("phase") or current_phase)
            else:
                current_phase = manager_run_phase_for_plan(current_plan, fallback=current_phase)
            self._sync_run_from_legacy_plan(run, plan=current_plan, phase=current_phase)
            self._save_run_state(
                run,
                phase=current_phase,
                status="failed",
                mode_context={
                    "execution_context": self._execution_context(session=session, dest=dest),
                    "error_message": str(exc or "")[:500],
                },
            )
            self._mark_run_finished(run, status="failed", phase=current_phase)
            self._clear_active_run_handle(session)
            raise

        try:
            current_plan = self._load_live_plan(session)
        except Exception:
            current_plan = None
        current_state = {}
        if run is not None:
            artifact_store = self._artifact_store()
            if artifact_store is not None:
                current_state = artifact_store.load_state(run)
        if current_plan is None:
            current_phase = str(current_state.get("phase") or current_phase)
        else:
            current_phase = manager_run_phase_for_plan(current_plan, fallback=current_phase)
        try:
            self._sync_run_from_legacy_plan(run, plan=current_plan, phase=current_phase)
            final_status = str(current_state.get("status") or "completed") or "completed"
            final_report = result
            if current_plan is not None:
                plan_status = str(getattr(current_plan, "status", "") or "").strip().lower()
                if plan_status == "failed":
                    final_status = "failed"
                elif plan_status == "paused":
                    final_status = "cancelled"
                elif plan_status == "completed":
                    final_status = "completed"
                report_text = str(getattr(current_plan, "completion_report", "") or "").strip()
                if report_text:
                    final_report = report_text
            self._save_run_state(
                run,
                phase=current_phase,
                status=final_status,
                mode_context={
                    "execution_context": self._execution_context(session=session, dest=dest),
                    "final_report": str(final_report or "").strip() or None,
                },
            )
            if final_status == "completed" and current_phase == "complete":
                self._validate_run_boundary(run, phase="complete")
            # Historical replay must stay isolated from the live session-scoped manager plan.
            self._mark_run_finished(run, status=final_status, phase=current_phase)
        finally:
            self._clear_active_run_handle(session)
        return result

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
        _ = state, report
        resolved_action = str(action or "").strip()
        if resolved_action != "replay_finalize":
            return {
                "status": "blocked",
                "message": f"Recovery action `{resolved_action}` не поддерживается Manager hook.",
                "executed_operation": resolved_action,
            }
        if run is None:
            return {
                "status": "blocked",
                "message": "Manager recovery не может быть выполнен: run artifacts отсутствуют.",
                "executed_operation": resolved_action,
            }
        recovery_node = self._manager_recovery_node_from_run(run, action=resolved_action)
        if recovery_node is None:
            return {
                "status": "blocked",
                "message": "Manager recovery не может replay finalize без сохранённого recovery snapshot в run artifacts.",
                "executed_operation": resolved_action,
            }
        legacy_plan = self._restore_manager_plan_snapshot(recovery_node.get("plan_snapshot"))
        if legacy_plan is None:
            return {
                "status": "blocked",
                "message": "Manager recovery не может replay finalize: сохранённый plan snapshot повреждён.",
                "executed_operation": resolved_action,
            }
        if not str(getattr(legacy_plan, "current_task_id", "") or "").strip():
            for task in reversed(list(getattr(legacy_plan, "tasks", []) or [])):
                task_id = str(getattr(task, "id", "") or "").strip()
                task_status = str(getattr(task, "status", "") or "").strip().lower()
                if task_id and task_status == "approved":
                    legacy_plan.current_task_id = task_id
                    break
        source_run_id = str(recovery_node.get("source_run_id") or run.run_id).strip() or run.run_id

        current_phase = manager_run_phase_for_plan(legacy_plan, fallback="complete")
        final_status = "completed"
        legacy_status = str(getattr(legacy_plan, "status", "") or "").strip().lower()
        if legacy_status == "failed":
            final_status = "failed"
        elif legacy_status == "paused":
            final_status = "cancelled"
        elif legacy_status == "completed":
            final_status = "completed"
        final_report = str(getattr(legacy_plan, "completion_report", "") or "").strip() or None

        recovery_prompt = str(
            final_report
            or getattr(legacy_plan, "project_goal", "")
            or f"Manager recovery replay finalize for {source_run_id}"
        ).strip() or f"Manager recovery replay finalize for {source_run_id}"
        recovery_run, _ = self._prepare_run_artifacts(
            session=session,
            user_text=recovery_prompt,
            dest=dest,
        )
        if recovery_run is None:
            return {
                "status": "blocked",
                "message": "Manager recovery не может создать новый recovery run.",
                "executed_operation": resolved_action,
            }
        artifact_store = self._artifact_store()
        assert artifact_store is not None
        recovery_nodes = {
            "replay_finalize": {
                "source_run_id": source_run_id,
                "phase": str(current_phase or "").strip() or "complete",
                "plan_snapshot": asdict(legacy_plan),
            }
        }
        source_plan_payload = artifact_store.load_plan(run)
        if isinstance(source_plan_payload, dict):
            source_plan_payload["legacy_plan_sync"] = manager_legacy_plan_sync_payload(legacy_plan)
            source_recovery_nodes = source_plan_payload.get("recovery_nodes")
            if isinstance(source_recovery_nodes, dict):
                source_recovery_nodes = dict(source_recovery_nodes)
            else:
                source_recovery_nodes = {}
            source_recovery_nodes["replay_finalize"] = recovery_nodes["replay_finalize"]
            source_plan_payload["recovery_nodes"] = source_recovery_nodes
            artifact_store.save_plan(run, source_plan_payload)
        artifact_store.save_plan(
            recovery_run,
            {
                **manager_run_plan_payload(legacy_plan, phase=current_phase),
                "recovery_nodes": recovery_nodes,
            },
        )
        self._save_run_state(
            recovery_run,
            phase=current_phase,
            status=final_status,
            mode_context={
                "recovery_request": {
                    "action": resolved_action,
                    "source_run_id": source_run_id,
                },
                "execution_context": self._execution_context(session=session, dest=dest),
                "final_report": final_report,
                "legacy_plan_sync": manager_legacy_plan_sync_payload(legacy_plan),
                "legacy_plan_sync_status": "synced",
                "recovery_nodes": {
                    "replay_finalize": {
                        "source_run_id": source_run_id,
                        "phase": str(current_phase or "").strip() or "complete",
                    }
                },
            },
        )
        if final_status == "completed" and current_phase == "complete":
            self._validate_run_boundary(recovery_run, phase="complete")
        self._mark_run_finished(recovery_run, status=final_status, phase=current_phase)
        self._clear_active_run_handle(session)
        return {
            "status": "ok",
            "message": (
                "Manager recovery replay finalize выполнен в отдельном recovery run "
                "по сохранённому historical snapshot."
            ),
            "executed_operation": resolved_action,
            "executed_via": "manager_replay_finalize",
            "spawned_run_id": recovery_run.run_id,
        }

    def _build_codebase_context(self, *, session: Any) -> str:
        prompts = self._load_prompts(session=session)
        text = CodebaseContextText(
            intro=str(prompts["codebase_intro"]),
            stack=str(prompts["codebase_stack"]),
            architecture=str(prompts["codebase_architecture"]),
            structure=str(prompts["codebase_structure"]),
            integrations=str(prompts["codebase_integrations"]),
            conventions=str(prompts["codebase_conventions"]),
            testing=str(prompts["codebase_testing"]),
            concerns=str(prompts["codebase_concerns"]),
            outro=str(prompts["codebase_outro"]),
        )
        return CodebaseContextService.build_context(
            session=session,
            runtime_getter=self._optional_runtime_getter(),
            text=text,
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
                "manager project prompts validation failed session_id=%s workdir=%s",
                getattr(session, "id", None),
                getattr(session, "workdir", None),
            )
            if chat_id is not None:
                try:
                    ms = self._messaging(bot_app=bot_app, context=context)
                    await ms.send_or_edit(
                        query=query,
                        chat_id=str(chat_id),
                        text=self._INVALID_PROMPTS_ENABLE_TEXT,
                        md2=True,
                    )
                except Exception:
                    self._log.exception("manager invalid prompts notification failed")
            return False
        except Exception:
            self._log.exception(
                "manager project prompts unexpected load failure session_id=%s workdir=%s",
                getattr(session, "id", None),
                getattr(session, "workdir", None),
            )
            if chat_id is not None:
                try:
                    ms = self._messaging(bot_app=bot_app, context=context)
                    await ms.send_or_edit(
                        query=query,
                        chat_id=str(chat_id),
                        text=self._INVALID_PROMPTS_ENABLE_TEXT,
                        md2=True,
                    )
                except Exception:
                    self._log.exception("manager prompts unexpected error notification failed")
            return False

    def _load_prompts(self, *, session: Any, lang: Optional[str] = None) -> Dict[str, str]:
        if lang is None:
            try:
                lang = resolve_user_lang(self.config, chat_id=getattr(session, "chat_id", None))
            except Exception:
                lang = "ru"
        return load_mode_prompt_texts(getattr(session, "workdir", ""), self.mode_id, lang)

    def _is_run_artifacts_enabled(self) -> bool:
        service = self._optional_run_artifacts()
        if service is None:
            return False
        try:
            return bool(service.is_enabled())
        except Exception:
            self._log.exception("manager run artifacts: failed to resolve enabled flag")
            return False

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
                    phase=str(latest_state.get("phase") or "develop"),
                )
                resume_guard["previous_run_repaired"] = True

        current_plan = None
        try:
            current_plan = self._load_live_plan(session)
        except Exception:
            current_plan = None
        initial_phase = manager_run_phase_for_plan(current_plan, fallback="plan")
        run = artifact_store.start_run(
            session=session,
            mode_id=self.mode_id,
            phase=initial_phase,
            source_prompt_hash=self._prompt_hash(user_text),
            mode_context={
                "dest_kind": str((dest or {}).get("kind") or "telegram"),
                "run_scope": "mode_pipeline",
                "legacy_phase": manager_legacy_phase_for_run_phase(initial_phase),
                "legacy_plan_sync_status": "missing",
                "resume_guard": dict(resume_guard or {}),
            },
        )
        self._set_active_run_handle(session, run)
        try:
            setattr(session, _MANAGER_RUN_RESUME_GUARD_SESSION_ATTR, dict(resume_guard or {}))
        except Exception:
            self._log.exception("manager run artifacts: failed to set resume guard session attr")
        return run, resume_guard

    def _diagnose_resume_boundary(self, run: RunArtifactHandle) -> Any:
        doctor = self._optional_run_doctor()
        if doctor is None or not doctor.is_enabled():
            return None
        artifact_store = self._artifact_store()
        try:
            state = artifact_store.load_state(run) if artifact_store is not None else {}
            phase = str((state or {}).get("phase") or "develop")
            return doctor.diagnose(run, mode_id=self.mode_id, phase=phase)
        except Exception:
            self._log.exception("manager run artifacts: doctor resume diagnosis failed run_id=%s", run.run_id)
            return None

    def _sync_run_from_legacy_plan(
        self,
        run: Optional[RunArtifactHandle],
        *,
        plan: Any,
        phase: Optional[str] = None,
        mode_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if run is None or plan is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            resolved_phase = manager_run_phase_for_plan(plan, fallback=phase or "plan")
            current = artifact_store.load_state(run)
            existing_plan_payload = artifact_store.load_plan(run)
            existing_plan_recovery_nodes = {}
            existing_plan_sync = {}
            if isinstance(existing_plan_payload, dict):
                raw_recovery_nodes = existing_plan_payload.get("recovery_nodes")
                if isinstance(raw_recovery_nodes, dict):
                    existing_plan_recovery_nodes = dict(raw_recovery_nodes)
                raw_plan_sync = existing_plan_payload.get("legacy_plan_sync")
                if isinstance(raw_plan_sync, dict):
                    existing_plan_sync = dict(raw_plan_sync)
            current_mode_context = current.get("mode_context")
            if isinstance(current_mode_context, dict):
                current_mode_context = dict(current_mode_context)
            else:
                current_mode_context = {}
            existing_state_sync = current_mode_context.get("legacy_plan_sync")
            if isinstance(existing_state_sync, dict) and not existing_plan_sync:
                existing_plan_sync = dict(existing_state_sync)
            existing_current_task_id = str(existing_plan_sync.get("current_task_id") or "").strip() or None
            merged_mode_context = dict(current_mode_context)
            merged_mode_context.update(manager_run_state_context_from_plan(plan, phase=resolved_phase))
            merged_mode_context.update(dict(mode_context or {}))
            merged_state_sync = merged_mode_context.get("legacy_plan_sync")
            if isinstance(merged_state_sync, dict) and existing_current_task_id:
                if not str(merged_state_sync.get("current_task_id") or "").strip():
                    merged_state_sync = dict(merged_state_sync)
                    merged_state_sync["current_task_id"] = existing_current_task_id
                    merged_mode_context["legacy_plan_sync"] = merged_state_sync
            recovery_nodes = self._manager_recovery_nodes(run, plan=plan, phase=resolved_phase)
            current_recovery_nodes = merged_mode_context.get("recovery_nodes")
            if isinstance(current_recovery_nodes, dict):
                merged_recovery_nodes = dict(current_recovery_nodes)
            else:
                merged_recovery_nodes = {}
            for node_key, node_payload in recovery_nodes.items():
                if isinstance(node_payload, dict):
                    merged_recovery_nodes.setdefault(
                        node_key,
                        {
                            "source_run_id": (
                                str(node_payload.get("source_run_id") or run.run_id).strip() or run.run_id
                            ),
                            "phase": str(node_payload.get("phase") or resolved_phase).strip() or resolved_phase,
                        },
                    )
            if merged_recovery_nodes:
                merged_mode_context["recovery_nodes"] = merged_recovery_nodes
            plan_payload = manager_run_plan_payload(plan, phase=resolved_phase)
            plan_sync = plan_payload.get("legacy_plan_sync")
            if isinstance(plan_sync, dict) and existing_current_task_id:
                if not str(plan_sync.get("current_task_id") or "").strip():
                    plan_sync = dict(plan_sync)
                    plan_sync["current_task_id"] = existing_current_task_id
                    plan_payload["legacy_plan_sync"] = plan_sync
            merged_plan_recovery_nodes = dict(existing_plan_recovery_nodes)
            if recovery_nodes:
                for node_key, node_payload in recovery_nodes.items():
                    merged_plan_recovery_nodes.setdefault(node_key, node_payload)
            if merged_plan_recovery_nodes:
                plan_payload["recovery_nodes"] = merged_plan_recovery_nodes
            artifact_store.save_plan(run, plan_payload)
            artifact_store.save_state(
                run,
                {
                    "phase": resolved_phase,
                    "status": current.get("status") or "running",
                    "mode_context": merged_mode_context,
                },
            )
        except Exception:
            self._log.exception("manager run artifacts: sync from legacy plan failed run_id=%s", run.run_id)

    @staticmethod
    def _manager_recovery_nodes(
        run: RunArtifactHandle,
        *,
        plan: Any,
        phase: str,
    ) -> Dict[str, Any]:
        if not isinstance(plan, ProjectPlan):
            return {}
        return {
            "replay_finalize": {
                "source_run_id": str(run.run_id or "").strip() or "",
                "phase": str(phase or "").strip() or "complete",
                "plan_snapshot": asdict(plan),
            }
        }

    def _manager_recovery_node_from_run(
        self,
        run: RunArtifactHandle,
        *,
        action: str,
    ) -> Optional[Dict[str, Any]]:
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None
        plan_payload = artifact_store.load_plan(run)
        recovery_nodes = plan_payload.get("recovery_nodes") if isinstance(plan_payload, dict) else None
        if not isinstance(recovery_nodes, dict):
            return None
        node = recovery_nodes.get(str(action or "").strip())
        return dict(node) if isinstance(node, dict) else None

    @staticmethod
    def _restore_manager_plan_snapshot(snapshot: Any) -> Optional[ProjectPlan]:
        if not isinstance(snapshot, dict):
            return None
        tasks_raw = snapshot.get("tasks")
        if not isinstance(tasks_raw, list):
            return None
        try:
            tasks = []
            for item in tasks_raw:
                if not isinstance(item, dict):
                    return None
                tasks.append(
                    DevTask(
                        id=str(item.get("id") or ""),
                        title=str(item.get("title") or ""),
                        description=str(item.get("description") or ""),
                        acceptance_criteria=[str(value).strip() for value in list(item.get("acceptance_criteria") or [])],
                        covers_requirements=[str(value).strip() for value in list(item.get("covers_requirements") or [])],
                        depends_on=[str(value).strip() for value in list(item.get("depends_on") or [])],
                        status=str(item.get("status") or "pending"),
                        attempt=int(item.get("attempt") or 0),
                        max_attempts=int(item.get("max_attempts") or 3),
                        dev_report=str(item.get("dev_report") or "").strip() or None,
                        review_verdict=str(item.get("review_verdict") or "").strip() or None,
                        review_comments=str(item.get("review_comments") or "").strip() or None,
                        rejection_history=[dict(value) for value in list(item.get("rejection_history") or []) if isinstance(value, dict)],
                        partial_work_note=str(item.get("partial_work_note") or "").strip() or None,
                        started_at=str(item.get("started_at") or "").strip() or None,
                        completed_at=str(item.get("completed_at") or "").strip() or None,
                        manager_change_audit=str(item.get("manager_change_audit") or "").strip() or None,
                        manager_change_audit_has_changes=(
                            bool(item.get("manager_change_audit_has_changes"))
                            if "manager_change_audit_has_changes" in item
                            else None
                        ),
                    )
                )
            analysis_raw = snapshot.get("analysis")
            analysis = None
            if isinstance(analysis_raw, dict):
                analysis = ProjectAnalysis(
                    current_state=str(analysis_raw.get("current_state") or ""),
                    already_done=[str(value).strip() for value in list(analysis_raw.get("already_done") or [])],
                    remaining_work=[str(value).strip() for value in list(analysis_raw.get("remaining_work") or [])],
                    requirements=[str(value).strip() for value in list(analysis_raw.get("requirements") or [])],
                    checklist_table=[dict(value) for value in list(analysis_raw.get("checklist_table") or []) if isinstance(value, dict)],
                )
            return ProjectPlan(
                project_goal=str(snapshot.get("project_goal") or ""),
                tasks=tasks,
                analysis=analysis,
                status=str(snapshot.get("status") or "active"),
                created_at=str(snapshot.get("created_at") or ""),
                updated_at=str(snapshot.get("updated_at") or ""),
                current_task_id=str(snapshot.get("current_task_id") or "").strip() or None,
                completion_report=str(snapshot.get("completion_report") or "").strip() or None,
            )
        except Exception:
            return None

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
        # manager всегда выполняет shallow-merge с phase-fallback "plan", поэтому параметр игнорируется.
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
                    "phase": str(phase or current.get("phase") or "plan"),
                    "status": str(status or current.get("status") or "running"),
                    "mode_context": merged_mode_context,
                },
            )
        except Exception:
            self._log.exception("manager run artifacts: save_state failed phase=%s run_id=%s", phase, run.run_id)

    def _validate_run_boundary(self, run: Optional[RunArtifactHandle], *, phase: str) -> None:
        if run is None:
            return
        validator = self._optional_run_boundary_validation()
        if validator is None or not validator.is_enabled():
            return
        report = validator.validate(run, mode_id=self.mode_id, phase=phase)
        if str(report.status or "") == "ok":
            return
        issues = ", ".join(issue.code for issue in report.issues)
        raise RuntimeError(f"Manager run boundary validation failed phase={phase}: {issues}")

    def _mark_run_finished(self, run: Optional[RunArtifactHandle], *, status: str, phase: str) -> None:
        if run is None:
            return
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return
        try:
            artifact_store.mark_finished(run, status=status, phase=phase)
        except Exception:
            self._log.exception("manager run artifacts: mark_finished failed run_id=%s", run.run_id)

    # _set_active_run_handle / _active_run_handle наследуются из RunArtifactsMixin
    # (используют _RUN_HANDLE_SESSION_ATTR == _MANAGER_RUN_HANDLE_SESSION_ATTR).
    def _clear_active_run_handle(self, session: Any) -> None:
        # Override: дополнительно очищает _MANAGER_RUN_RESUME_GUARD_SESSION_ATTR.
        super()._clear_active_run_handle(session)
        if hasattr(session, _MANAGER_RUN_RESUME_GUARD_SESSION_ATTR):
            setattr(session, _MANAGER_RUN_RESUME_GUARD_SESSION_ATTR, {})

    def _execution_context(self, *, session: Any, dest: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "dest_kind": str((dest or {}).get("kind") or "telegram"),
            "chat_id": (dest or {}).get("chat_id"),
            "project_root": str(getattr(session, "project_root", "") or "").strip() or None,
            "workdir": str(getattr(session, "workdir", "") or "").strip() or None,
            "session_scoped_key": self._plan_scoped_key(session) or None,
        }

    def _latest_mode_run(self, session: Any) -> Optional[RunArtifactHandle]:
        artifact_store = self._artifact_store()
        if artifact_store is None:
            return None
        latest = artifact_store.latest_run(session=session, mode_id=self.mode_id)
        if latest is not None and self._is_top_level_mode_run(artifact_store.load_state(latest)):
            return latest
        try:
            for handle in artifact_store.list_mode_runs(session=session, mode_id=self.mode_id):
                state = artifact_store.load_state(handle)
                if self._is_top_level_mode_run(state):
                    return handle
        except Exception:
            self._log.exception("manager run artifacts: latest top-level run lookup failed")
        return None

    @staticmethod
    def _is_top_level_mode_run(state: Dict[str, Any]) -> bool:
        mode_context = state.get("mode_context") if isinstance(state, dict) else {}
        if not isinstance(mode_context, dict):
            return False
        return str(mode_context.get("run_scope") or "").strip() == "mode_pipeline"

    def _save_legacy_plan_with_run_artifacts(self, session: Any, plan: Any, *, phase: Optional[str] = None) -> Any:
        self._save_live_plan(session, plan)
        try:
            persisted = self._load_live_plan(session)
        except Exception:
            persisted = plan
        if persisted is not None and plan is not None:
            plan = manager_apply_persisted_plan_metadata(plan, persisted)
        else:
            plan = persisted if persisted is not None else plan
        resolved_phase = manager_run_phase_for_plan(persisted, fallback=phase or "plan") if persisted is not None else (phase or "plan")
        self._sync_run_from_legacy_plan(self._active_run_handle(session), plan=persisted, phase=resolved_phase)
        self._save_run_state(
            self._active_run_handle(session),
            phase=resolved_phase,
            status="running",
            mode_context={"legacy_plan_sync": manager_legacy_plan_sync_payload(persisted)} if persisted is not None else {},
        )
        return plan

    async def handle_callback(self, callback: CallbackModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        query = ctx.get("query")
        chat_id = self._normalize_callback_chat_id(callback.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")

        ms = self._messaging(bot_app=bot_app, context=context)
        callback_user_id = int(callback.user_id) if getattr(callback, "user_id", None) is not None else None
        callback_dest = self._normalize_dest(ctx_dest=ctx.get("dest"), chat_id=chat_id, user_id=callback_user_id)
        if query is None and context is None and not ctx.get("dest"):
            callback_dest = {"kind": "desktop", "chat_id": chat_id}

        action = str(callback.action or "").strip()
        handlers = self._build_callback_handlers(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            ms=ms,
            callback_dest=callback_dest,
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
        callback_dest: Dict[str, Any],
    ) -> Dict[str, Callable[[], Awaitable[ToolResult]]]:
        return {
            "enable": lambda: self._cb_enable(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, ms=ms),
            "on": lambda: self._cb_enable(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, ms=ms),
            "disable": lambda: self._cb_disable(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query),
            "off": lambda: self._cb_disable(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query),
            "quiet_toggle": lambda: self._cb_quiet_toggle(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query),
            "pause": lambda: self._cb_pause(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, ms=ms),
            "resume_paused": lambda: self._cb_resume_paused(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                callback_dest=callback_dest,
            ),
            "resume_continue": lambda: self._cb_resume_continue(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                callback_dest=callback_dest,
            ),
            "failed_retry": lambda: self._cb_failed_retry(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                callback_dest=callback_dest,
            ),
            "resume_new": lambda: self._cb_resume_new(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                callback_dest=callback_dest,
            ),
            "resume_cancel": lambda: self._cb_resume_cancel(session=session, chat_id=chat_id, query=query, ms=ms),
            "reset": lambda: self._cb_reset(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, ms=ms),
            "status": lambda: self._cb_status(bot_app=bot_app, session=session, chat_id=chat_id, context=context, query=query, ms=ms),
            "failed_archive": lambda: self._cb_failed_archive(session=session, chat_id=chat_id, query=query, ms=ms),
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
    ) -> ToolResult:
        ok = await self._check_enable_requirements(
            bot_app=bot_app,
            session=session,
            ms=ms,
            query=query,
            chat_id=chat_id,
            require_openai=True,
            require_workdir=True,
            openai_error_text="Для работы Manager нужен OpenAI API. Настройте openai_api_key и openai_model в config.yaml.",
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
        # Политика переключения режимов централизована в ModeCallbackRouterService:
        # enable/disable допускаются только когда сессия не busy/locked/queued.
        # Поэтому здесь не отменяем "чужие" задачи других режимов.
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="Менеджер включен.")
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
        await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="Менеджер выключен.")
        return ToolResult.ok()

    async def _cb_quiet_toggle(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        current = bool(
            getattr(getattr(session, "modes", None), "manager_quiet_mode", getattr(session, "manager_quiet_mode", False))
        )
        session.modes.manager_quiet_mode = not current
        self._persist_sessions(bot_app)
        await self._rerender_menu(bot_app, session, chat_id, context, query)
        return ToolResult.ok()

    async def _cb_pause(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        runtime_getter = self._runtime_getter()
        runtime = runtime_getter("manager_control")
        if runtime is None:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.manager_runtime_unavailable(),
                md2=True,
            )
            return ToolResult.ok()
        try:
            runtime.pause(session)
        except Exception:
            # Keep behavior stable: swallow but log.
            self._log.exception("manager pause failed")
        try:
            await self._cancel_mode_tasks(
                bot_app=bot_app,
                session_id=session_runtime_uid(session),
                mode_id=self.mode_id,
                timeout_s=0.5,
            )
        except Exception:
            self._log.exception("manager pause cancel mode tasks failed")
        await self._rerender_menu(bot_app, session, chat_id, context, query)
        return ToolResult.ok()

    async def _cb_resume_paused(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        callback_dest: Dict[str, Any],
    ) -> ToolResult:
        if self._is_callback_run_busy(bot_app=bot_app, session=session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⚠️ План уже выполняется.",
                md2=True,
            )
            return ToolResult.ok()
        try:
            plan = self._load_live_plan(session)
            if not plan:
                await ms.send_or_edit(query=query, chat_id=chat_id, text="План не найден.", md2=True)
                return ToolResult.ok()
            if str(getattr(plan, "status", "") or "") != "paused":
                await self._rerender_menu(bot_app, session, chat_id, context, query)
                return ToolResult.ok()
            self._set_status(plan, "active")
            plan = self._save_legacy_plan_with_run_artifacts(session, plan, phase="develop")
        except Exception:
            self._log.exception("manager resume_paused failed")
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.manager_resume_failed(),
                md2=True,
            )
            return ToolResult.ok()

        await ms.send_or_edit(query=query, chat_id=chat_id, text="▶️ Продолжаю план...", md2=True)

        async def _run() -> None:
            pipeline = self._pipeline()
            await pipeline.run_mode_pipeline(
                session,
                MANAGER_CONTINUE_TOKEN,
                dict(callback_dest),
                context,
                mode_id=self.mode_id,
            )

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="resume_paused")
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="▶️ Продолжаю план...")
        return ToolResult.ok()

    async def _cb_resume_continue(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        callback_dest: Dict[str, Any],
    ) -> ToolResult:
        if self._is_callback_run_busy(bot_app=bot_app, session=session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⚠️ План уже выполняется.",
                md2=True,
            )
            return ToolResult.ok()

        pending = None
        try:
            pending = self._manager_pending().pop(session_runtime_uid(session), None)
        except Exception:
            pending = None
        if self._pending_is_stale(pending):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.stale_choice_resend_task(),
                md2=True,
            )
            return ToolResult.ok()
        return await self._cb_run_continue(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            ms=ms,
            action="resume_continue",
            message="▶️ Продолжаю план...",
            effective_dest=dict((pending or {}).get("dest") or callback_dest),
        )

    async def _cb_failed_retry(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        callback_dest: Dict[str, Any],
    ) -> ToolResult:
        try:
            plan = self._load_live_plan(session)
            if plan:
                changed = False
                for task in plan.tasks or []:
                    status = str(getattr(task, "status", "") or "").strip().lower()
                    if status == "failed":
                        if task.status != "pending" or int(getattr(task, "attempt", 0)) != 0:
                            changed = True
                        self._set_status(task, "pending")
                        task.attempt = 0
                        task.completed_at = None
                    elif status == "blocked":
                        if task.status != "pending":
                            changed = True
                        self._set_status(task, "pending")
                if changed:
                    plan = self._save_legacy_plan_with_run_artifacts(
                        session,
                        plan,
                        phase=manager_run_phase_for_plan(plan, fallback="develop"),
                    )
        except Exception:
            self._log.exception("manager failed_retry prepare failed")
        return await self._cb_run_continue(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            ms=ms,
            action="failed_retry",
            message="🔄 Повторяю выполнение плана...",
            effective_dest=dict(callback_dest),
        )

    async def _cb_run_continue(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        action: str,
        message: str,
        effective_dest: Dict[str, Any],
    ) -> ToolResult:
        if self._is_callback_run_busy(bot_app=bot_app, session=session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⚠️ План уже выполняется.",
                md2=True,
            )
            return ToolResult.ok()

        await ms.send_or_edit(query=query, chat_id=chat_id, text=message, md2=True)

        async def _run() -> None:
            pipeline = self._pipeline()
            await pipeline.run_mode_pipeline(
                session,
                MANAGER_CONTINUE_TOKEN,
                dict(effective_dest),
                context,
                mode_id=self.mode_id,
            )

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name=action)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note=message)
        return ToolResult.ok()

    def _is_callback_run_busy(self, *, bot_app: Any, session: Any) -> bool:
        run_lock = getattr(session, "run_lock", None)
        if is_session_busy(session, run_lock):
            return True
        return bool(self._mode_task_names(bot_app=bot_app, session=session))

    async def _cb_resume_new(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
        callback_dest: Dict[str, Any],
    ) -> ToolResult:
        if self._is_callback_run_busy(bot_app=bot_app, session=session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⚠️ План уже выполняется.",
                md2=True,
            )
            return ToolResult.ok()

        pending_store = self._manager_pending()
        pending = None
        session_uid = session_runtime_uid(session)
        try:
            pending = pending_store.pop(session_uid, None)
        except Exception:
            pending = None
        if not pending:
            try:
                pending_store.delete(session_uid)
            except Exception:
                self._log.exception("manager resume_new stale pending cleanup failed")
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.stale_choice_resend_task(),
                md2=True,
            )
            return ToolResult.ok()
        if self._pending_is_stale(pending):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.stale_choice_resend_task(),
                md2=True,
            )
            return ToolResult.ok()
        runtime_getter = self._runtime_getter()
        runtime = runtime_getter("manager_control")
        if runtime is None:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.manager_runtime_unavailable(),
                md2=True,
            )
            return ToolResult.ok()
        try:
            runtime.reset(session)
        except Exception:
            self._log.exception("manager reset before resume_new failed")
        await ms.send_or_edit(query=query, chat_id=chat_id, text="🆕 Начинаю новый план...", md2=True)

        async def _run() -> None:
            pipeline = self._pipeline()
            await pipeline.run_mode_pipeline(
                session,
                str(pending.get("prompt") or ""),
                pending.get("dest") or dict(callback_dest),
                context,
                mode_id=self.mode_id,
            )

        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="resume_new")
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="🆕 Начинаю новый план...")
        return ToolResult.ok()

    async def _cb_resume_cancel(
        self,
        *,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        try:
            self._manager_pending().delete(session_runtime_uid(session))
        except Exception:
            self._log.exception("manager resume_cancel pending cleanup failed")
        await ms.send_or_edit(query=query, chat_id=chat_id, text="Отменено.", md2=True)
        return ToolResult.ok()

    async def _cb_reset(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        if self._is_callback_run_busy(bot_app=bot_app, session=session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text="⚠️ Невозможно сбросить план, пока он выполняется.",
                md2=True,
            )
            return ToolResult.ok()

        runtime_getter = self._runtime_getter()
        runtime = runtime_getter("manager_control")
        if runtime is None:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.manager_runtime_unavailable(),
                md2=True,
            )
            return ToolResult.ok()
        try:
            runtime.reset(session)
        except Exception:
            self._log.exception("manager reset failed")
        await ms.send_or_edit(query=query, chat_id=chat_id, text="План сброшен.", md2=True)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note="План сброшен.")
        return ToolResult.ok()

    async def _cb_status(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        try:
            plan = self._load_live_plan(session)
        except Exception:
            plan = None
        running = bool(self._mode_task_names(bot_app=bot_app, session=session))
        enabled = str(get_active_mode(session, "") or "").strip() == self.mode_id
        status_full = ""
        if plan:
            status_full = format_manager_status_brief(plan)
        stage = ModeStatusService.build_manager_mode_stage(
            enabled=enabled,
            running=running,
            busy=bool(getattr(session, "busy", False)),
            queue_len=ModeStatusService.get_session_queue_len(session),
            plan_status=str(getattr(plan, "status", "") or "").strip() if plan else "",
        )
        header = ModeStatusService.build_mode_status_text(
            session,
            title="🏗 Статус Менеджера",
            stage=stage,
            enabled=enabled,
            task_suffix=f"Задача: {'активна' if running else 'нет'}",
            extra_sections=[
                (
                    "Тихий режим",
                    "вкл"
                    if bool(
                        getattr(
                            getattr(session, "modes", None),
                            "manager_quiet_mode",
                            getattr(session, "manager_quiet_mode", False),
                        )
                    )
                    else "выкл",
                ),
            ],
        )
        if status_full:
            status_full = f"{header}\n\nПлан:\n{status_full}"
        else:
            status_full = f"{header}\n\nПлан: не найден."
        limit = 3900
        suffix = "\n\n(Обрезано по лимиту Telegram. Остальное отправлено HTML-выводом.)"
        status_display = status_full
        truncated = False
        if len(status_display) > limit:
            truncated = True
            status_display = status_display[: max(0, limit - len(suffix))].rstrip() + "…" + suffix

        ok = True
        if query and getattr(query, "message", None):
            ok = bool(
                await ms.edit_text(
                    query.message.chat_id,
                    query.message.message_id,
                    status_display,
                    md2=True,
                )
            )
        else:
            await ms.send_text(chat_id, status_display, md2=True)
        if not ok:
            await ms.send_text(chat_id, status_display, md2=True)

        if truncated:
            dest = {"kind": "telegram", "chat_id": chat_id}

            async def _send_status_full_output() -> None:
                # TODO(M3): route large output via a transport-agnostic MessagingService.send_large_output when available.
                await bot_app.send_output(
                    session,
                    dest,
                    status_full,
                    context,
                    send_header=False,
                    force_html=True,
                    send_summary=False,
                )

            self._start_mode_task(
                bot_app=bot_app,
                session=session,
                coro=_send_status_full_output(),
                name="status_send_output",
            )
        return ToolResult.ok()

    async def _cb_failed_archive(
        self,
        *,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        try:
            archived_path = self._archive_live_plan(session, "failed")
        except Exception:
            self._log.exception("manager archive failed")
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.manager_archive_failed(),
                md2=True,
            )
            return ToolResult.ok()
        if not archived_path:
            self._log.error(
                "manager archive returned empty path on callback session_id=%s workdir=%s",
                getattr(session, "id", None),
                getattr(session, "workdir", None),
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=ErrorMessageService.manager_archive_failed(),
                md2=True,
            )
            return ToolResult.ok()
        await ms.send_or_edit(query=query, chat_id=chat_id, text="📦 План перенесён в архив.", md2=True)
        return ToolResult.ok()

    def _pending_is_stale(self, pending: Any) -> bool:
        if pending is None:
            return True
        if not isinstance(pending, dict):
            return True
        raw_created_at = pending.get("created_at")
        try:
            created_at = float(raw_created_at)
        except Exception:
            return True
        if created_at <= 0:
            return True
        return (time.time() - created_at) > float(self._PENDING_TTL_SEC)

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
            note_formatter=strip_ansi,
        )

    def _tool_ctx(
        self,
        *,
        bot_app: Any,
        session: Any,
        context: Any,
        dest: Dict[str, Any],
        chat_id: int,
    ) -> Dict[str, Any]:
        return {
            "cwd": getattr(session, "workdir", None),
            "state_root": getattr(session, "workdir", None),
            "session_id": getattr(session, "id", None),
            "chat_id": chat_id,
            "chat_type": dest.get("chat_type"),
            "bot": bot_app,
            "context": context,
            "session": session,
            "allowed_tools": ["All"],
            "corr_id": f"manager:{getattr(session, 'id', 'unknown')}:resume_choice",
        }

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
        menu_visibility: Any = None,
    ) -> tuple[str, Any]:
        plan_status = None
        try:
            plan = self._load_live_plan(session)
            plan_status = plan.status if plan else None
        except Exception:
            self._log.exception("manager build_menu load_plan failed")
            plan_status = None
        return build_manager_menu_with_back(
            session,
            back_callback=back_callback,
            back_text=back_text,
            plan_status=plan_status,
            mode_id=self.mode_id,
            menu_visibility=menu_visibility,
        )
