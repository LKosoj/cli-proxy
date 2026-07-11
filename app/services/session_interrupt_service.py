from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.services.runtime_progress_service import clear_runtime_progress
from app.services.session_tick_history_store import clear_session_ticks
from session import session_runtime_uid
from sessions.conversation_scope import ConversationScope


ListSessionTasksFn = Callable[[str], list[str]]
CancelSessionTasksFn = Callable[[str, float], Awaitable[int]]
ClearBufferFn = Callable[[Any, int], bool]
ClearMediaGroupsFn = Callable[[Any], None]
ClearPendingQuestionsFn = Callable[[Any, Optional[int], Optional[int]], int]
CancelNotificationScopeFn = Callable[[str], Awaitable[int]]
PersistSessionFn = Callable[[int, str], None]


@dataclass(frozen=True)
class SessionInterruptReport:
    session_uid: str
    status: str
    cancelled_task_count: int = 0
    remaining_task_names: tuple[str, ...] = ()
    backend_state: str = "stopped"
    cleared_queue: bool = False
    cleared_message_buffer: bool = False
    cleared_media_groups: bool = False
    cleared_pending_questions: int = 0
    cleared_notifications: int = 0
    cleared_busy_signals: bool = False
    warnings: tuple[str, ...] = ()


class SessionInterruptService:
    def __init__(
        self,
        *,
        cancel_session_tasks: CancelSessionTasksFn,
        list_session_tasks: Optional[ListSessionTasksFn] = None,
        clear_message_buffer: Optional[ClearBufferFn] = None,
        clear_media_groups: Optional[ClearMediaGroupsFn] = None,
        clear_pending_questions: Optional[ClearPendingQuestionsFn] = None,
        cancel_notification_scope: Optional[CancelNotificationScopeFn] = None,
        persist_session: Optional[PersistSessionFn] = None,
        task_cancel_timeout_s: float = 1.5,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._cancel_session_tasks = cancel_session_tasks
        self._list_session_tasks = list_session_tasks
        self._clear_message_buffer = clear_message_buffer
        self._clear_media_groups = clear_media_groups
        self._clear_pending_questions = clear_pending_questions
        self._cancel_notification_scope = cancel_notification_scope
        self._persist_session = persist_session
        self._task_cancel_timeout_s = max(0.1, float(task_cancel_timeout_s))
        self._log = logger_ or logging.getLogger(__name__)

    @staticmethod
    def resolve_interrupt_scope(
        session: Any,
        *,
        reply_chat_id: Optional[int],
        message_thread_id: Optional[int],
    ) -> tuple[Optional[int], Optional[int]]:
        resolved_chat_id = int(reply_chat_id) if reply_chat_id is not None else None
        resolved_thread_id = int(message_thread_id) if message_thread_id is not None else None
        scope = getattr(session, "conversation_scope", None)
        if isinstance(scope, ConversationScope):
            if resolved_chat_id is None:
                resolved_chat_id = int(scope.chat_id)
            if resolved_thread_id is None and scope.message_thread_id is not None:
                resolved_thread_id = int(scope.message_thread_id)
        return resolved_chat_id, resolved_thread_id

    async def interrupt_session_runtime(
        self,
        session: Any,
        *,
        owner_chat_id: Optional[int],
        reply_chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        reason: str = "interrupt",
        clear_pending_questions: bool = True,
    ) -> SessionInterruptReport:
        warnings: list[str] = []
        session_uid = session_runtime_uid(session)
        resolved_reply_chat_id, resolved_thread_id = self.resolve_interrupt_scope(
            session,
            reply_chat_id=reply_chat_id,
            message_thread_id=message_thread_id,
        )
        self._log.info(
            "session interrupt: start session_uid=%s reason=%s reply_chat_id=%s thread_id=%s",
            session_uid,
            str(reason or "interrupt"),
            resolved_reply_chat_id,
            resolved_thread_id,
        )

        cleared_queue = False
        queue = getattr(session, "queue", None)
        if queue:
            try:
                queue.clear()
                cleared_queue = True
            except Exception:
                warnings.append("queue_clear_failed")
                self._log.exception("session interrupt: queue clear failed session_uid=%s", session_uid)

        cleared_message_buffer = False
        if self._clear_message_buffer is not None and resolved_reply_chat_id is not None:
            try:
                cleared_message_buffer = bool(self._clear_message_buffer(session, int(resolved_reply_chat_id)))
            except Exception:
                warnings.append("message_buffer_clear_failed")
                self._log.exception("session interrupt: message buffer clear failed session_uid=%s", session_uid)

        cleared_media_groups = False
        if self._clear_media_groups is not None:
            try:
                self._clear_media_groups(session)
                cleared_media_groups = True
            except Exception:
                warnings.append("media_groups_clear_failed")
                self._log.exception("session interrupt: media group clear failed session_uid=%s", session_uid)

        cleared_pending_questions_count = 0
        if clear_pending_questions and self._clear_pending_questions is not None:
            try:
                cleared_pending_questions_count = int(
                    self._clear_pending_questions(session, resolved_reply_chat_id, resolved_thread_id)
                )
            except Exception:
                warnings.append("pending_questions_clear_failed")
                self._log.exception("session interrupt: pending questions clear failed session_uid=%s", session_uid)

        try:
            session.interrupt()
        except Exception:
            warnings.append("session_interrupt_failed")
            self._log.exception("session interrupt: backend interrupt failed session_uid=%s", session_uid)

        cancelled_task_count = 0
        try:
            cancelled_task_count = int(
                await self._cancel_session_tasks(session_uid, float(self._task_cancel_timeout_s))
            )
        except Exception:
            warnings.append("task_cancel_failed")
            self._log.exception("session interrupt: cancel_session failed session_uid=%s", session_uid)
        await asyncio.sleep(0)

        cleared_notifications = 0
        if self._cancel_notification_scope is not None and session_uid:
            try:
                cleared_notifications = int(await self._cancel_notification_scope(session_uid))
            except Exception:
                warnings.append("notification_scope_cancel_failed")
                self._log.exception("session interrupt: notification scope cancel failed session_uid=%s", session_uid)
        await asyncio.sleep(0)

        self._clear_runtime_markers(session)

        effective_chat_id = owner_chat_id if owner_chat_id is not None else getattr(session, "chat_id", None)
        # Персистим состояние при любом прерывании (не только когда очистили
        # очередь): прогон мог только что зафиксировать resume_token, и его нужно
        # сбросить на диск, чтобы прерванная сессия пережила рестарт процесса.
        if self._persist_session is not None and effective_chat_id is not None:
            try:
                self._persist_session(int(effective_chat_id), str(getattr(session, "id", "") or ""))
            except Exception:
                warnings.append("persist_session_failed")
                self._log.exception("session interrupt: persist session failed session_uid=%s", session_uid)

        remaining_task_names = tuple(self._collect_remaining_task_names(session_uid))
        backend_state = self._backend_state(session)
        cleared_busy_signals = self._busy_signals_cleared(session, remaining_task_names=remaining_task_names)

        status = "completed"
        if remaining_task_names or backend_state not in {"stopped", "idle", "tmux_idle"} or not cleared_busy_signals:
            status = "partial_timeout"
        if "session_interrupt_failed" in warnings and status == "partial_timeout":
            status = "failed"

        report = SessionInterruptReport(
            session_uid=session_uid,
            status=status,
            cancelled_task_count=cancelled_task_count,
            remaining_task_names=remaining_task_names,
            backend_state=backend_state,
            cleared_queue=cleared_queue,
            cleared_message_buffer=cleared_message_buffer,
            cleared_media_groups=cleared_media_groups,
            cleared_pending_questions=cleared_pending_questions_count,
            cleared_notifications=cleared_notifications,
            cleared_busy_signals=cleared_busy_signals,
            warnings=tuple(warnings),
        )
        self._log.info(
            "session interrupt: finish session_uid=%s status=%s cancelled_task_count=%s remaining_tasks=%s backend_state=%s warnings=%s",
            session_uid,
            report.status,
            report.cancelled_task_count,
            list(report.remaining_task_names),
            report.backend_state,
            list(report.warnings),
        )
        return report

    def _collect_remaining_task_names(self, session_uid: str) -> list[str]:
        if not session_uid or self._list_session_tasks is None:
            return []
        try:
            return [str(item or "") for item in list(self._list_session_tasks(session_uid) or []) if str(item or "").strip()]
        except Exception:
            self._log.exception("session interrupt: list_session_tasks failed session_uid=%s", session_uid)
            return []

    @staticmethod
    def _clear_runtime_markers(session: Any) -> None:
        clear_session_ticks(session)
        clear_runtime_progress(session)
        try:
            setattr(session, "last_tick_ts", None)
            setattr(session, "last_tick_value", None)
            setattr(session, "last_assistant_text_ts", None)
            setattr(session, "last_assistant_text_value", None)
            setattr(session, "assistant_preview_message_id", None)
            setattr(session, "assistant_preview_last_value", None)
            setattr(session, "assistant_preview_creation_attempted", False)
            setattr(session, "tick_seen", 0)
            if not bool(getattr(session, "run_lock", None) and session.run_lock.locked()):
                setattr(session, "busy", False)
        except Exception:
            logging.getLogger(__name__).exception(
                "session interrupt: failed to clear runtime markers session=%s",
                getattr(session, "id", None),
            )

    @staticmethod
    def _backend_state(session: Any) -> str:
        backend = str(getattr(session, "_active_execution_backend", "") or "").strip().lower()
        selected_backend = ""
        try:
            from session import get_session_execution_backend

            selected_backend = str(get_session_execution_backend(session) or "").strip().lower()
        except Exception:
            selected_backend = ""
        proc = getattr(session, "current_proc", None)
        child = getattr(session, "child", None)
        try:
            child_alive = bool(child and child.isalive())
        except Exception:
            child_alive = False

        if proc is not None and getattr(proc, "returncode", None) is None:
            return "headless_running"
        if backend == "headless":
            return "headless_running"
        if backend == "tmux" or selected_backend == "tmux":
            try:
                from app.services.cli_backends.tmux_backend import TmuxExecutionBackend

                paths = TmuxExecutionBackend().paths(session)
                state = TmuxExecutionBackend._read_state(paths)
                tmux_state = str(state.get("state") or "").strip().lower() if isinstance(state, dict) else ""
                if not tmux_state:
                    return "tmux_unknown" if backend == "tmux" else "stopped"
                if tmux_state == "active":
                    return "tmux_active"
                if tmux_state == "idle":
                    return "tmux_idle"
                if tmux_state == "stopped":
                    return "stopped"
                return "tmux_unknown"
            except Exception:
                return "tmux_unknown"
        if backend == "interactive":
            return "interactive_active" if child_alive else "interactive_unknown"
        if child_alive:
            return "idle"
        return "stopped"

    @staticmethod
    def _busy_signals_cleared(session: Any, *, remaining_task_names: tuple[str, ...]) -> bool:
        run_lock = getattr(session, "run_lock", None)
        run_lock_locked = bool(run_lock and hasattr(run_lock, "locked") and run_lock.locked())
        tick_active = False
        try:
            tick_fn = getattr(session, "is_active_by_tick", None)
            if callable(tick_fn):
                tick_active = bool(tick_fn())
        except Exception:
            tick_active = False
        return bool(
            not bool(getattr(session, "busy", False))
            and not run_lock_locked
            and not tick_active
            and not remaining_task_names
        )
