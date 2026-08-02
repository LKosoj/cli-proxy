import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Mapping
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter

from app.services.assistant_preview_service import (
    ASSISTANT_PREVIEW_TIMER_REFRESH_INTERVAL_SEC,
    AssistantPreviewRateLimiter,
    assistant_preview_enabled,
    assistant_preview_supported_dest,
    watch_session_assistant_preview,
)
from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, TmuxRecoveryRequest
from app.services.logging_service import bind_session_log_context

from app.services.runtime_progress_service import clear_runtime_progress, emit_runtime_progress
from app.services.task_bearing_cli_hook_service import get_task_bearing_cli_hook_service
from app.services.session_tick_history_store import clear_session_ticks
from app.services.telegram_transport import TelegramEditOutcome
from session import (
    consume_session_cli_switch_notice_text,
    get_session_execution_backend,
    session_runtime_uid,
    switch_session_active_cli_if_needed,
)
from sessions.conversation_scope import ConversationScope
from sessions.queue_item import normalize_queue_item
from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_pending_input,
    is_orchestrator_enabled,
    set_orchestrator_last_mode_id,
    set_orchestrator_last_mode_output,
    set_orchestrator_pending_input,
)
from app.services.core_orchestration_service import CoreOrchestrationService
from app.services.final_output_delivery import clear_final_output_delivery_guard
from i18n import t
from utils.lang import resolve_user_lang
from utils.text import build_preview, strip_ansi

_SESSION_TASK_MODE_ID = "__session__"
# Вариант меню CLI: одна цифра, необязательный курсор TUI или чекбокс слева.
_CHOICE_OPTION_RE = re.compile(r'^(?:[❯▶►▌»>]\s*)?(?:☐\s*)?([1-9])[\.\)]\s+(\S.*)$')
# Курсор выбора на строке варианта — признак настоящего меню, а не текста агента.
_CHOICE_CURSOR_RE = re.compile(r'^\s*[❯▶►▌»>]\s*(?:☐\s*)?[1-9][\.\)]\s', re.M)
_CHOICE_QUESTION_MAX_LINES = 12
_log = logging.getLogger(__name__)


