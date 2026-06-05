from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, Optional

import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.mode_dependencies import ModeDependencies
from app.services.run_artifact_store import RunArtifactHandle, RunArtifactStore
from app.services.telegram_ui_scope import TelegramUiKey
from modes.sdk import BaseMode, CallbackModel, MessageModel, ToolResult, decode_mode_dirs, encode_mode_dirs
from modes.sdk.json_store import read_json_locked
from modes.sdk.run_artifacts_mixin import MergeStrategy, RunArtifactsMixin
from modes.sdk.runtime.json_normalizer import parse_normalize_validate
from modes.sdk.session_busy import is_session_busy
from modes.sdk.services import ModeStatusService
from modes.sdk.services.callback_data import (
    build_mode_action_callback_data,
    build_session_mode_pick_callback_data,
)
from session import session_runtime_uid, session_scoped_key
from sessions.session_state_access import get_active_mode
from app.services.runtime_progress_service import build_runtime_progress_payload
from modes.agent.ui import build_agent_menu, build_agent_status_payload, build_agent_status_text
from i18n import t
from utils.lang import resolve_user_lang
from utils.paths import is_within_root

_AGENT_RUN_RESUME_GUARD_SESSION_ATTR = "agent_run_resume_guard"
_AGENT_PROJECT_SELECTION_STALE_KEY = "agent.msg.project_selection_stale"


def _optional_int(value: Any) -> Optional[int]:
    try:
        resolved = int(value) if value is not None else 0
    except Exception:
        resolved = 0
    return resolved if resolved > 0 else None


def agent_project_session_key(session: Any) -> str:
    key = str(session_scoped_key(session) or "").strip()
    if not key:
        key = str(getattr(session, "id", "") or "").strip()
    return key


def agent_project_scope_key(chat_id: Any, message_thread_id: Any = None) -> str:
    ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
    thread_token = int(ui_key.message_thread_id or 0)
    return f"{int(ui_key.chat_id)}:{thread_token}"


def normalize_agent_project_pending_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        session_id = str(raw.get("session_id") or "").strip()
        session_scoped = str(
            raw.get("session_scoped_key")
            or raw.get("scoped_key")
            or raw.get("session_key")
            or ""
        ).strip()
        ui_chat_id = _optional_int(raw.get("ui_chat_id", raw.get("chat_id")))
        message_thread_id = _optional_int(raw.get("message_thread_id"))
        if not session_id and not session_scoped:
            return None
        return {
            "session_id": session_id,
            "session_scoped_key": session_scoped,
            "ui_chat_id": ui_chat_id,
            "message_thread_id": message_thread_id,
        }
    token = str(raw or "").strip()
    if not token:
        return None
    return {
        "session_id": token,
        "session_scoped_key": "",
        "ui_chat_id": None,
        "message_thread_id": None,
    }


