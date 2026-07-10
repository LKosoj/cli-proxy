"""
Module containing session management functionality for the Telegram bot.
"""

import asyncio
import concurrent.futures
import functools
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from app.services.cli_dialog_logger import log_cli_dialog
from app.services.logging_service import bind_session_log_context
from app.services.session_interrupt_service import SessionInterruptReport, SessionInterruptService
from app.services.task_bearing_cli_hook_service import get_task_bearing_cli_hook_service
from app.services.session_run_service import ModeScopedPreRunResetService
from i18n import t
from telegram.ext import ContextTypes
from utils.lang import resolve_user_lang

from session import Session, session_runtime_uid, switch_session_active_cli_if_needed
from summary import summarize_text_with_reason
from utils.html_renderer import ansi_to_html, make_html_file
from sessions.session_run_service import SessionRunService
from sessions.session_output_service import SessionOutputService

_SESSION_TASK_MODE_ID = "__session__"


@dataclass
class PendingInput:
    session_id: str
    text: str
    dest: dict
    image_path: Optional[str] = None
    image_paths: Optional[list[str]] = None


class SessionManagement:
    """
    Class containing session management functionality for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app
        # HTML rendering of large ANSI logs is CPU-heavy and often pure-Python.
        # Running it in a thread can starve the event loop due to the GIL, which looks like "polling freeze".
        # For large outputs we offload conversion to a separate process.
        self._html_process_threshold_chars = 100_000
        self._html_process_pool = None  # Will be initialized in main bot app
        self._html_render_tail_chars = 70_000
        self._summary_prepare_threshold_chars = 50_000
        self._summary_tail_chars = 70_000
        self._summary_wait_for_html_s = 5.0
        self._summary_timeout_s = 100.0
        self._output_service = SessionOutputService(
            bot_app=self.bot_app,
            persist_sessions=self._persist_sessions,
            persist_session=self._persist_single_session,
            html_process_pool=self._html_process_pool,
            html_process_threshold_chars=self._html_process_threshold_chars,
            html_render_tail_chars=self._html_render_tail_chars,
            summary_prepare_threshold_chars=self._summary_prepare_threshold_chars,
            summary_tail_chars=self._summary_tail_chars,
            summary_wait_for_html_s=self._summary_wait_for_html_s,
            summary_timeout_s=self._summary_timeout_s,
            summarize_fn=summarize_text_with_reason,
            summarize_fn_getter=lambda: summarize_text_with_reason,
            ansi_to_html_fn=functools.partial(ansi_to_html, allow_network_fetch=True),
            ansi_to_html_fn_getter=lambda: functools.partial(ansi_to_html, allow_network_fetch=True),
            make_html_file_fn=make_html_file,
            make_html_file_fn_getter=lambda: make_html_file,
        )
        self._run_service = SessionRunService(
            bot_app=self.bot_app,
            persist_sessions=self._persist_sessions,
            persist_session=self._persist_single_session,
            mode_tasks_list=self._mode_tasks_list,
            mode_tasks_create=self._mode_tasks_create,
            mode_tasks_track=self._mode_tasks_track,
            log_cli_dialog=self._log_cli_dialog,
            reset_session_fields_like_sessions_reset=self._reset_session_fields_like_sessions_reset,
        )
        self._mode_pre_run_reset = ModeScopedPreRunResetService(logger=logging.getLogger(__name__))
        self._interrupt_service = SessionInterruptService(
            cancel_session_tasks=self._cancel_mode_tasks_session_counted,
            list_session_tasks=self._list_session_task_names,
            clear_message_buffer=self._clear_message_buffer_for_session,
            clear_media_groups=self._clear_media_groups_for_session,
            clear_pending_questions=self._clear_pending_questions_for_interrupt,
            cancel_notification_scope=self._cancel_notification_scope,
            persist_session=self._persist_single_session,
            logger_=logging.getLogger(__name__),
        )

    def _persist_sessions(self) -> None:
        try:
            self.bot_app.mode_session_control.persist()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")

    def _persist_single_session(self, chat_id: int, session_id: str) -> bool:
        manager = getattr(self.bot_app, "manager", None)
        if manager is None or not hasattr(manager, "persist_session"):
            self._persist_sessions()
            return False
        try:
            return bool(manager.persist_session(int(chat_id), str(session_id)))
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            self._persist_sessions()
            return False

    def set_html_process_pool(self, pool: concurrent.futures.Executor | None) -> None:
        self._html_process_pool = pool
        self._output_service._html_process_pool = pool

    async def _cancel_mode_tasks_session(self, session_id: str) -> None:
        try:
            await self.bot_app.mode_session_control.cancel_session(session_id=session_id, timeout_s=0.2)
        except Exception as e:
            logging.exception("cancel mode tasks session=%s failed: %s", session_id, e)

    async def _cancel_mode_tasks_session_counted(self, session_id: str, timeout_s: float) -> int:
        try:
            return int(await self.bot_app.mode_session_control.cancel_session(session_id=session_id, timeout_s=timeout_s))
        except Exception as e:
            logging.exception("cancel mode tasks session=%s failed: %s", session_id, e)
            return 0

    def _mode_tasks_list(self, *, session_id: str, mode_id: str) -> list[str]:
        tasks = getattr(self.bot_app, "mode_tasks", None)
        if tasks is None:
            return []
        return list(tasks.list(session_uid=session_id, mode_id=mode_id) or [])

    def _mode_tasks_create(self, *, session_id: str, mode_id: str, coro, name: str) -> None:
        tasks = getattr(self.bot_app, "mode_tasks", None)
        if tasks is None:
            return
        tasks.create(session_uid=session_id, mode_id=mode_id, coro=coro, name=name)

    def _mode_tasks_track(self, *, session_id: str, mode_id: str, name: str) -> None:
        tasks = getattr(self.bot_app, "mode_tasks", None)
        if tasks is None or not hasattr(tasks, "track"):
            return
        tasks.track(session_uid=session_id, mode_id=mode_id, name=name)

    def _list_session_task_names(self, session_uid: str) -> list[str]:
        tasks = getattr(self.bot_app, "mode_tasks", None)
        if tasks is None:
            return []
        try:
            if hasattr(tasks, "list_session"):
                return list(tasks.list_session(session_uid=str(session_uid)) or [])
        except Exception:
            logging.exception("list session tasks failed session_uid=%s", session_uid)
        return []

    def _clear_message_buffer_for_session(self, session: Session, reply_chat_id: int) -> bool:
        message_buffer_service = getattr(self.bot_app, "message_buffer_service", None)
        if message_buffer_service is None:
            return False
        return bool(message_buffer_service.clear_buffer(session, int(reply_chat_id)))

    def _clear_media_groups_for_session(self, session: Session) -> None:
        clear_media_groups = getattr(self.bot_app, "_clear_media_groups_for_session", None)
        if callable(clear_media_groups):
            clear_media_groups(session)

    def _clear_pending_questions_for_interrupt(
        self,
        session: Session,
        reply_chat_id: Optional[int],
        message_thread_id: Optional[int],
    ) -> int:
        clear_pending = getattr(self.bot_app, "_clear_pending_questions", None)
        if not callable(clear_pending):
            return 0
        return int(
            clear_pending(
                session_id=session.id,
                chat_id=reply_chat_id,
                message_thread_id=message_thread_id,
            )
        )

    async def _cancel_notification_scope(self, session_uid: str) -> int:
        service = getattr(self.bot_app, "notification_queue_service", None)
        if service is None or not hasattr(service, "cancel_scope"):
            return 0
        return int(await service.cancel_scope(str(session_uid or "")))

    def _log_cli_dialog(
        self,
        session: Session,
        prompt: str,
        output: str,
        chat_id: Optional[int] = None,
        image_path: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
    ) -> None:
        log_cli_dialog(
            session,
            prompt,
            output,
            chat_id=chat_id,
            image_path=image_path,
            image_paths=image_paths,
        )

    def _reset_session_fields_like_sessions_reset(self, session: Session, *, preserve_mode_id: Optional[str]) -> None:
        """
        Runs mode-scoped pre-run reset.

        The reset is intentionally narrow: for analyst mode it clears only analyst-specific
        transient state, stale pending questions and runtime cache.
        """
        self._mode_pre_run_reset.apply(
            session=session,
            mode_id=str(preserve_mode_id or "").strip() or None,
            clear_runtime_cache=self._clear_agent_session_cache,
            clear_pending_questions=self._clear_pending_questions_for_session,
        )

    def _clear_pending_questions_for_session(self, session_id: str) -> int:
        clear_fn = getattr(self.bot_app, "_clear_pending_questions", None)
        if not callable(clear_fn):
            return 0
        return int(clear_fn(session_id=str(session_id)))

    async def send_output(
        self,
        session: Session,
        dest: dict,
        output: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        send_header: bool = True,
        header_override: Optional[str] = None,
        force_html: bool = False,
        send_summary: bool = True,
    ) -> None:
        await self._output_service.send_output(
            session=session,
            dest=dest,
            output=output,
            context=context,
            send_header=send_header,
            header_override=header_override,
            force_html=force_html,
            send_summary=send_summary,
        )

    async def run_prompt(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._run_service.run_prompt(session, prompt, dest, context)

    def start_prompt_task(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        task_name: str = "run_prompt",
    ) -> bool:
        return bool(
            self._run_service.start_prompt_task(
                session,
                prompt,
                dest,
                context,
                task_name=task_name,
            )
        )

    async def recover_tmux_sessions(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        return await self._run_service.recover_tmux_sessions(context)

    async def run_mode_pipeline(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        mode_id: str,
    ) -> None:
        await self._run_service.run_mode_pipeline(session, prompt, dest, context, mode_id=mode_id)

    def _resolve_raw_prompt_session(
        self,
        session_id: Optional[str],
        chat_id: Optional[int] = None,
    ) -> Tuple[Session, Optional[int]]:
        manager = getattr(self.bot_app, "manager", None)
        if manager is None:
            raise RuntimeError("session_manager_not_initialized")

        all_sessions: list[Tuple[int, Session]] = []
        for cid, by_id in dict(getattr(manager, "sessions_by_chat", {}) or {}).items():
            if not isinstance(by_id, dict):
                continue
            for sess in by_id.values():
                if isinstance(sess, Session):
                    all_sessions.append((int(cid), sess))

        expected_chat_id = int(chat_id) if chat_id is not None else None
        sid = str(session_id or "").strip()
        if sid:
            matches = [(cid, sess) for cid, sess in all_sessions if str(getattr(sess, "id", "") or "") == sid]
            if expected_chat_id is not None:
                matches = [(cid, sess) for cid, sess in matches if int(cid) == expected_chat_id]
            if not matches:
                raise RuntimeError(f"session_not_found:{sid}")
            if len(matches) > 1:
                raise RuntimeError(f"ambiguous_session_id:{sid}")
            return matches[0]

        candidate_sessions: list[Tuple[int, Session]] = []
        seen: set[tuple[int, str]] = set()
        sessions_by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
        for candidate_chat_id in sorted(sessions_by_chat.keys()):
            if expected_chat_id is not None and int(candidate_chat_id) != expected_chat_id:
                continue
            by_id = sessions_by_chat.get(candidate_chat_id)
            if not isinstance(by_id, dict):
                continue
            sessions_for_chat = [sess for sess in by_id.values() if isinstance(sess, Session)]
            if len(sessions_for_chat) != 1:
                continue
            session = sessions_for_chat[0]
            key = (int(candidate_chat_id), str(getattr(session, "id", "") or ""))
            if key in seen:
                continue
            seen.add(key)
            candidate_sessions.append((int(candidate_chat_id), session))
        if not candidate_sessions:
            raise RuntimeError("no_single_session")
        if len(candidate_sessions) > 1:
            raise RuntimeError("multiple_sessions_require_session_id")
        return candidate_sessions[0]

    async def run_prompt_raw(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        *,
        chat_id: Optional[int] = None,
        source: str = "raw_prompt",
        task_bearing: bool = True,
        technical_command: Optional[bool] = None,
    ) -> str:
        text = str(prompt or "")
        if not text.strip():
            raise RuntimeError("empty_prompt")
        chat_id, session = self._resolve_raw_prompt_session(session_id, chat_id=chat_id)
        prev_busy = bool(getattr(session, "busy", False))
        self._mode_tasks_track(
            session_id=session_runtime_uid(session),
            mode_id=_SESSION_TASK_MODE_ID,
            name="run_prompt_raw",
        )
        with bind_session_log_context(session=session, chat_id=chat_id):
            async with session.run_lock:
                session.busy = True
                hook = None
                prepared = None
                prompt_for_cli = text
                hook_config = getattr(session, "config", None) or getattr(self.bot_app, "config", None)
                try:
                    cli_switch = await switch_session_active_cli_if_needed(session)
                    if cli_switch.switched:
                        self._persist_single_session(chat_id, session.id)
                    if hook_config is not None:
                        hook = get_task_bearing_cli_hook_service(hook_config)
                        prepared = await hook.prepare_prompt(
                            session=session,
                            prompt=text,
                            source=str(source or "raw_prompt"),
                            phase="execute",
                            task_bearing=task_bearing,
                            technical_command=technical_command,
                        )
                        prompt_for_cli = prepared.prompt_for_cli
                    output = await session.run_prompt(prompt_for_cli)
                    if hook is not None and prepared is not None:
                        hook.record_success(prepared, output=output)
                    self._log_cli_dialog(
                        session,
                        text,
                        str(output or ""),
                        chat_id=chat_id,
                    )
                    return str(output or "")
                except Exception as exc:
                    if hook is not None and prepared is not None:
                        hook.record_error(prepared, error=exc)
                    logging.exception("run_prompt_raw failed session=%s", getattr(session, "id", "?"))
                    raise
                finally:
                    session.busy = prev_busy

    async def _dispatch_queued_input(
        self,
        session: Session,
        next_prompt: str,
        next_dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        fallback_dest: dict,
    ) -> None:
        await self._run_service.dispatch_queued_input(
            session=session,
            next_prompt=next_prompt,
            next_dest=next_dest,
            context=context,
            fallback_dest=fallback_dest,
        )

    def _clear_agent_session_cache(self, session_id: str) -> None:
        for runtime in (getattr(self.bot_app, "iter_mode_runtimes", lambda: [])() or []):
            try:
                if runtime and hasattr(runtime, "clear_session_cache"):
                    runtime.clear_session_cache(session_id)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")

    @staticmethod
    def format_interrupt_user_message(report: SessionInterruptReport, lang: str = "ru") -> str:
        if str(report.status or "") == "completed":
            return t("msg.session.interrupt_work_done", lang)
        if str(report.status or "") == "partial_timeout":
            return t("msg.session.interrupt_partial_runtime", lang)
        return t("msg.session.interrupt_failed_diag", lang)

    async def interrupt_session_runtime(
        self,
        session: Session,
        *,
        owner_chat_id: Optional[int],
        reply_chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        reason: str = "interrupt",
        clear_pending_questions: bool = True,
    ) -> SessionInterruptReport:
        return await self._interrupt_service.interrupt_session_runtime(
            session,
            owner_chat_id=owner_chat_id,
            reply_chat_id=reply_chat_id,
            message_thread_id=message_thread_id,
            reason=reason,
            clear_pending_questions=clear_pending_questions,
        )

    def interrupt_session_now(
        self,
        session: Session,
        *,
        owner_chat_id: int,
        reply_chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        clear_pending_questions: bool = True,
    ) -> None:
        try:
            session.interrupt()
        except Exception:
            logging.exception("interrupt_session_now: session.interrupt failed for session=%s", session.id)
        queue = getattr(session, "queue", None)
        if queue:
            try:
                queue.clear()
            except Exception:
                logging.exception("interrupt_session_now: session queue clear failed for session=%s", session.id)
        reply_chat_id, message_thread_id = SessionInterruptService.resolve_interrupt_scope(
            session,
            reply_chat_id=reply_chat_id,
            message_thread_id=message_thread_id,
        )
        try:
            self._clear_message_buffer_for_session(session, int(reply_chat_id)) if reply_chat_id is not None else None
        except Exception:
            logging.exception("interrupt_session_now: message buffer clear failed for session=%s", session.id)
        try:
            self._clear_media_groups_for_session(session)
        except Exception:
            logging.exception("interrupt_session_now: media group clear failed for session=%s", session.id)
        if clear_pending_questions:
            try:
                self._clear_pending_questions_for_interrupt(session, reply_chat_id, message_thread_id)
            except Exception:
                logging.exception("interrupt_session_now: clear_pending_questions failed for session=%s", session.id)

    def _interrupt_before_close(self, session_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.bot_app.manager.get(chat_id, session_id)
        if not session:
            return
        coro = self.interrupt_session_runtime(
            session,
            owner_chat_id=int(chat_id),
            reason="before_close",
        )
        try:
            # create_task requires a running loop. In tests / sync call sites we may not have one.
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except Exception as e:
            try:
                coro.close()
            except Exception:
                logging.exception(
                    "_interrupt_before_close: failed to close cancellation coroutine for session=%s",
                    session_id,
                )
            logging.exception("_interrupt_before_close: failed to schedule mode task cancellation: %s", e)

    async def ensure_scope_session(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        reply_chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
    ) -> Optional[Session]:
        resolver = getattr(self.bot_app, "resolve_telegram_scope_session", None)
        session = None
        if callable(resolver):
            session = resolver(
                reply_chat_id=int(reply_chat_id if reply_chat_id is not None else chat_id),
                message_thread_id=message_thread_id,
                owner_chat_id=int(chat_id),
            )
        if not session:
            lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
            if not self.bot_app.is_admin(chat_id):
                projects = self.bot_app.user_projects(chat_id)
                if not projects:
                    await self.bot_app._send_message(
                        context,
                        chat_id=chat_id,
                        text=t("msg.session.access_not_configured", lang),
                    )
                    return None
                sessions = [
                    item
                    for item in self.bot_app.manager.sessions_for_chat(chat_id).values()
                    if self.bot_app.is_session_allowed_for_chat(chat_id, item)
                ]
                text = t("msg.session.use_existing_topics", lang) if sessions else t("msg.session.create_session_first", lang)
                await self.bot_app._send_message(
                    context,
                    chat_id=chat_id,
                    text=text,
                )
                return None
            sessions = self.bot_app.manager.sessions_for_chat(chat_id)
            text = t("msg.session.use_existing_topics", lang) if sessions else t("msg.session.create_session_first", lang)
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text=text,
            )
            return None
        return session

    def start_mode_task(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        mode_id: Optional[str] = None,
    ) -> None:
        self._run_service.start_mode_task(
            session=session,
            prompt=prompt,
            dest=dest,
            context=context,
            mode_id=mode_id,
        )