class SessionRunService:
    def __init__(
        self,
        *,
        bot_app,
        persist_sessions,
        persist_session=None,
        mode_tasks_list,
        mode_tasks_create,
        mode_tasks_track=None,
        log_cli_dialog,
        reset_session_fields_like_sessions_reset,
    ):
        self.bot_app = bot_app
        self._persist_sessions = persist_sessions
        self._persist_session = persist_session
        self._mode_tasks_list = mode_tasks_list
        self._mode_tasks_create = mode_tasks_create
        self._mode_tasks_track = mode_tasks_track
        self._log_cli_dialog = log_cli_dialog
        self._reset_session_fields_like_sessions_reset = reset_session_fields_like_sessions_reset
        self._core_orchestration: Optional[CoreOrchestrationService] = None
        self._assistant_preview_rate_limiter = AssistantPreviewRateLimiter()

    def _get_core_orchestration(self) -> CoreOrchestrationService:
        if self._core_orchestration is None:
            adv_orch = getattr(self.bot_app, "advanced_orchestrator_service", None)
            if adv_orch is None:
                from app.services.advanced_orchestrator_service import AdvancedOrchestratorService
                adv_orch = AdvancedOrchestratorService()
            self._core_orchestration = CoreOrchestrationService(advanced_orchestrator=adv_orch)
        return self._core_orchestration

    def _is_shutting_down(self) -> bool:
        return bool(getattr(self.bot_app, "_shutdown_in_progress", False))

    def _dest_lang(self, dest) -> str:
        try:
            return resolve_user_lang(self.bot_app.config, chat_id=dest.get("chat_id"))
        except Exception:
            return "ru"

    def _safe_create_task(self, coro, *, label: str) -> Optional[asyncio.Task]:
        if self._is_shutting_down():
            try:
                coro.close()
            except Exception:
                _log.exception(
                    "best_effort_cleanup: failed to close coroutine during shutdown operation=%s",
                    label,
                )
            _log.info("skip task scheduling during shutdown: %s", label)
            return None
        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed():
                try:
                    coro.close()
                except Exception:
                    _log.exception(
                        "best_effort_cleanup: failed to close coroutine for closed event loop operation=%s",
                        label,
                    )
                _log.warning("event loop is closed; skip scheduling task: %s", label)
                return None
            return loop.create_task(coro)
        except Exception:
            try:
                coro.close()
            except Exception:
                _log.exception(
                    "best_effort_cleanup: failed to close coroutine after scheduling failure operation=%s",
                    label,
                )
            _log.exception("failed to schedule task: %s", label)
            return None

    def _persist_for_session(self, session, *, fallback_chat_id: Optional[int] = None) -> None:
        callback = self._persist_session
        if callable(callback):
            session_id = str(getattr(session, "id", "") or "").strip()
            chat_id = getattr(session, "chat_id", None)
            if chat_id is None:
                chat_id = fallback_chat_id
            if session_id and chat_id is not None:
                try:
                    if bool(callback(int(chat_id), session_id)):
                        return
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
        self._persist_sessions()

    def _track_session_task(self, session, *, name: str) -> None:
        tracker = self._mode_tasks_track
        if not callable(tracker):
            return
        try:
            tracker(
                session_id=session_runtime_uid(session),
                mode_id=_SESSION_TASK_MODE_ID,
                name=name,
            )
        except Exception:
            logging.exception(
                "failed to track session task session=%s name=%s",
                getattr(session, "id", "?"),
                name,
            )

    def start_session_task(self, session, *, coro, name: str) -> bool:
        if self._is_shutting_down():
            try:
                coro.close()
            except Exception:
                logging.getLogger(__name__).exception("failed to close coroutine during shutdown: %s", name)
            logging.getLogger(__name__).info("skip session task scheduling during shutdown: %s", name)
            return False
        self._mode_tasks_create(
            session_id=session_runtime_uid(session),
            mode_id=_SESSION_TASK_MODE_ID,
            coro=coro,
            name=name,
        )
        return True

    def start_prompt_task(self, session, prompt: str, dest: dict, context, *, task_name: str = "run_prompt") -> bool:
        return self.start_session_task(
            session,
            coro=self.run_prompt(session, prompt, dest, context),
            name=task_name,
        )

    def _recovery_dest(self, session, request: TmuxRecoveryRequest) -> dict:
        persisted = dict(request.dest or {})
        chat_id = persisted.get("chat_id")
        if chat_id is None:
            scope = getattr(session, "conversation_scope", None)
            chat_id = getattr(scope, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(session, "chat_id", None)
        base: dict = {"kind": "telegram"}
        builder = getattr(self.bot_app, "build_telegram_reply_dest", None)
        if chat_id is not None and callable(builder):
            base = builder(
                session,
                int(chat_id),
                user_id=persisted.get("user_id"),
                direct_messages_topic_id=persisted.get("direct_messages_topic_id"),
            )
        elif chat_id is not None:
            base["chat_id"] = int(chat_id)
        base.update(persisted)
        return base

    async def recover_tmux_sessions(self, context) -> int:
        manager = getattr(self.bot_app, "manager", None)
        sessions_by_chat = getattr(manager, "sessions_by_chat", None)
        if not isinstance(sessions_by_chat, dict):
            return 0
        recovered = 0
        seen: set[int] = set()
        for sessions in sessions_by_chat.values():
            if not isinstance(sessions, dict):
                continue
            for session in sessions.values():
                if id(session) in seen:
                    continue
                seen.add(id(session))
                if get_session_execution_backend(session) != "tmux":
                    continue
                request_id = str(getattr(session, "_tmux_recovery_request_id", "") or "").strip()
                if request_id:
                    continue
                try:
                    request = await TmuxExecutionBackend().get_recovery_request(session)
                except Exception:
                    _log.exception("tmux startup reconciliation failed session=%s", getattr(session, "id", "?"))
                    continue
                if request is None:
                    continue
                if self._start_tmux_recovery(session, request, context):
                    recovered += 1
        return recovered

    def _start_tmux_recovery(self, session, request: TmuxRecoveryRequest, context) -> bool:
        dest = self._recovery_dest(session, request)
        if str(dest.get("kind") or "telegram").strip().lower() == "telegram" and dest.get("chat_id") is None:
            _log.error(
                "tmux recovery skipped: Telegram destination is missing session=%s request_id=%s",
                getattr(session, "id", "?"),
                request.request_id,
            )
            return False
        session._tmux_recovery_request_id = request.request_id
        session._active_execution_backend = "tmux"
        session.busy = True
        prompt = request.prompt or f"Recovered tmux request {request.request_id}"
        started = self.start_session_task(
            session,
            coro=self.run_prompt(
                session,
                prompt,
                dest,
                context,
                recovery_request=request,
            ),
            name=f"tmux_recovery:{request.request_id}",
        )
        if not started:
            session._tmux_recovery_request_id = None
            session._active_execution_backend = "none"
            session.busy = False
        return started

    async def _detach_tmux_monitor(self, session) -> None:
        """Снять текущие задачи сессии, не трогая сам CLI в pane."""

        control = getattr(self.bot_app, "mode_session_control", None)
        cancel = getattr(control, "cancel_session", None)
        if not callable(cancel):
            return
        # Обычная отмена монитора шлёт Ctrl+C в pane, то есть прерывает работающий
        # CLI. Здесь прерывается только чтение вывода, поэтому взводим тот же флаг,
        # что и на выключении бота.
        setattr(session, "_preserve_tmux_on_shutdown", True)
        try:
            await cancel(session_id=session_runtime_uid(session), timeout_s=5.0)
        except Exception:
            _log.exception("tmux reread: task cancellation failed session=%s", getattr(session, "id", "?"))
        finally:
            setattr(session, "_preserve_tmux_on_shutdown", False)

    async def reread_tmux_output(self, session, context) -> str:
        """Переподключить чтение вывода tmux для активного запроса сессии.

        Возвращает код результата: started / not_tmux / no_request / failed.
        """

        if get_session_execution_backend(session) != "tmux":
            return "not_tmux"
        try:
            request = await TmuxExecutionBackend().get_recovery_request(session)
        except Exception:
            _log.exception("tmux reread: recovery request failed session=%s", getattr(session, "id", "?"))
            return "failed"
        if request is None:
            return "no_request"
        await self._detach_tmux_monitor(session)
        # Вопрос с кнопками отправляется один раз на кадр TUI: без сброса тот же
        # кадр после перечитывания будет молча подавлен как дубль.
        clear_pending = getattr(self.bot_app, "_clear_pending_questions", None)
        if callable(clear_pending):
            try:
                clear_pending(session_id=str(getattr(session, "id", "") or ""))
            except Exception:
                _log.exception("tmux reread: pending questions cleanup failed session=%s", getattr(session, "id", "?"))
        setattr(session, "_tmux_recovery_request_id", None)
        return "started" if self._start_tmux_recovery(session, request, context) else "failed"

    @classmethod
    def _is_cli_choice_question(cls, text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        if "enter selection [" in t or "or escape to cancel" in t or "or esc to cancel" in t:
            return True
        # "esc to interrupt" / "shift+tab" — это индикатор работы и подсказка в
        # футере TUI, а не вопрос: по ним нельзя распознавать выбор.
        _, options = cls._parse_cli_choice_question(text)
        if len(options) < 2:
            return False
        # Нумерованный список сам по себе ничего не значит: его печатают и команды
        # (`find -printf '%T@ %p'`, `ls -l`), и сам агент в ответе. Меню TUI
        # отличают курсор выбора или чекбоксы.
        return bool(_CHOICE_CURSOR_RE.search(text)) or "☐" in text

    @staticmethod
    def _tmux_choice_question_id(session_key: str, question: str, options: List[str]) -> str:
        """Идентификатор вопроса по его содержимому: повтор того же кадра TUI
        даёт тот же id, поэтому одинаковые вопросы не отправляются дважды."""

        payload = "\x00".join([str(question), *[str(option) for option in options]])
        digest = hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"tmux_choice_{session_key}_{digest}"

    @staticmethod
    def _parse_cli_choice_question(text: str) -> Tuple[str, List[str]]:
        if not text:
            return "Please choose an option:", []
        lines = text.splitlines()
        q_lines = []
        opts = []
        for line in lines:
            s = line.strip()
            # Support plain "1. foo", "1) foo", "☐ 1. foo", "❯ 1. foo"
            m = _CHOICE_OPTION_RE.match(s)
            # Нумерация меню идёт подряд с единицы, поэтому "1754161234.567 /tmp/x"
            # из вывода команд вариантом не становится.
            if m and int(m.group(1)) == len(opts) + 1:
                opts.append(f"{m.group(1)}. {m.group(2).strip()}")
            else:
                if not opts:
                    q_lines.append(line)
        # Вопрос стоит прямо над вариантами и отделён от остального вывода пустой
        # строкой: без этого в него попадал весь экран TUI, включая логи команд.
        if opts:
            tail: List[str] = []
            for line in reversed(q_lines):
                if not line.strip() or len(tail) >= _CHOICE_QUESTION_MAX_LINES:
                    break
                tail.append(line)
            q_lines = list(reversed(tail))
        q = "\n".join(q_lines).strip() or "Please choose:"
        # Заглушки "Option 1..5" не подставляются: без распознанных вариантов
        # кнопки бессмысленны, вызывающий код покажет обычное превью.
        return q, opts[:5]

    @classmethod
    def _is_tmux_choice_preview(cls, session, text: str) -> bool:
        return get_session_execution_backend(session) == "tmux" and cls._is_cli_choice_question(text)

    async def _send_output_task(self, session, dest: dict, output: str, context) -> bool:
        sent = False
        try:
            await self.bot_app.send_output(session, dest, output, context)
            sent = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.getLogger("bot.send_output").exception("[send_output] task failed: %s", e)
        finally:
            if sent or not str(output or "").strip():
                await self._clear_telegram_assistant_preview(session, dest, context)
        return sent or not str(output or "").strip()

    @staticmethod
    def _telegram_reply_kwargs(
        dest: Optional[dict],
        *,
        fallback_chat_id: Optional[int] = None,
        session=None,
    ) -> dict:
        kwargs: dict = {}
        if isinstance(dest, dict):
            chat_id = dest.get("chat_id")
            if chat_id is not None:
                kwargs["chat_id"] = chat_id
            message_thread_id = dest.get("message_thread_id")
            if message_thread_id is None:
                scope = getattr(session, "conversation_scope", None)
                if (
                    isinstance(scope, ConversationScope)
                    and scope.message_thread_id is not None
                    and chat_id is not None
                    and int(scope.chat_id) == int(chat_id)
                ):
                    message_thread_id = int(scope.message_thread_id)
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            direct_messages_topic_id = dest.get("direct_messages_topic_id")
            if direct_messages_topic_id is not None:
                kwargs["direct_messages_topic_id"] = direct_messages_topic_id
        if "chat_id" not in kwargs and fallback_chat_id is not None:
            kwargs["chat_id"] = fallback_chat_id
        return kwargs

    @classmethod
    def _merge_fallback_dest(cls, next_dest: dict, fallback_dest: dict) -> dict:
        merged = dict(next_dest or {})
        if merged.get("kind") == "telegram":
            if merged.get("chat_id") is None:
                merged["chat_id"] = fallback_dest.get("chat_id")
            if merged.get("user_id") is None:
                merged["user_id"] = fallback_dest.get("user_id")
            if merged.get("message_thread_id") is None and fallback_dest.get("message_thread_id") is not None:
                merged["message_thread_id"] = fallback_dest.get("message_thread_id")
            if (
                merged.get("direct_messages_topic_id") is None
                and fallback_dest.get("direct_messages_topic_id") is not None
            ):
                merged["direct_messages_topic_id"] = fallback_dest.get("direct_messages_topic_id")
        return merged

    @staticmethod
    def _reset_assistant_preview_state(session) -> None:
        session.assistant_preview_message_id = None
        session.assistant_preview_last_value = None
        session.assistant_preview_creation_attempted = False

    def _mode_framework_sends_output(self, mode, *, session, mode_id: str) -> bool:
        framework_sends_output = getattr(mode, "framework_sends_output", None)
        if not callable(framework_sends_output):
            return True
        try:
            return bool(framework_sends_output())
        except Exception:
            _log.debug(
                "legacy_fallback: failed to resolve framework output policy session=%s mode=%s",
                getattr(session, "id", "?"),
                mode_id,
                exc_info=True,
            )
            return True

    def _telegram_assistant_preview_enabled(self, session, dest: Optional[dict]) -> bool:
        config = getattr(session, "config", None) or getattr(self.bot_app, "config", None)
        if not assistant_preview_enabled(config):
            return False
        if not assistant_preview_supported_dest(dest):
            return False
        if str((dest or {}).get("kind") or "").strip().lower() != "telegram":
            return False
        reply_kwargs = self._telegram_reply_kwargs(dest, session=session)
        return reply_kwargs.get("chat_id") is not None

    async def _upsert_telegram_assistant_preview(
        self,
        session,
        dest: dict,
        context,
        text: Optional[str],
        *,
        timer_only: bool = False,
    ) -> None:
        if not text:
            return
        if self._is_tmux_choice_preview(session, text):
            question, options = self._parse_cli_choice_question(text)
            if options:
                qid = self._tmux_choice_question_id(str(getattr(session, "id", "x")), question, options)
                if qid in self.bot_app.ui_state.pending_questions:
                    return  # этот вопрос уже отправлен, повтор кадра игнорируем
                meta = {
                    "question": question,
                    "options": options,
                    "chat_id": dest.get("chat_id"),
                    "message_thread_id": dest.get("message_thread_id"),
                    "session_id": str(getattr(session, "id", "")),
                    "tmux_feed": True,
                }
                self.bot_app.ui_state.pending_questions[qid] = meta
                try:
                    cid = int(dest.get("chat_id") or 0)
                    await self.bot_app._send_ask_question(
                        context,
                        cid,
                        str(getattr(session, "id", "")),
                        qid,
                        question,
                        options,
                        allow_custom=False,
                        system_options=False,
                        message_thread_id=dest.get("message_thread_id"),
                    )
                except Exception:
                    logging.getLogger("bot.preview").exception("failed to send tmux choice as ask from preview")
                return  # do not show plain preview for the choice question
        send_message = getattr(self.bot_app, "_send_message", None)
        edit_message = getattr(self.bot_app, "_edit_message", None)
        edit_message_outcome = getattr(self.bot_app, "_edit_message_outcome", None)
        if not callable(send_message):
            return
        reply_kwargs = self._telegram_reply_kwargs(dest, session=session)
        chat_id = reply_kwargs.get("chat_id")
        if chat_id is None:
            return
        async with session.send_lock:
            if (
                session.assistant_preview_message_id is not None
                and session.assistant_preview_last_value == text
            ):
                return
            if session.assistant_preview_message_id is None:
                await self._send_telegram_assistant_preview_locked(
                    session,
                    context,
                    text,
                    reply_kwargs,
                )
                return
        if not callable(edit_message_outcome) and not callable(edit_message):
            return
        await self._assistant_preview_rate_limiter.submit(
            chat_id=int(chat_id),
            owner=session_runtime_uid(session),
            timer_only=timer_only,
            apply_update=lambda: self._edit_telegram_assistant_preview(
                session,
                context,
                text,
                reply_kwargs,
            ),
        )

    async def _send_telegram_assistant_preview_locked(
        self,
        session,
        context,
        text: str,
        reply_kwargs: dict,
    ) -> None:
        if getattr(session, "assistant_preview_creation_attempted", False):
            return
        session.assistant_preview_creation_attempted = True
        try:
            message = await self.bot_app._send_message(
                context,
                text=text,
                prefer_rich=False,
                **reply_kwargs,
            )
        except Exception:
            _log.exception(
                "assistant preview send failed session=%s chat_id=%s",
                getattr(session, "id", "?"),
                reply_kwargs.get("chat_id"),
            )
            return
        message_id = getattr(message, "message_id", None)
        if message_id is not None:
            session.assistant_preview_message_id = int(message_id)
            session.assistant_preview_last_value = text

    async def _edit_telegram_assistant_preview(
        self,
        session,
        context,
        text: str,
        reply_kwargs: dict,
    ) -> None:
        edit_message = getattr(self.bot_app, "_edit_message", None)
        edit_message_outcome = getattr(self.bot_app, "_edit_message_outcome", None)
        async with session.send_lock:
            message_id = getattr(session, "assistant_preview_message_id", None)
            if message_id is None or session.assistant_preview_last_value == text:
                return
            try:
                if callable(edit_message_outcome):
                    outcome = await edit_message_outcome(
                        context,
                        chat_id=int(reply_kwargs["chat_id"]),
                        message_id=int(message_id),
                        text=text,
                        md2=True,
                        prefer_rich=False,
                    )
                elif callable(edit_message):
                    edited = await edit_message(
                        context,
                        chat_id=int(reply_kwargs["chat_id"]),
                        message_id=int(message_id),
                        text=text,
                        md2=True,
                        prefer_rich=False,
                    )
                    outcome = TelegramEditOutcome.UPDATED if edited else TelegramEditOutcome.RETRY
                else:
                    return
            except RetryAfter:
                raise
            except Exception:
                _log.debug(
                    "assistant preview edit failed; keeping current message session=%s chat_id=%s message_id=%s",
                    getattr(session, "id", "?"),
                    reply_kwargs.get("chat_id"),
                    message_id,
                    exc_info=True,
                )
                return
            if outcome is TelegramEditOutcome.UPDATED:
                session.assistant_preview_last_value = text
                return
            if outcome is not TelegramEditOutcome.REPLACE:
                return
            session.assistant_preview_message_id = None
            session.assistant_preview_last_value = None
            session.assistant_preview_creation_attempted = False
            await self._send_telegram_assistant_preview_locked(
                session,
                context,
                text,
                reply_kwargs,
            )

    async def _clear_telegram_assistant_preview(self, session, dest: Optional[dict], context) -> bool:
        reply_kwargs = self._telegram_reply_kwargs(dest, session=session)
        chat_id = reply_kwargs.get("chat_id")
        if chat_id is not None:
            await self._assistant_preview_rate_limiter.cancel(
                chat_id=int(chat_id),
                owner=session_runtime_uid(session),
            )
        delete_message = getattr(self.bot_app, "_delete_message", None)
        if chat_id is None:
            self._reset_assistant_preview_state(session)
            return False
        async with session.send_lock:
            message_id = getattr(session, "assistant_preview_message_id", None)
            if message_id is None:
                self._reset_assistant_preview_state(session)
                return True
            if not callable(delete_message):
                self._reset_assistant_preview_state(session)
                return False
            try:
                deleted = bool(await delete_message(context, int(chat_id), int(message_id)))
            except Exception:
                logging.getLogger(__name__).exception(
                    "assistant preview delete failed session=%s chat_id=%s message_id=%s",
                    getattr(session, "id", "?"),
                    chat_id,
                    message_id,
                )
                self._reset_assistant_preview_state(session)
                return False
            if deleted:
                self._reset_assistant_preview_state(session)
                return True
            logging.getLogger(__name__).warning(
                "assistant preview delete returned false session=%s chat_id=%s message_id=%s",
                getattr(session, "id", "?"),
                chat_id,
                message_id,
            )
            self._reset_assistant_preview_state(session)
            return False

    @staticmethod
    def _orchestrator_keyboard(session_uid: str, target_mode_id: str, lang: str = "ru") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("btn.orch_transition.apply", lang),
                        callback_data=f"orch_transition:apply:{session_uid}:{target_mode_id}",
                    ),
                    InlineKeyboardButton(
                        t("btn.orch_transition.cancel", lang),
                        callback_data=f"orch_transition:cancel:{session_uid}",
                    ),
                ]
            ]
        )

    async def _maybe_offer_post_run_transition(
        self,
        *,
        session,
        mode_id: str,
        output_text: str,
        dest: dict,
        context,
    ) -> None:
        if not is_orchestrator_enabled(session, False):
            return
        if get_orchestrator_pending_input(session, None):
            return
        if str(mode_id or "").strip() == "agent":
            return

        chat_id = dest.get("chat_id")
        if chat_id is None:
            return
        reply_kwargs = self._telegram_reply_kwargs(dest, session=session)

        orch = getattr(self.bot_app, "advanced_orchestrator_service", None)
        mode_registry = getattr(self.bot_app, "mode_registry_service", None)
        if orch is None:
            return

        proposal = None
        try:
            proposal = await orch.propose_transition_hybrid(
                session=session,
                text=str(output_text or ""),
                mode_registry=mode_registry,
                app_config=getattr(self.bot_app, "config", None),
                llm_router_fn=getattr(self.bot_app, "orchestrator_chat_completion", None),
            )
        except Exception:
            logging.getLogger(__name__).exception("post-run orchestrator proposal failed")
            return
        if proposal is None:
            return
        previous_mode_id = str(getattr(session, "_orchestrator_prev_mode_id", "") or "").strip()
        is_return_to_previous = bool(
            previous_mode_id and str(proposal.target_mode_id or "").strip() == previous_mode_id
        )

        handoff_text = orch.build_handoff_input(
            session=session,
            original_user_text=str(output_text or ""),
        )
        set_orchestrator_pending_input(session, {
            "text": handoff_text,
            "dest": dict(dest or {"kind": "telegram", "chat_id": int(chat_id)}),
            "target_mode_id": str(proposal.target_mode_id),
            "disable_orchestrator_on_cancel": True,
        })
        lang = self._dest_lang(dest)
        current_label = orch.current_mode_label(session=session, mode_registry=mode_registry)
        confirm_text = t("run.orch_transition_ready", lang) + orch.build_confirm_text(
            current_mode_label=current_label,
            proposal=proposal,
        )
        if is_return_to_previous:
            confirm_text += t("run.orch_transition_return_warning", lang)
        session_uid = session_runtime_uid(session)
        await self.bot_app._send_message(
            context,
            text=confirm_text,
            reply_markup=self._orchestrator_keyboard(
                session_uid=session_uid,
                target_mode_id=proposal.target_mode_id,
                lang=lang,
            ),
            **reply_kwargs,
        )

    async def run_prompt(
        self,
        session,
        prompt: str,
        dest: dict,
        context,
        *,
        recovery_request: Optional[TmuxRecoveryRequest] = None,
    ) -> None:
        self._track_session_task(session, name="run_prompt")
        _rp_log = logging.getLogger("bot.run_prompt")
        with bind_session_log_context(session=session, chat_id=dest.get("chat_id")):
            _rp_log.info("[run_prompt] acquiring run_lock session=%s prompt=%r", session.id, prompt[:100])
            async with session.run_lock:
                _rp_log.info("[run_prompt] lock acquired session=%s", session.id)
                session.busy = True
                session.started_at = time.time()
                session.last_output_ts = session.started_at
                session.last_tick_ts = None
                session.last_tick_value = None
                session.last_assistant_text_ts = None
                session.last_assistant_text_value = None
                self._reset_assistant_preview_state(session)
                clear_final_output_delivery_guard(session)
                session.tick_seen = 0
                clear_session_ticks(session)
                clear_runtime_progress(session)
                image_path = dest.get("image_path")
                image_paths = dest.get("image_paths")
                source = "telegram_direct" if str((dest or {}).get("kind") or "").strip().lower() == "telegram" else "session_run_service"
                hook = None
                prepared = None
                prompt_for_cli = prompt
                preview_stop_event: Optional[asyncio.Event] = None
                preview_task: Optional[asyncio.Task] = None
                send_output_scheduled = False
                managed_tmux = False
                queue_drain_allowed = False
                hook_config = getattr(session, "config", None) or getattr(self.bot_app, "config", None)
                try:
                    if recovery_request is None:
                        cli_switch = await switch_session_active_cli_if_needed(session)
                        if cli_switch.switched:
                            self._persist_for_session(session, fallback_chat_id=dest.get("chat_id"))
                        switch_notice = consume_session_cli_switch_notice_text(session, lang=self._dest_lang(dest))
                    else:
                        switch_notice = None
                    if switch_notice and dest.get("chat_id") is not None:
                        await self.bot_app._send_message(
                            context,
                            text=switch_notice,
                            **self._telegram_reply_kwargs(dest, session=session),
                        )
                    managed_tmux = recovery_request is not None or get_session_execution_backend(session) == "tmux"
                    queue_drain_allowed = not managed_tmux
                    if hook_config is not None and recovery_request is None:
                        hook = get_task_bearing_cli_hook_service(hook_config)
                        prepared = await hook.prepare_prompt(
                            session=session,
                            prompt=prompt,
                            source=source,
                            phase="execute",
                            task_bearing=True,
                        )
                        prompt_for_cli = prepared.prompt_for_cli
                    if self._telegram_assistant_preview_enabled(session, dest):
                        preview_stop_event = asyncio.Event()
                        preview_task = self._safe_create_task(
                            watch_session_assistant_preview(
                                session,
                                emit_update=lambda text, timer_only: self._upsert_telegram_assistant_preview(
                                    session,
                                    dest,
                                    context,
                                    text,
                                    timer_only=timer_only,
                                ),
                                stop_event=preview_stop_event,
                                include_elapsed_time=True,
                                refresh_interval_sec=ASSISTANT_PREVIEW_TIMER_REFRESH_INTERVAL_SEC,
                            ),
                            label=f"assistant_preview:{session.id}",
                        )
                    _rp_log.info("[run_prompt] calling session.run_prompt session=%s", session.id)
                    if recovery_request is not None:
                        output = await session.recover_tmux_request(recovery_request)
                    else:
                        run_kwargs = {
                            "image_path": image_path,
                            "image_paths": image_paths,
                        }
                        if managed_tmux:
                            run_kwargs["tmux_request_context"] = {
                                "prompt": prompt,
                                "dest": dict(dest or {}),
                            }
                        output = await session.run_prompt(prompt_for_cli, **run_kwargs)
                    _rp_log.info("[run_prompt] session.run_prompt returned session=%s output_len=%d", session.id, len(output))
                    if preview_stop_event is not None:
                        preview_stop_event.set()
                    if preview_task is not None:
                        await asyncio.gather(preview_task, return_exceptions=True)

                    # Просто удалить превью перед отправкой финального сообщения.
                    await self._clear_telegram_assistant_preview(session, dest, context)
                    if hook is not None and prepared is not None:
                        hook.record_success(prepared, output=output)
                    self._log_cli_dialog(
                        session,
                        prompt,
                        output,
                        chat_id=dest.get("chat_id"),
                        image_path=image_path,
                        image_paths=image_paths,
                    )
                    tmux_request_id = ""
                    if recovery_request is not None:
                        tmux_request_id = recovery_request.request_id
                    elif managed_tmux:
                        tmux_request_id = str(getattr(session, "last_tmux_request_id", "") or "").strip()
                    if tmux_request_id:
                        send_output_scheduled = True
                        delivered = await self._send_output_task(session, dest, output, context)
                        if delivered:
                            queue_drain_allowed = TmuxExecutionBackend.mark_request_delivered(
                                session,
                                tmux_request_id,
                            )
                            if not queue_drain_allowed:
                                _log.error(
                                    "tmux delivery marker was not persisted session=%s request_id=%s",
                                    session.id,
                                    tmux_request_id,
                                )
                    else:
                        send_output_scheduled = self.start_session_task(
                            session,
                            coro=self._send_output_task(session, dest, output, context),
                            name="send_output",
                        )
                    if not send_output_scheduled:
                        await self._clear_telegram_assistant_preview(session, dest, context)
                    forced = getattr(session, "headless_forced_stop", None)
                    if forced:
                        details = f"{session.id} ({session.name or session.tool.name}) @ {session.workdir}"
                        msg = t("run.cli_abnormal", self._dest_lang(dest), details=details)
                        if dest.get("chat_id") is not None:
                            await self.bot_app._send_message(
                                context,
                                text=msg,
                                **self._telegram_reply_kwargs(dest, session=session),
                            )
                        session.headless_forced_stop = None
                except Exception as e:
                    if preview_stop_event is not None:
                        preview_stop_event.set()
                    if preview_task is not None:
                        await asyncio.gather(preview_task, return_exceptions=True)
                    if hook is not None and prepared is not None:
                        hook.record_error(prepared, error=e)
                    logging.exception(f"tool failed {str(e)}")
                    if dest.get("chat_id") is not None:
                        await self.bot_app._send_message(
                            context,
                            text=t("run.exec_error", self._dest_lang(dest), e=e),
                            **self._telegram_reply_kwargs(dest, session=session),
                        )
                    await self._clear_telegram_assistant_preview(session, dest, context)
                finally:
                    if preview_stop_event is not None and not preview_stop_event.is_set():
                        preview_stop_event.set()
                    if preview_task is not None and not preview_task.done():
                        await asyncio.gather(preview_task, return_exceptions=True)
                    if not send_output_scheduled:
                        await self._clear_telegram_assistant_preview(session, dest, context)
                    session.busy = False
                    try:
                        if queue_drain_allowed and session.queue:
                            raw_next_item = session.queue.popleft()
                            try:
                                fallback_dest = self._merge_fallback_dest({"kind": "telegram"}, dest)
                                next_item = normalize_queue_item(raw_next_item, fallback_dest=fallback_dest)
                                next_prompt = next_item.text
                                next_dest = self._merge_fallback_dest(next_item.dest, dest)
                                if isinstance(raw_next_item, Mapping):
                                    image_paths = raw_next_item.get("image_paths")
                                    if image_paths:
                                        next_dest["image_paths"] = list(image_paths)
                                        next_dest["cleanup_images"] = True
                                    image_path = raw_next_item.get("image_path")
                                    if image_path:
                                        next_dest["image_path"] = image_path
                                        next_dest["cleanup_image"] = True
                                if not self.start_prompt_task(
                                    session,
                                    next_prompt,
                                    next_dest,
                                    context,
                                    task_name="run_prompt.queue_next",
                                ):
                                    session.queue.appendleft(raw_next_item)
                                    _log.warning(
                                        "prompt queue drain: task start rejected, item rolled back session=%s",
                                        session.id,
                                    )
                                else:
                                    self._persist_for_session(session, fallback_chat_id=dest.get("chat_id"))
                            except Exception:
                                session.queue.appendleft(raw_next_item)
                                _log.exception(
                                    "prompt queue drain: item prep failed, rolled back session=%s", session.id,
                                )
                    except Exception:
                        _log.exception(
                            "prompt queue drain failed session=%s", session.id,
                        )
                    if recovery_request is not None:
                        setattr(session, "_tmux_recovery_request_id", None)

    async def run_mode_pipeline(self, session, prompt: str, dest: dict, context, *, mode_id: str) -> None:
        mode_id = str(mode_id or "").strip()
        if not mode_id:
            raise RuntimeError("mode_id is required for run_mode_pipeline")
        _rw_log = logging.getLogger(f"bot.run_mode.{mode_id}")
        with bind_session_log_context(session=session, chat_id=dest.get("chat_id")):
            _rw_log.info("[run_mode] acquiring run_lock session=%s mode=%s prompt=%r", session.id, mode_id, prompt[:100])
            async with session.run_lock:
                _rw_log.info("[run_mode] lock acquired session=%s mode=%s", session.id, mode_id)
                session.busy = True
                session.started_at = time.time()
                session.last_output_ts = session.started_at
                session.last_tick_ts = None
                session.last_tick_value = None
                session.last_assistant_text_ts = None
                session.last_assistant_text_value = None
                self._reset_assistant_preview_state(session)
                clear_final_output_delivery_guard(session)
                session.tick_seen = 0
                clear_session_ticks(session)
                clear_runtime_progress(session)
                try:
                    emit_runtime_progress(
                        session,
                        {
                            "mode_id": mode_id,
                            "source": "mode_pipeline",
                            "phase": "start",
                            "status": "running",
                            "message": f"Запуск режима {mode_id}",
                        },
                    )
                    registry = getattr(self.bot_app, "mode_registry", None)
                    mode = registry.get(mode_id) if registry else None
                    if mode is None or not hasattr(mode, "run_pipeline"):
                        raise RuntimeError(f"Режим {mode_id} недоступен")
                    preserve_mode_id = None
                    if hasattr(mode, "pre_run_reset_mode_id"):
                        preserve_mode_id = str(mode.pre_run_reset_mode_id() or "").strip() or None
                    if preserve_mode_id:
                        self._reset_session_fields_like_sessions_reset(session, preserve_mode_id=preserve_mode_id)
                        self._persist_for_session(session, fallback_chat_id=dest.get("chat_id"))
                    output = await self._get_core_orchestration().execute_mode_run(
                        session=session,
                        mode=mode,
                        mode_id=mode_id,
                        user_text=prompt,
                        bot_app=self.bot_app,
                        context=context,
                        dest=dest,
                    )
                    try:
                        set_orchestrator_last_mode_output(session, str(output or ""))
                        set_orchestrator_last_mode_id(session, str(mode_id or ""))
                    except Exception:
                        logging.exception("failed to capture orchestrator mode output session=%s mode=%s", session.id, mode_id)
                    now = time.time()
                    session.last_output_ts = now
                    emit_runtime_progress(
                        session,
                        {
                            "mode_id": mode_id,
                            "source": "mode_pipeline",
                            "phase": "final",
                            "status": "ok",
                            "message": f"Режим {mode_id} завершен",
                        },
                    )
                    if self._mode_framework_sends_output(mode, session=session, mode_id=mode_id):
                        await self.bot_app.send_output(
                            session, dest, str(output or ""), context, send_header=False
                        )
                    try:
                        await self._maybe_offer_post_run_transition(
                            session=session,
                            mode_id=mode_id,
                            output_text=str(output or ""),
                            dest=dest,
                            context=context,
                        )
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "post-run orchestrator transition offer failed session=%s mode=%s",
                            session.id,
                            mode_id,
                        )
                    try:
                        preview = build_preview(strip_ansi(str(output or "")), self.bot_app.config.defaults.summary_max_chars)
                        session.state_summary = preview
                        session.state_updated_at = time.time()
                        self._persist_for_session(session, fallback_chat_id=dest.get("chat_id"))
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                except asyncio.CancelledError:
                    _rw_log.warning("[run_mode] CancelledError session=%s mode=%s", session.id, mode_id)
                    emit_runtime_progress(
                        session,
                        {
                            "mode_id": mode_id,
                            "source": "mode_pipeline",
                            "phase": "cancelled",
                            "status": "error",
                            "message": f"Режим {mode_id} прерван",
                        },
                    )
                    if dest.get("chat_id") is not None:
                        await self.bot_app._send_message(
                            context,
                            text=t("run.mode_interrupted", self._dest_lang(dest), mode_id=mode_id),
                            **self._telegram_reply_kwargs(dest, session=session),
                        )
                    raise
                except Exception as e:
                    _rw_log.exception("[run_mode] exception session=%s mode=%s: %s", session.id, mode_id, e)
                    emit_runtime_progress(
                        session,
                        {
                            "mode_id": mode_id,
                            "source": "mode_pipeline",
                            "phase": "error",
                            "status": "error",
                            "message": f"Ошибка режима {mode_id}: {e}",
                        },
                    )
                    if dest.get("chat_id") is not None:
                        await self.bot_app._send_message(
                            context,
                            text=t("run.mode_error", self._dest_lang(dest), mode_id=mode_id, e=e),
                            **self._telegram_reply_kwargs(dest, session=session),
                        )
                finally:
                    session.busy = False
                    try:
                        if session.queue:
                            raw_next_item = session.queue.popleft()
                            try:
                                fallback_dest = self._merge_fallback_dest({"kind": "telegram"}, dest)
                                next_item = normalize_queue_item(raw_next_item, fallback_dest=fallback_dest)
                                next_prompt = next_item.text
                                next_dest = self._merge_fallback_dest(next_item.dest, dest)
                                if not self.start_session_task(
                                    session,
                                    coro=self.dispatch_queued_input(session, next_prompt, next_dest, context, fallback_dest=dest),
                                    name="dispatch_queued_input",
                                ):
                                    session.queue.appendleft(raw_next_item)
                                    _log.warning(
                                        "mode pipeline queue drain: task start rejected, item rolled back session=%s mode=%s",
                                        session.id,
                                        mode_id,
                                    )
                                else:
                                    self._persist_for_session(session, fallback_chat_id=dest.get("chat_id"))
                            except Exception:
                                session.queue.appendleft(raw_next_item)
                                _log.exception(
                                    "mode pipeline queue drain: item prep failed, rolled back session=%s mode=%s",
                                    session.id,
                                    mode_id,
                                )
                    except Exception:
                        _log.exception(
                            "mode pipeline queue drain failed session=%s mode=%s", session.id, mode_id,
                        )

    async def dispatch_queued_input(self, session, next_prompt: str, next_dest: dict, context, *, fallback_dest: dict) -> None:
        next_dest = self._merge_fallback_dest(next_dest, fallback_dest)
        chat_id = next_dest.get("chat_id")
        if chat_id is None:
            self.start_prompt_task(
                session,
                next_prompt,
                next_dest,
                context,
                task_name="dispatch_queued_input.no_chat_id",
            )
            return
        await self.bot_app._handle_user_input(
            session,
            next_prompt,
            int(chat_id),
            context,
            dest=next_dest,
        )

    def start_mode_task(self, session, prompt: str, dest: dict, context, *, mode_id=None) -> None:
        mode_id = str(mode_id or get_active_mode(session, "") or "").strip()
        if not mode_id:
            return
        session_uid = session_runtime_uid(session)
        if self._mode_tasks_list(session_id=session_uid, mode_id=mode_id):
            return
        coro = self.run_mode_pipeline(session, prompt, dest, context, mode_id=mode_id)
        task_name = f"run_{mode_id}"
        self._mode_tasks_create(session_id=session_uid, mode_id=mode_id, coro=coro, name=task_name)