class AgentMode(BaseMode, RunArtifactsMixin):
    mode_id = "agent"
    _RUN_HANDLE_SESSION_ATTR = "agent_run_artifact_handle"
    display_name = "🤖 Агент"
    description = "ИИ-агент (оркестратор) с инструментами и планированием"

    def __init__(self, dependencies: Optional[ModeDependencies] = None) -> None:
        super().__init__(dependencies)
        self._log = logging.getLogger(__name__)
        self._prompts: Dict[str, str] = {}
        self._pending_project_by_scope: Dict[str, Any] = {}

    def allows_agent_plugin_ui(self) -> bool:
        return True

    def framework_sends_output(self) -> bool:
        return False

    def _extract_plugin_id(self, payload: Dict[str, Any]) -> str:
        pid, _sid = self._extract_plugin_context(payload)
        return pid

    def _extract_plugin_session_id(self, payload: Dict[str, Any]) -> str:
        _pid, sid = self._extract_plugin_context(payload)
        return sid

    def _extract_plugin_context(self, payload: Dict[str, Any]) -> tuple[str, str]:
        schema = {
            "type": "object",
            "properties": {
                "p": {"type": "string"},
                "s": {"type": "string"},
                "value": {"type": "string", "default": ""},
            },
            "additionalProperties": True,
        }
        try:
            normalized = parse_normalize_validate(
                json.dumps(payload or {}, ensure_ascii=False),
                schema,
            )
            pid = str(normalized.get("p") or "").strip()
            sid = str(normalized.get("s") or "").strip()
            value = str(normalized.get("value") or "").strip()
            if value:
                parsed = self._parse_compact_callback_payload(value)
                if not sid:
                    sid = str(parsed.get("s") or "").strip()
                if not pid:
                    pid = str(parsed.get("p") or "").strip()
            return pid, sid
        except Exception:
            self._log.exception("agent plugin callback payload parse failed payload=%r", payload)
            return "", ""

    @staticmethod
    def _parse_compact_callback_payload(value: str) -> Dict[str, str]:
        pairs: Dict[str, str] = {}
        raw = str(value or "").strip()
        if not raw:
            return pairs
        for chunk in raw.replace("&", "|").split("|"):
            token = str(chunk or "").strip()
            if not token or "=" not in token:
                continue
            key, val = token.split("=", 1)
            k = str(key or "").strip()
            if not k:
                continue
            pairs[k] = str(val or "").strip()
        return pairs

    @staticmethod
    def _plugin_session_payload(session: Any, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        session_uid = str(session_runtime_uid(session) or "").strip()
        if session_uid:
            payload["s"] = session_uid
        payload.update(dict(extra or {}))
        return payload

    def _resolve_plugin_callback_session(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        payload: Dict[str, Any],
    ) -> tuple[Any, bool]:
        sid = self._extract_plugin_session_id(payload or {})
        if not sid:
            return session, False
        _ = bot_app
        try:
            resolved = self._agent_runtime().get_session_by_uid(sid, chat_id=int(chat_id))
        except Exception:
            self._log.exception(
                "agent plugin callback session lookup service failed chat_id=%s sid=%s",
                chat_id,
                sid,
            )
            resolved = None
        if resolved is None:
            return None, True
        resolved_chat_id = getattr(resolved, "chat_id", None)
        if resolved_chat_id is not None and int(resolved_chat_id) != int(chat_id):
            self._log.warning(
                "agent plugin callback session ownership mismatch: chat_id=%s resolved_chat_id=%s sid=%s",
                chat_id, resolved_chat_id, sid,
            )
            return None, True
        return resolved, False

    def build_runtime(self, config: Any) -> Any:
        from .runner_service import AgentModeRunnerService
        return AgentModeRunnerService(config)

    async def on_enable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session and bot_app:
            await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile=None)
        return None

    async def on_disable(self, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        session = ctx.get("session")
        bot_app = ctx.get("bot_app")
        if session:
            try:
                scope = getattr(session, "conversation_scope", None)
                chat_id = int(
                    getattr(session, "chat_id", 0)
                    or getattr(scope, "chat_id", 0)
                    or 0
                )
                self._clear_pending_project_session(
                    chat_id,
                    session=session,
                    clear_all_for_session=True,
                )
            except Exception:
                self._log.exception("agent pending project cleanup on_disable failed")
        if session and bot_app and self._is_mode_active(session):
            await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        return None

    async def handle_input(self, message: MessageModel, ctx: Dict[str, Any]) -> ToolResult:
        bot_app = ctx.get("bot_app")
        session = ctx.get("session")
        context = ctx.get("context")
        chat_id = self._normalize_callback_chat_id(message.chat_id)
        if not bot_app or not session:
            return ToolResult.fail("missing_context")
        ms = self._messaging(bot_app=bot_app, context=context)
        msg_user_id = int(message.user_id) if getattr(message, "user_id", None) is not None else None

        dest = self._normalize_dest(ctx_dest=ctx.get("dest"), chat_id=chat_id, user_id=msg_user_id)

        # Minimal queueing behavior for parity: do not start parallel agent runs.
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

        # TaskService is the source of truth for agent background work under the new architecture.
        self._start_mode_task(bot_app=bot_app, session=session, coro=_run(), name="run_agent")
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
        runtime_getter = self._runtime_getter()
        runtime = runtime_getter("run_agent")
        if runtime is None:
            raise RuntimeError("Agent runtime is not configured")
        try:
            run, resume_guard = self._prepare_run_artifacts(
                session=session,
                user_text=user_text,
                dest=dest,
            )
        except Exception:
            self._clear_active_run_handle(session)
            raise
        phase = "plan"
        prompt_text = str(user_text or "")
        try:
            self._save_run_state(
                run,
                phase="plan",
                status="running",
                mode_context={
                    "source_prompt": prompt_text,
                    "cli_work_type": self._cli_work_type(session),
                    "executor_profile": self._executor_profile(session),
                    "required_use_cli_steps": [],
                    "blocking_clarification_open": False,
                    "blocking_clarifications": self._blocking_clarification_context(bot_app=bot_app, session=session),
                    "resume_guard": dict(resume_guard or {}),
                    "execution_context": self._execution_context(session=session, dest=dest),
                },
            )
            self._save_run_plan(
                run,
                {
                    "kind": "agent_orchestrator",
                    "units": [
                        {
                            "id": "agent:orchestrator",
                            "step_type": "orchestrator",
                            "title": "Run agent orchestrator pipeline",
                        }
                    ],
                },
            )
            self._validate_run_boundary(run, phase="plan")

            phase = "execute"
            self._save_run_state(
                run,
                phase="execute",
                status="running",
                mode_context={
                    "blocking_clarification_open": False,
                    "blocking_clarifications": self._blocking_clarification_context(bot_app=bot_app, session=session),
                    "execution_context": self._execution_context(session=session, dest=dest),
                },
            )
            output = await runtime.run(session, prompt_text, bot_app, context, dict(dest or {}))
            self._ensure_execute_boundary_evidence(run, output=output)
            self._validate_run_boundary(run, phase="execute")

            phase = "complete"
            blocking_context = self._blocking_clarification_context(bot_app=bot_app, session=session)
            self._save_run_state(
                run,
                phase="complete",
                status="running",
                mode_context={
                    "cli_work_type": self._cli_work_type(session),
                    "executor_profile": self._executor_profile(session),
                    "blocking_clarification_open": bool(blocking_context.get("count")),
                    "blocking_clarifications": blocking_context,
                    "final_deliverable": str(output or ""),
                    "execution_context": self._execution_context(session=session, dest=dest),
                },
            )
            self._validate_run_boundary(run, phase="complete")
            self._mark_run_finished(run, status="completed", phase="complete")
            return str(output or "")
        except Exception as exc:
            error_blocking_context = self._blocking_clarification_context(bot_app=bot_app, session=session)
            self._save_run_state(
                run,
                phase=phase,
                status="failed",
                mode_context={
                    "runtime_error": str(exc or ""),
                    "blocking_clarification_open": bool(error_blocking_context.get("count")),
                    "blocking_clarifications": error_blocking_context,
                },
            )
            self._mark_run_finished(run, status="failed", phase=phase)
            raise
        finally:
            self._clear_active_run_handle(session)

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
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=int((dest or {}).get("chat_id") or 0))
        if resolved_action not in {"rollback_to_checkpoint", "restart_from_phase"}:
            return {
                "status": "blocked",
                "message": t("agent.msg.recovery_action_unsupported", lang, action=resolved_action),
                "executed_operation": resolved_action,
            }
        prompt_text = self._recovery_prompt_from_state(action=resolved_action, state=state)
        if not prompt_text:
            return {
                "status": "blocked",
                "message": t("agent.msg.recovery_no_prompt", lang),
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
            "message": str(output or "").strip() or t("agent.msg.operation_done", lang, action=resolved_action),
            "executed_operation": resolved_action,
            "executed_via": f"agent_recovery_hook:{resolved_action}",
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
            prompt = str(
                execution_context.get("runner_prompt_preview")
                or mode_context.get("source_prompt")
                or ""
            ).strip()
        if not prompt:
            prompt = str(mode_context.get("source_prompt") or "").strip()
        if not prompt:
            prompt = str(execution_context.get("user_text_preview") or "").strip()
        if not prompt:
            prompt = str(execution_context.get("runner_prompt_preview") or "").strip()
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
            phase=str(state.get("phase") or "plan"),
            status=str(state.get("status") or "running"),
            mode_context={
                "recovery_request": {
                    "source_run_id": str(source_run_id or "").strip() or None,
                    "action": str(action or "").strip(),
                    "prompt_preview": str(prompt_text or "").strip()[:500],
                }
            },
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
            "project_connect": lambda: self._cb_project_connect(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                prompts=prompts,
                payload=payload,
            ),
            "project_change": lambda: self._cb_project_connect(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
                ms=ms,
                prompts=prompts,
                payload=payload,
            ),
            "project_pick": lambda: self._cb_project_pick(
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
            "project_disconnect": lambda: self._cb_project_disconnect(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
            ),
            "clean_all": lambda: self._cb_clean_all(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
            ),
            "clean_session": lambda: self._cb_clean_session(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                context=context,
                query=query,
            ),
            "plugins": lambda: self._cb_plugins(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
                payload=payload,
            ),
            "plugin": lambda: self._cb_plugin(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
                payload=payload,
            ),
            "doctor": lambda: self._cb_run_operation(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
                operation="doctor",
            ),
            "recover": lambda: self._cb_run_operation(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
                operation="recover",
            ),
            "resume": lambda: self._cb_run_operation(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
                operation="resume",
            ),
            "promote_skills": lambda: self._cb_promote_skills(
                bot_app=bot_app,
                session=session,
                chat_id=chat_id,
                query=query,
                ms=ms,
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
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
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
                or t("agent.msg.openai_required", lang)
            ),
            workdir_error_text=str(
                prompts.get("workdir_error_text")
                or t("agent.msg.session_required", lang)
            ),
        )
        if not ok:
            return ToolResult.ok()
        await self._activate_mode(session=session, bot_app=bot_app, cli_work_type=None, executor_profile=None)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note=t("agent.msg.agent_enabled", lang))
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
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        self._clear_pending_project_session(chat_id, query=query, context=context, session=session, clear_all_for_session=True)
        await self._deactivate_mode(session=session, bot_app=bot_app, cancel_tasks=True, timeout_s=0.2)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note=t("agent.msg.agent_disabled", lang))
        return ToolResult.ok()

    async def _cb_project_connect(
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
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        if not await self._validate_project_selection_callback(
            session=session,
            chat_id=chat_id,
            query=query,
            context=context,
            ms=ms,
            payload=payload,
            lang=lang,
        ):
            return ToolResult.ok()
        if self._is_project_switch_blocked(session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.session_busy_switch", lang),
                md2=True,
            )
            return ToolResult.ok()

        # Non-admin users can only pick from pre-configured projects.
        if not bot_app.is_admin(chat_id):
            projects = list(getattr(bot_app, "user_projects", lambda _cid: [])(chat_id) or [])
            if not projects:
                await ms.send_or_edit(
                    query=query,
                    chat_id=chat_id,
                    text=str(
                        prompts.get("projects_not_configured")
                        or t("agent.msg.projects_not_configured", lang)
                    ),
                    md2=True,
                )
                return ToolResult.ok()
            rows = []
            session_key = agent_project_session_key(session)
            if not session_key:
                await ms.send_or_edit(
                    query=query, chat_id=chat_id,
                    text=t("agent.msg.session_not_initialized", lang),
                    md2=True,
                )
                return ToolResult.ok()
            for i, p in enumerate(projects):
                label = self._project_label(str(p), max_len=60)
                project_payload = f"sk={session_key}|idx={i}"
                rows.append(
                    [
                        InlineKeyboardButton(
                            label,
                            callback_data=build_mode_action_callback_data(
                                self.mode_id,
                                "project_pick",
                                session=session,
                                payload=project_payload,
                            ),
                        )
                    ]
                )
            rows.append([InlineKeyboardButton(t("common.back", lang), callback_data=build_session_mode_pick_callback_data(session))])
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=str(prompts.get("project_select_text") or t("agent.msg.select_project", lang)),
                md2=True,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return ToolResult.ok()
        self._set_pending_project_session(chat_id, session=session, query=query, context=context)
        dirs_flow = self._dirs_flow()
        try:
            # Inform in place, then show the dirs menu as a separate UI.
            if query and getattr(query, "message", None):
                await ms.edit_text(
                    query.message.chat_id,
                    query.message.message_id,
                    str(prompts.get("project_dir_select_text") or t("agent.msg.select_project_dir", lang)),
                    md2=True,
                )
            else:
                await ms.send_text(
                    chat_id,
                    str(prompts.get("project_dir_select_text") or t("agent.msg.select_project_dir", lang)),
                    md2=True,
                )
        except Exception:
            self._log.exception("agent project connect prompt failed")
        try:
            await dirs_flow.start_flow(
                chat_id=chat_id,
                context=context,
                root=bot_app.config.defaults.workdir,
                mode_token=encode_mode_dirs("agent", "project"),
            )
        except Exception:
            self._clear_pending_project_session(chat_id, query=query, context=context, session=session)
            self._log.exception("agent project connect dirs flow start failed")
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.project_open_failed", lang),
                md2=True,
            )
        return ToolResult.ok()

    async def _cb_project_pick(
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
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        if not await self._validate_project_selection_callback(
            session=session,
            chat_id=chat_id,
            query=query,
            context=context,
            ms=ms,
            payload=payload,
            lang=lang,
        ):
            return ToolResult.ok()
        if self._is_project_switch_blocked(session):
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.session_busy_switch", lang),
                md2=True,
            )
            return ToolResult.ok()
        projects = list(getattr(bot_app, "user_projects", lambda _cid: [])(chat_id) or [])
        payload_data = self._extract_project_callback_payload(payload)
        try:
            idx = int(payload_data.get("idx", -1))
        except Exception:
            idx = -1
        if idx < 0 or idx >= len(projects):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.pick_unavailable", lang), md2=True)
            return ToolResult.ok()
        ok, msg = self._set_project_root(bot_app, session, chat_id, context, projects[idx], lang=lang)
        await self._rerender_menu(
            bot_app,
            session,
            chat_id,
            context,
            query,
            note=msg if ok else t("agent.msg.project_connect_failed", lang),
        )
        return ToolResult.ok()

    @staticmethod
    def _is_project_switch_blocked(session: Any) -> bool:
        run_lock = getattr(session, "run_lock", None)
        return is_session_busy(session, run_lock)

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
        queue_len = ModeStatusService.get_session_queue_len(session)
        pending_questions = self._pending_questions_map(session=session, chat_id=chat_id)
        status_payload = build_agent_status_payload(
            session,
            mode_id=self.mode_id,
            agent_running=running,
            pending_questions=pending_questions,
            active_plugin_flow=self._status_active_plugin_flow(
                session=session,
                chat_id=chat_id,
            ),
            queue_len=queue_len,
            runtime_progress=build_runtime_progress_payload(session, recent_limit=10),
        )
        try:
            _lang = resolve_user_lang(bot_app.config, chat_id=chat_id)
        except Exception:
            _lang = "ru"
        text = build_agent_status_text(
            session,
            mode_id=self.mode_id,
            agent_running=running,
            pending_questions=pending_questions,
            active_plugin_flow=str(status_payload.get("active_plugin_flow") or ""),
            runtime_progress=status_payload.get("runtime_progress"),
            lang=_lang,
        )
        text = (
            f"{text}\n{t('session_status.plugins_ui', _lang)}: "
            f"{t('session_status.plugins_available', _lang) if self.allows_agent_plugin_ui() else t('session_status.none', _lang)}"
        )
        await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        return ToolResult.ok()

    async def _cb_project_disconnect(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        ok, msg = self._set_project_root(bot_app, session, chat_id, context, None, lang=lang)
        await self._rerender_menu(
            bot_app, session, chat_id, context, query,
            note=msg if ok else t("agent.msg.project_disconnect_failed", lang),
        )
        return ToolResult.ok()

    async def _cb_clean_all(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        ms = self._messaging(bot_app=bot_app, context=context)
        if not self._is_agent_active(session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.agent_not_active", lang), md2=True)
            return ToolResult.ok()
        runtime = self._agent_runtime()
        scoped_key = session_scoped_key(session)
        try:
            runtime.interrupt_session(session.id, chat_id, context)
            runtime.clear_session_cache(scoped_key or session.id)
        except Exception:
            self._log.exception("agent clean_all pre-clean interrupt failed")
        removed, errors = runtime.clear_sandbox(chat_id=chat_id)
        msg = t("agent.msg.sandbox_cleared", lang, removed=removed)
        if errors:
            msg += t("agent.msg.sandbox_cleared_errors", lang, errors=errors)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note=msg)
        return ToolResult.ok()

    async def _cb_clean_session(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        query: Any,
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        ms = self._messaging(bot_app=bot_app, context=context)
        if not self._is_agent_active(session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.agent_not_active", lang), md2=True)
            return ToolResult.ok()
        runtime = self._agent_runtime()
        scoped_key = session_scoped_key(session)
        try:
            runtime.interrupt_session(session.id, chat_id, context)
            runtime.clear_session_cache(scoped_key or session.id)
        except Exception:
            self._log.exception("agent clean_session pre-clean interrupt failed")
        ok = runtime.clear_session_files(scoped_key or session.id)
        msg = t("agent.msg.session_files_cleared", lang) if ok else t("agent.msg.session_files_clear_failed", lang)
        await self._rerender_menu(bot_app, session, chat_id, context, query, note=msg)
        return ToolResult.ok()

    async def _cb_plugins(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        target_session, session_missing = self._resolve_plugin_callback_session(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            payload=payload,
        )
        if session_missing or target_session is None:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.session_menu_unavailable", lang),
                md2=True,
            )
            return ToolResult.ok()
        if not self._is_agent_active(target_session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.agent_not_active", lang), md2=True)
            return ToolResult.ok()
        try:
            from modes.sdk.runtime.profiles import build_default_profile

            tool_registry = self._tool_registry()
            profile = build_default_profile(bot_app.config, tool_registry)
            runtime_getter = self._runtime_getter()
            runtime = runtime_getter("plugin_ui")
            if runtime is None:
                await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.runtime_unavailable", lang), md2=True)
                return ToolResult.ok()
            ui = runtime.get_plugin_ui(profile)
            plugin_menu = ui.get("plugin_menu") or []
            if not plugin_menu:
                await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.no_plugins", lang), md2=True)
                return ToolResult.ok()
            rows = [
                # Prefix payload so it never parses as JSON (e.g. "null"/"true"/numbers").
                [
                    InlineKeyboardButton(
                        entry["label"],
                        callback_data=build_mode_action_callback_data(
                            self.mode_id,
                            "plugin",
                            payload=self._plugin_session_payload(
                                target_session,
                                {"p": entry["plugin_id"]},
                            ),
                        ),
                    )
                ]
                for entry in plugin_menu
            ]
            rows.append([InlineKeyboardButton(t("common.back", lang), callback_data=build_session_mode_pick_callback_data(target_session))])
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.plugins_header", lang),
                md2=True,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except Exception as e:
            self._log.exception("agent plugins menu failed: %s", e)
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.plugins_list_failed", lang), md2=True)
        return ToolResult.ok()

    async def _cb_plugin(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
        payload: Dict[str, Any],
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        target_session, session_missing = self._resolve_plugin_callback_session(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            payload=payload,
        )
        if session_missing or target_session is None:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.session_menu_unavailable", lang),
                md2=True,
            )
            return ToolResult.ok()
        if not self._is_agent_active(target_session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.agent_not_active", lang), md2=True)
            return ToolResult.ok()
        pid = self._extract_plugin_id(payload or {})
        if not pid:
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.plugin_unavailable", lang), md2=True)
            return ToolResult.ok()
        try:
            from modes.sdk.runtime.profiles import build_default_profile

            tool_registry = self._tool_registry()
            profile = build_default_profile(bot_app.config, tool_registry)
            runtime_getter = self._runtime_getter()
            runtime = runtime_getter("plugin_ui")
            if runtime is None:
                await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.runtime_unavailable", lang), md2=True)
                return ToolResult.ok()
            ui = runtime.get_plugin_ui(profile)
            plugin_menu = ui.get("plugin_menu") or []
            entry = next((e for e in plugin_menu if e.get("plugin_id") == pid), None)
            if not entry:
                await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.plugin_unavailable", lang), md2=True)
                return ToolResult.ok()
            plugin = entry.get("plugin")
            actions = entry.get("actions") or []
            rows = []
            for act in actions:
                label = str(act.get("label", "") or "").strip() or t("agent.msg.plugin_action_default", lang)
                plugin_action = str(act.get("action", "") or "").strip()
                if plugin and hasattr(plugin, "action_button"):
                    btn = plugin.action_button(label, plugin_action)
                else:
                    # cb:{pid}:{action} is routed by the agent plugin callback handlers.
                    btn = InlineKeyboardButton(label, callback_data=f"cb:{pid}:{plugin_action}")
                rows.append([btn])
            rows.append(
                [
                    InlineKeyboardButton(
                        t("agent.btn.back_to_plugins", lang),
                        callback_data=build_mode_action_callback_data(
                            self.mode_id,
                            "plugins",
                            payload=self._plugin_session_payload(target_session),
                        ),
                    )
                ]
            )
            title = str(entry.get("label", pid) or pid)
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=f"{title}:",
                md2=True,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except Exception as e:
            self._log.exception("agent plugin view failed: %s", e)
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.plugin_load_failed", lang), md2=True)
        return ToolResult.ok()

    async def _cb_run_operation(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
        operation: str,
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        if not self._is_agent_active(session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.agent_not_active", lang), md2=True)
            return ToolResult.ok()
        service = getattr(bot_app, "mode_run_operations", None)
        if service is None or not getattr(service, "is_enabled", lambda: False)():
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.run_ops_unavailable", lang), md2=True)
            return ToolResult.ok()
        method = getattr(service, f"{operation}_run", None)
        if not callable(method):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.run_op_unsupported", lang, operation=operation), md2=True)
            return ToolResult.ok()
        try:
            result = await method(session=session, mode_id=self.mode_id)
            status = str(getattr(result, "status", "") or "").strip()
            message = str(getattr(result, "message", "") or "").strip()
            text = message or t("agent.msg.run_op_done", lang, operation=operation, status=status or t("agent.msg.run_op_executed", lang))
            await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        except Exception as e:
            self._log.exception("agent run operation %s failed: %s", operation, e)
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.run_op_failed", lang, operation=operation), md2=True)
        return ToolResult.ok()

    async def _cb_promote_skills(
        self,
        *,
        bot_app: Any,
        session: Any,
        chat_id: int,
        query: Any,
        ms: Any,
    ) -> ToolResult:
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        if not self._is_agent_active(session):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.agent_not_active", lang), md2=True)
            return ToolResult.ok()
        if not getattr(bot_app, "is_admin", lambda _: False)(chat_id):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.promote_admin_only", lang), md2=True)
            return ToolResult.ok()
        skill_runtime = self._optional_skill_runtime()
        if skill_runtime is None or not hasattr(skill_runtime, "promote_run_skills"):
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.skill_runtime_unavailable", lang), md2=True)
            return ToolResult.ok()
        try:
            artifact_store = self._artifact_store()
            latest = self._latest_mode_run(session)
            if latest is None or artifact_store is None:
                await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.no_runs_for_promote", lang), md2=True)
                return ToolResult.ok()
            result = skill_runtime.promote_run_skills(
                session=session,
                run_artifact_store=artifact_store,
                mode_id=self.mode_id,
                run_id=str(latest.run_id or ""),
                is_admin=True,
            )
            result_dict = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
            promoted = result_dict.get("promoted_skill_ids") or []
            if promoted:
                text = f"Promoted: {', '.join(str(s) for s in promoted)}"
            else:
                text = str(result_dict.get("message") or t("agent.msg.no_skills_for_promote", lang))
            await ms.send_or_edit(query=query, chat_id=chat_id, text=text, md2=True)
        except Exception as e:
            self._log.exception("agent promote_skills failed: %s", e)
            await ms.send_or_edit(query=query, chat_id=chat_id, text=t("agent.msg.promote_failed", lang), md2=True)
        return ToolResult.ok()

    async def handle_dirs_selection(self, *, flow: str, event: str, path: str, ctx: Dict[str, Any]) -> Optional[ToolResult]:
        if str(flow or "").strip() != "project":
            return None
        bot_app = ctx.get("bot_app")
        chat_id = int(ctx.get("chat_id"))
        context = ctx.get("context")
        session = ctx.get("session")
        if not bot_app:
            return ToolResult.fail("missing_context")
        lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        event_name = str(event or "").strip().lower()
        if event_name in {"cancelled", "canceled", "cancel"}:
            self._clear_pending_project_session(chat_id, context=context, session=session)
            return ToolResult.ok(t("agent.msg.project_pick_cancelled", lang))
        selected_path = str(path or "").strip()
        if not selected_path:
            self._clear_pending_project_session(chat_id, context=context, session=session)
            return ToolResult.ok(t("agent.msg.path_not_selected", lang))
        if not session:
            return ToolResult.ok(t("agent.msg.session_not_found", lang))
        pending = self._pop_pending_project_session(chat_id, context=context, session=session)
        if pending is None:
            return ToolResult.ok(t(_AGENT_PROJECT_SELECTION_STALE_KEY, lang))
        if not self._pending_entry_matches_session(pending, session):
            return ToolResult.ok(t(_AGENT_PROJECT_SELECTION_STALE_KEY, lang))
        if not self._is_agent_active(session):
            return ToolResult.ok(t("agent.msg.agent_inactive_for_session", lang))
        ok, msg = self._set_project_root(bot_app, session, chat_id, context, selected_path, lang=lang)
        return ToolResult.ok(msg if ok else t("agent.msg.project_connect_failed", lang))

    def build_menu(
        self,
        session: Any,
        back_callback: str = "sess_active",
        back_text: str = "⬅️ Назад",
        menu_visibility: Any = None,
    ) -> tuple[str, InlineKeyboardMarkup]:
        return build_agent_menu(
            session,
            back_callback=back_callback,
            back_text=back_text,
            mode_id=self.mode_id,
            menu_visibility=menu_visibility,
        )

    def _is_agent_active(self, session: Any) -> bool:
        return str(get_active_mode(session, "") or "").strip() == self.mode_id

    def _set_project_root(
        self,
        bot_app: Any,
        session: Any,
        chat_id: int,
        context: Any,
        project_root: Optional[str],
        lang: str = "ru",
    ) -> tuple[bool, str]:
        if project_root:
            if not bot_app.is_admin(chat_id):
                allowed = {os.path.realpath(p) for p in (bot_app.user_projects(chat_id) or [])}
                resolved = os.path.realpath(str(project_root))
                if resolved not in allowed:
                    return False, t("agent.msg.project_not_allowed", lang)
            root = bot_app.config.defaults.workdir
            if not is_within_root(project_root, root):
                return False, t("agent.msg.project_outside_root", lang)
            if not os.path.isdir(project_root):
                return False, t("agent.msg.project_dir_not_exists", lang)
            project_root = os.path.realpath(project_root)
        session.project_root = project_root
        runtime = self._agent_runtime()
        scoped_key = session_scoped_key(session)
        try:
            runtime.interrupt_session(session.id, chat_id, context)
        except Exception:
            self._log.exception("agent set_project_root interrupt failed")
        try:
            runtime.clear_session_cache(scoped_key or session.id)
        except Exception:
            self._log.exception("agent set_project_root clear_session_cache failed")
        self._persist_sessions(bot_app)
        if project_root:
            return True, t("agent.msg.project_connected", lang, project_root=project_root)
        return True, t("agent.msg.project_disconnected", lang)

    async def _rerender_menu(self, bot_app: Any, session: Any, chat_id: int, context: Any, query: Any, *, note: str = "") -> None:
        try:
            _lang = resolve_user_lang(getattr(bot_app, "config", None), chat_id=chat_id)
        except Exception:
            _lang = "ru"
        await self._rerender_menu_common(
            bot_app=bot_app,
            session=session,
            chat_id=chat_id,
            context=context,
            query=query,
            note=note,
            back_callback="sess_active",
            back_text=t("common.back", _lang),
        )

    @staticmethod
    def _project_label(project_path: str, *, max_len: int = 60) -> str:
        label = os.path.basename(str(project_path)) or str(project_path)
        if len(label) <= int(max_len):
            return label
        return label[: int(max_len) - 3] + "..."

    def _tool_registry(self) -> Any:
        tooling = self._tooling()
        return tooling.get_registry()

    def _pending_questions_list(self, *, session: Any, chat_id: Any = None) -> list[Dict[str, Any]]:
        dialogs = self._optional_dialogs()
        if dialogs is None:
            self._log.warning("agent pending questions backend unavailable")
            return []
        reader = getattr(dialogs, "pending_questions_list", None)
        if not callable(reader):
            self._log.warning("agent pending questions backend unsupported")
            return []
        try:
            return list(reader(session=session, chat_id=chat_id) or [])
        except Exception:
            self._log.exception("agent pending questions lookup failed")
            return []

    def _pending_questions_map(self, *, session: Any, chat_id: Any = None) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for item in self._pending_questions_list(session=session, chat_id=chat_id):
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            if not question_id:
                continue
            meta = dict(item)
            meta.pop("question_id", None)
            session_id = str(getattr(session, "id", "") or "").strip()
            if session_id:
                meta["session_id"] = session_id
            out[question_id] = meta
        return out

    def _pending_questions_summary(self, *, session: Any, chat_id: Any = None) -> Dict[str, Any]:
        dialogs = self._optional_dialogs()
        if dialogs is None:
            self._log.warning("agent pending questions backend unavailable")
            return {"count": 0, "awaiting_custom": False, "active_question_id": ""}
        reader = getattr(dialogs, "pending_questions_summary", None)
        if not callable(reader):
            self._log.warning("agent pending questions backend unsupported")
            return {"count": 0, "awaiting_custom": False, "active_question_id": ""}
        try:
            return dict(reader(session=session, chat_id=chat_id) or {})
        except Exception:
            self._log.exception("agent pending questions summary failed")
            return {"count": 0, "awaiting_custom": False, "active_question_id": ""}

    def _status_active_plugin_flow(self, *, session: Any, chat_id: Any) -> str:
        flows: list[str] = []
        project_chat_id = _optional_int(chat_id)
        if project_chat_id is not None:
            try:
                pending = self._get_pending_project_session(project_chat_id, session=session)
                if pending is not None and self._pending_entry_matches_session(pending, session):
                    flows.append("project_connect")
            except Exception:
                self._log.exception("agent status pending flow detect failed chat_id=%s", chat_id)

        try:
            scope = getattr(session, "conversation_scope", None)
            token = self._dirs_flow().active_token(
                chat_id,
                message_thread_id=getattr(scope, "message_thread_id", None),
            )
            mode_id, flow = decode_mode_dirs(token)
            if mode_id == self.mode_id and flow:
                flows.append(f"dirs:{flow}")
        except Exception:
            self._log.exception("agent status dirs flow detect failed chat_id=%s", chat_id)

        try:
            dialogs = self._optional_dialogs()
            if dialogs is not None and dialogs.is_active(chat_id=str(chat_id), session_id=str(session.id), mode_id=self.mode_id):
                flows.append("dialog")
        except Exception:
            self._log.exception("agent status dialog flow detect failed chat_id=%s", chat_id)

        dedup: list[str] = []
        for item in flows:
            val = str(item or "").strip()
            if not val or val in dedup:
                continue
            dedup.append(val)
        return ",".join(dedup)

    def _pending_project_ui_key(
        self,
        chat_id: int,
        *,
        query: Any = None,
        context: Any = None,
        session: Any = None,
    ) -> TelegramUiKey:
        if query is not None:
            ui_key = TelegramUiKey.from_query(query)
            if ui_key is not None:
                return ui_key
        context_thread_id = getattr(context, "message_thread_id", None)
        if context_thread_id is not None:
            return TelegramUiKey.from_parts(chat_id, context_thread_id)
        scope = getattr(session, "conversation_scope", None)
        if (
            scope is not None
            and getattr(scope, "message_thread_id", None) is not None
            and int(getattr(scope, "chat_id", 0) or 0) == int(chat_id)
        ):
            return TelegramUiKey.from_parts(chat_id, getattr(scope, "message_thread_id", None))
        return TelegramUiKey.from_parts(chat_id, None)

    def _pending_project_scope_token(
        self,
        chat_id: int,
        *,
        query: Any = None,
        context: Any = None,
        session: Any = None,
    ) -> str:
        ui_key = self._pending_project_ui_key(chat_id, query=query, context=context, session=session)
        return agent_project_scope_key(ui_key.chat_id, ui_key.message_thread_id)

    @staticmethod
    def _pending_project_legacy_key(chat_id: int) -> str:
        return str(int(chat_id))

    def _extract_project_callback_payload(self, payload: Dict[str, Any]) -> Dict[str, str]:
        normalized = {str(key): str(value or "").strip() for key, value in dict(payload or {}).items() if value is not None}
        compact = self._parse_compact_callback_payload(str(normalized.get("value") or ""))
        for key, value in compact.items():
            normalized.setdefault(str(key), str(value or "").strip())
        return normalized

    def _pending_entry_matches_session(self, entry: Dict[str, Any], session: Any) -> bool:
        current_session_key = agent_project_session_key(session)
        entry_session_key = str(entry.get("session_scoped_key") or "").strip()
        if entry_session_key and current_session_key:
            return entry_session_key == current_session_key
        return str(entry.get("session_id") or "").strip() == str(getattr(session, "id", "") or "").strip()

    def _collect_pending_project_state_keys(self) -> set[str]:
        keys = set(self._pending_project_by_scope.keys())
        pending = self._optional_agent_pending()
        store = getattr(pending, "store", None) if pending is not None else None
        if store is not None and hasattr(store, "keys"):
            try:
                keys.update(str(key) for key in store.keys())
            except Exception:
                self._log.exception("agent pending project key iteration failed")
        return {str(key) for key in keys if str(key).strip()}

    def _pending_project_raw(self, key: str) -> Any:
        scope_key = str(key or "").strip()
        if not scope_key:
            return None
        if scope_key in self._pending_project_by_scope:
            return self._pending_project_by_scope.get(scope_key)
        pending = self._optional_agent_pending()
        if pending is None:
            return None
        try:
            return pending.get(scope_key)
        except Exception:
            self._log.exception("agent pending project raw read failed key=%s", scope_key)
            return None

    def _remove_pending_project_key(self, key: str) -> None:
        scope_key = str(key or "").strip()
        if not scope_key:
            return
        self._pending_project_by_scope.pop(scope_key, None)
        pending = self._optional_agent_pending()
        if pending is None:
            return
        try:
            pending.pop(scope_key, None)
        except Exception:
            self._log.exception("agent pending project clear failed key=%s", scope_key)

    def _get_pending_project_session(
        self,
        chat_id: int,
        *,
        query: Any = None,
        context: Any = None,
        session: Any = None,
    ) -> Optional[Dict[str, Any]]:
        scope_key = self._pending_project_scope_token(chat_id, query=query, context=context, session=session)
        raw = self._pending_project_by_scope.get(scope_key)
        if raw is None:
            pending = self._optional_agent_pending()
            if pending is not None:
                try:
                    raw = pending.get(scope_key)
                except Exception:
                    self._log.exception("agent pending project read failed key=%s", scope_key)
                    raw = None
        entry = normalize_agent_project_pending_entry(raw)
        if entry is not None:
            return entry
        legacy_key = self._pending_project_legacy_key(chat_id)
        raw = self._pending_project_by_scope.get(legacy_key)
        if raw is None:
            pending = self._optional_agent_pending()
            if pending is not None:
                try:
                    raw = pending.get(legacy_key)
                except Exception:
                    self._log.exception("agent pending project legacy read failed chat_id=%s", chat_id)
                    raw = None
        entry = normalize_agent_project_pending_entry(raw)
        if entry is None:
            return None
        migrated_entry = dict(entry)
        migrated_entry["ui_chat_id"] = int(self._pending_project_ui_key(chat_id, query=query, context=context, session=session).chat_id)
        migrated_entry["message_thread_id"] = self._pending_project_ui_key(
            chat_id,
            query=query,
            context=context,
            session=session,
        ).message_thread_id
        self._pending_project_by_scope[scope_key] = migrated_entry
        pending = self._optional_agent_pending()
        if pending is not None:
            try:
                pending.set(scope_key, dict(migrated_entry))
                pending.pop(legacy_key, None)
            except Exception:
                self._log.exception("agent pending project legacy migrate failed chat_id=%s key=%s", chat_id, scope_key)
        self._pending_project_by_scope.pop(legacy_key, None)
        return migrated_entry

    def _set_pending_project_session(
        self,
        chat_id: int,
        *,
        session: Any,
        query: Any = None,
        context: Any = None,
    ) -> None:
        ui_key = self._pending_project_ui_key(chat_id, query=query, context=context, session=session)
        scope_key = agent_project_scope_key(ui_key.chat_id, ui_key.message_thread_id)
        entry = {
            "session_id": str(getattr(session, "id", "") or "").strip(),
            "session_scoped_key": agent_project_session_key(session),
            "ui_chat_id": int(ui_key.chat_id),
            "message_thread_id": ui_key.message_thread_id,
        }
        self._pending_project_by_scope[scope_key] = dict(entry)
        pending = self._optional_agent_pending()
        if pending is None:
            return
        try:
            pending.set(scope_key, dict(entry))
            pending.pop(self._pending_project_legacy_key(chat_id), None)
        except Exception:
            self._log.exception("agent pending project set failed scope=%s", scope_key)

    def _clear_pending_project_session(
        self,
        chat_id: int,
        *,
        query: Any = None,
        context: Any = None,
        session: Any = None,
        clear_all_for_session: bool = False,
    ) -> None:
        keys_to_remove = {
            self._pending_project_scope_token(chat_id, query=query, context=context, session=session),
            self._pending_project_legacy_key(chat_id),
        }
        if clear_all_for_session and session is not None:
            for key in self._collect_pending_project_state_keys():
                entry = normalize_agent_project_pending_entry(self._pending_project_raw(key))
                if entry is not None and self._pending_entry_matches_session(entry, session):
                    keys_to_remove.add(key)
        for key in keys_to_remove:
            self._remove_pending_project_key(key)

    def _pop_pending_project_session(
        self,
        chat_id: int,
        *,
        query: Any = None,
        context: Any = None,
        session: Any = None,
    ) -> Optional[Dict[str, Any]]:
        entry = self._get_pending_project_session(chat_id, query=query, context=context, session=session)
        self._clear_pending_project_session(chat_id, query=query, context=context, session=session)
        return entry

    async def _validate_project_selection_callback(
        self,
        *,
        session: Any,
        chat_id: int,
        query: Any,
        context: Any,
        ms: Any,
        payload: Dict[str, Any],
        lang: str = "ru",
    ) -> bool:
        payload_data = self._extract_project_callback_payload(payload)
        expected_session_key = str(payload_data.get("sk") or payload_data.get("session_scoped_key") or "").strip()
        current_session_key = agent_project_session_key(session)
        if not expected_session_key or not current_session_key or expected_session_key != current_session_key:
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t(_AGENT_PROJECT_SELECTION_STALE_KEY, lang),
                md2=True,
            )
            return False
        if not self._is_agent_active(session):
            self._clear_pending_project_session(
                chat_id,
                query=query,
                context=context,
                session=session,
                clear_all_for_session=True,
            )
            await ms.send_or_edit(
                query=query,
                chat_id=chat_id,
                text=t("agent.msg.agent_inactive_reopen", lang),
                md2=True,
            )
            return False
        return True

    def _mode_root(self) -> str:
        return os.path.dirname(__file__)

    def _artifact_store(self) -> Optional[RunArtifactStore]:
        lifecycle = self._optional_mode_run_lifecycle()
        if lifecycle is not None:
            return lifecycle.artifact_store
        config = getattr(self, "config", None)
        if config is None:
            return None
        cached = getattr(self, "_cached_artifact_store", None)
        if cached is not None and getattr(cached, "config", None) is config:
            return cached
        store = RunArtifactStore(config)
        self._cached_artifact_store = store
        return store

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
        lifecycle = self._optional_mode_run_lifecycle()
        if artifact_store is None or lifecycle is None:
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
                lifecycle.mark_finished(
                    latest,
                    status="failed",
                    phase=str(latest_state.get("phase") or "execute"),
                    validate_boundary=False,
                )
                resume_guard["previous_run_repaired"] = True

        started = lifecycle.start(
            session=session,
            mode_id=self.mode_id,
            phase="plan",
            source_prompt_hash=self._prompt_hash(user_text),
            mode_context={
                "dest_kind": str((dest or {}).get("kind") or "telegram"),
                "run_scope": "mode_pipeline",
                "required_use_cli_steps": [],
                "cli_work_type": self._cli_work_type(session),
                "executor_profile": self._executor_profile(session),
                "blocking_clarification_open": False,
                "blocking_clarifications": {},
                "resume_guard": dict(resume_guard or {}),
            },
            validate_boundary=False,
        )
        run = started.handle
        self._set_active_run_handle(session, run)
        try:
            setattr(session, _AGENT_RUN_RESUME_GUARD_SESSION_ATTR, dict(resume_guard or {}))
        except Exception:
            self._log.exception("agent run artifacts: failed to set resume guard session attr")
        return run, resume_guard

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
            self._log.exception("agent run artifacts: doctor resume diagnosis failed run_id=%s", run.run_id)
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
        # agent всегда выполняет shallow-merge через lifecycle, поэтому параметр игнорируется.
        if run is None:
            return
        artifact_store = self._artifact_store()
        lifecycle = self._optional_mode_run_lifecycle()
        if artifact_store is None or lifecycle is None:
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
            payload = dict(current)
            payload["phase"] = str(phase or payload.get("phase") or "plan")
            payload["status"] = str(status or payload.get("status") or "running")
            payload["mode_context"] = merged_mode_context
            lifecycle.save_phase(
                run,
                phase=str(payload["phase"]),
                state=payload,
                validate_boundary=False,
            )
        except Exception:
            self._log.exception("agent run artifacts: save_state failed phase=%s run_id=%s", phase, run.run_id)

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
        raise RuntimeError(f"Agent run boundary validation failed phase={phase}: {issues}")

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
                    "unit_id": "agent:orchestrator",
                    "status": "ok",
                    "message": str(output or "")[:500],
                    "synthetic": True,
                },
            )
        except Exception:
            self._log.exception("agent run artifacts: failed to ensure execute boundary evidence run_id=%s", run.run_id)

    def _mark_run_finished(self, run: Optional[RunArtifactHandle], *, status: str, phase: str) -> None:
        if run is None:
            return
        lifecycle = self._optional_mode_run_lifecycle()
        if lifecycle is None:
            return
        try:
            lifecycle.mark_finished(run, status=status, phase=phase, validate_boundary=False)
        except Exception:
            self._log.exception("agent run artifacts: mark_finished failed run_id=%s", run.run_id)

    def _clear_active_run_handle(self, session: Any) -> None:
        super()._clear_active_run_handle(session)  # type: ignore[misc]
        if hasattr(session, _AGENT_RUN_RESUME_GUARD_SESSION_ATTR):
            setattr(session, _AGENT_RUN_RESUME_GUARD_SESSION_ATTR, {})

    def _execution_context(self, *, session: Any, dest: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "dest_kind": str((dest or {}).get("kind") or "telegram"),
            "chat_id": (dest or {}).get("chat_id"),
            "project_root": str(getattr(session, "project_root", "") or "").strip() or None,
            "workdir": str(getattr(session, "workdir", "") or "").strip() or None,
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
            self._log.exception("agent run artifacts: latest top-level run lookup failed")
        return None

    @staticmethod
    def _is_top_level_mode_run(state: Dict[str, Any]) -> bool:
        mode_context = state.get("mode_context") if isinstance(state, dict) else {}
        if not isinstance(mode_context, dict):
            return False
        return str(mode_context.get("run_scope") or "").strip() == "mode_pipeline"

    def _blocking_clarification_context(self, *, bot_app: Any, session: Any) -> Dict[str, Any]:
        _ = bot_app
        payload = self._pending_questions_summary(session=session)
        return {
            "count": int(payload.get("count") or 0),
            "awaiting_custom": bool(payload.get("awaiting_custom")),
            "active_question_id": str(payload.get("active_question_id") or "").strip(),
        }

    @staticmethod
    def _cli_work_type(session: Any) -> Optional[str]:
        raw = getattr(getattr(session, "cli", None), "cli_work_type", getattr(session, "cli_work_type", ""))
        token = str(raw or "").strip()
        return token or None

    @staticmethod
    def _executor_profile(session: Any) -> str:
        return str(getattr(session, "executor_profile", "") or "").strip() or "default"

    def _load_prompts(self) -> Dict[str, str]:
        if self._prompts:
            return self._prompts
        path = os.path.join(self._mode_root(), "prompts.yaml")
        raw: Dict[str, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            self._log.exception("agent prompts read failed: %s", path)
            raw = {}
        prompts = raw.get("prompts") if isinstance(raw, dict) else {}
        if not isinstance(prompts, dict):
            prompts = {}
        self._prompts = {str(k): str(v) for k, v in prompts.items()}
        return self._prompts
