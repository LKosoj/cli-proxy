import asyncio
import logging
import os
import time
from typing import Optional

from i18n import t
from sessions.conversation_scope import ConversationScope
from tg.rich import is_rich_markdown_eligible
from utils.text import build_preview, strip_ansi
from utils.lang import resolve_user_lang


class SessionOutputService:
    def __init__(
        self,
        *,
        bot_app,
        persist_sessions,
        persist_session=None,
        html_process_pool,
        html_process_threshold_chars: int = 100_000,
        html_render_tail_chars: int = 70_000,
        summary_prepare_threshold_chars: int = 50_000,
        summary_tail_chars: int = 70_000,
        summary_wait_for_html_s: float = 5.0,
        summary_timeout_s: float = 100.0,
        summarize_fn=None,
        summarize_fn_getter=None,
        ansi_to_html_fn=None,
        ansi_to_html_fn_getter=None,
        make_html_file_fn=None,
        make_html_file_fn_getter=None,
    ):
        self.bot_app = bot_app
        self._persist_sessions = persist_sessions
        self._persist_session = persist_session
        self._html_process_threshold_chars = int(html_process_threshold_chars)
        self._html_process_pool = html_process_pool
        self._html_render_tail_chars = int(html_render_tail_chars)
        self._summary_prepare_threshold_chars = int(summary_prepare_threshold_chars)
        self._summary_tail_chars = int(summary_tail_chars)
        self._summary_wait_for_html_s = float(summary_wait_for_html_s)
        self._summary_timeout_s = float(summary_timeout_s)
        self._summarize_fn = summarize_fn
        self._summarize_fn_getter = summarize_fn_getter
        self._ansi_to_html_fn = ansi_to_html_fn
        self._ansi_to_html_fn_getter = ansi_to_html_fn_getter
        self._make_html_file_fn = make_html_file_fn
        self._make_html_file_fn_getter = make_html_file_fn_getter

    def _resolve_summarize_fn(self):
        if self._summarize_fn_getter is not None:
            fn = self._summarize_fn_getter()
            if fn is not None:
                return fn
        return self._summarize_fn

    def _resolve_ansi_to_html_fn(self):
        if self._ansi_to_html_fn_getter is not None:
            fn = self._ansi_to_html_fn_getter()
            if fn is not None:
                return fn
        return self._ansi_to_html_fn

    def _resolve_make_html_file_fn(self):
        if self._make_html_file_fn_getter is not None:
            fn = self._make_html_file_fn_getter()
            if fn is not None:
                return fn
        return self._make_html_file_fn

    def _persist_for_session(self, session) -> None:
        callback = self._persist_session
        if callable(callback):
            chat_id = getattr(session, "chat_id", None)
            session_id = str(getattr(session, "id", "") or "").strip()
            if session_id:
                try:
                    if chat_id is not None and bool(callback(int(chat_id), session_id)):
                        return
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
        self._persist_sessions()

    @staticmethod
    def _telegram_reply_kwargs(dest: dict, session=None) -> dict:
        kwargs: dict = {}
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
        return kwargs

    @classmethod
    def _resolve_notification_scope(cls, dest: dict, session=None) -> Optional[ConversationScope]:
        if str((dest or {}).get("kind") or "telegram").strip() != "telegram":
            return None
        reply_kwargs = cls._telegram_reply_kwargs(dest or {}, session=session)
        chat_id = reply_kwargs.get("chat_id")
        if chat_id is None:
            return None
        return ConversationScope.from_parts(chat_id, reply_kwargs.get("message_thread_id"))

    async def send_output(
        self,
        session,
        dest: dict,
        output: str,
        context,
        *,
        send_header: bool = True,
        header_override: Optional[str] = None,
        force_html: bool = False,
        send_summary: bool = True,
    ) -> None:
        output = output or ""
        _so_log = logging.getLogger("bot.send_output")
        if not output.strip():
            reason = "empty output"
            try:
                self.bot_app._last_delivery_error = reason
            except Exception:
                _so_log.debug(
                    "legacy_fallback: failed to store last delivery error session=%s reason=%s",
                    getattr(session, "id", "?"),
                    reason,
                    exc_info=True,
                )
            _so_log.warning("[send_output] refused to send: %s session=%s", reason, session.id)
            return

        queue_service = getattr(self.bot_app, "notification_queue_service", None)
        scope = self._resolve_notification_scope(dest, session=session)
        if (
            scope is not None
            and queue_service is not None
            and not queue_service.is_executing_scope(scope)
        ):
            await queue_service.enqueue(
                scope,
                operation="send_output",
                factory=lambda: self._send_output_now(
                    session,
                    dest,
                    output,
                    context,
                    send_header=send_header,
                    header_override=header_override,
                    force_html=force_html,
                    send_summary=send_summary,
                ),
            )
            return
        await self._send_output_now(
            session,
            dest,
            output,
            context,
            send_header=send_header,
            header_override=header_override,
            force_html=force_html,
            send_summary=send_summary,
        )

    async def _send_output_now(
        self,
        session,
        dest: dict,
        output: str,
        context,
        *,
        send_header: bool = True,
        header_override: Optional[str] = None,
        force_html: bool = False,
        send_summary: bool = True,
    ) -> None:
        output = output or ""
        _so_log = logging.getLogger("bot.send_output")
        _so_log.info("[send_output] start session=%s output_len=%d", session.id, len(output))
        async with session.send_lock:
            chat_id = dest.get("chat_id")
            reply_kwargs = self._telegram_reply_kwargs(dest, session=session)
            self.bot_app.metrics.observe_output(len(output))

            if not force_html and chat_id is not None and is_rich_markdown_eligible(output):
                await self.bot_app._send_message(context, text=output, **reply_kwargs)
                try:
                    session.state_summary = build_preview(strip_ansi(output), self.bot_app.config.defaults.summary_max_chars)
                    session.state_updated_at = time.time()
                    self._persist_for_session(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                return

            if send_header:
                try:
                    header_lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
                except Exception:
                    header_lang = "ru"
                delivery_tail = (
                    t("run.output_delivery_with_summary", header_lang, n=self._html_render_tail_chars)
                    if send_summary
                    else t("run.output_delivery_only", header_lang, n=self._html_render_tail_chars)
                )
                resume_txt = (
                    t("session_status.yes", header_lang) if session.resume_token else t("session_status.no", header_lang)
                )
                header = header_override or t(
                    "run.output_header",
                    header_lang,
                    sid=session.id,
                    name=session.name or session.tool.name,
                    tool=session.tool.name,
                    workdir=session.workdir,
                    length=len(output),
                    queue=len(session.queue),
                    resume=resume_txt,
                    delivery=delivery_tail,
                )
                if chat_id is not None:
                    await self.bot_app._send_message(context, text=header, **reply_kwargs)

            async def _render_html_to_file() -> str:
                _so_log.info("[send_output] generating HTML (in thread)...")
                render_src = output[-self._html_render_tail_chars:] if len(output) > self._html_render_tail_chars else output
                if len(render_src) != len(output):
                    _so_log.info(
                        "[send_output] HTML: truncating output for render (orig_len=%d -> render_len=%d)",
                        len(output),
                        len(render_src),
                    )
                loop = asyncio.get_running_loop()
                t0 = time.time()
                ansi_to_html_fn = self._resolve_ansi_to_html_fn()
                if len(render_src) >= self._html_process_threshold_chars:
                    _so_log.info("[send_output] HTML: using process pool (len=%d)", len(render_src))
                    html_text_local = await loop.run_in_executor(self._html_process_pool, ansi_to_html_fn, render_src)
                else:
                    html_text_local = await asyncio.to_thread(ansi_to_html_fn, render_src)
                _so_log.info("[send_output] HTML: conversion done in %.2fs", time.time() - t0)
                make_html_file_fn = self._resolve_make_html_file_fn()
                return await asyncio.to_thread(
                    make_html_file_fn,
                    html_text_local,
                    self.bot_app.config.defaults.html_filename_prefix,
                )

            async def _summarize() -> tuple[Optional[str], Optional[str]]:
                lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
                try:
                    text_for_summary = output[-self._summary_tail_chars:] if len(output) > self._summary_tail_chars else output
                    summarize_fn = self._resolve_summarize_fn()
                    s, err = await asyncio.wait_for(
                        summarize_fn(
                            text_for_summary,
                            config=self.bot_app.config,
                            language=lang,
                        ),
                        timeout=self._summary_timeout_s,
                    )
                    return s, err
                except asyncio.TimeoutError:
                    _so_log.warning("[send_output] summarize timed out after %ss", self._summary_timeout_s)
                    return None, t("run.summary_error_timeout", lang, sec=int(self._summary_timeout_s))
                except Exception:
                    _so_log.exception("[send_output] summarize exception")
                    return None, t("run.summary_error_unknown", lang)

            html_task = asyncio.create_task(_render_html_to_file())
            summary_send_task = None
            html_sent = None
            if send_summary:
                summary_task = asyncio.create_task(_summarize())
                html_sent = asyncio.Event()

                async def _send_summary_when_ready() -> None:
                    lang = resolve_user_lang(self.bot_app.config, chat_id=chat_id)
                    summary, summary_error = await summary_task
                    try:
                        text_for_preview = output[-self._summary_tail_chars:] if len(output) > self._summary_tail_chars else output
                        preview = summary or build_preview(strip_ansi(text_for_preview), self.bot_app.config.defaults.summary_max_chars)
                    except Exception:
                        preview = summary or ""
                    if not chat_id or not preview:
                        return

                    if not html_sent.is_set():
                        try:
                            await asyncio.wait_for(html_sent.wait(), timeout=self._summary_wait_for_html_s)
                        except asyncio.TimeoutError:
                            pass

                    if summary:
                        await self.bot_app._send_message(context, text=preview, md2=True, **reply_kwargs)
                        return

                    suffix = t("run.summary_unavailable", lang, err=summary_error) if summary_error else ""
                    if not html_sent.is_set():
                        suffix = (suffix + t("run.html_pending", lang)).strip()
                    await self.bot_app._send_message(
                        context,
                        text=f"{preview}\n\n{suffix}".strip(),
                        md2=True,
                        **reply_kwargs,
                    )

                summary_send_task = asyncio.create_task(_send_summary_when_ready())

            path = await html_task
            _so_log.info("[send_output] HTML ready, sending document...")
            try:
                if chat_id is not None:
                    with open(path, "rb") as f:
                        ok = await self.bot_app._send_document(context, document=f, **reply_kwargs)
                    if not ok:
                        reason = getattr(self.bot_app, "_last_delivery_error", None) or "unknown"
                        _so_log.warning("[send_output] failed to send document: %s", reason)
            finally:
                try:
                    os.remove(path)
                except Exception:
                    _so_log.debug(
                        "best_effort_cleanup: failed to remove generated html file session=%s path=%s",
                        getattr(session, "id", "?"),
                        path,
                        exc_info=True,
                    )
            if html_sent is not None:
                html_sent.set()

            if summary_send_task is not None:
                try:
                    await summary_send_task
                except Exception:
                    _so_log.exception("[send_output] summary send task failed")

            _so_log.info("[send_output] updating state...")
            try:
                text_for_preview = output[-self._summary_tail_chars:] if len(output) > self._summary_tail_chars else output
                state_preview = build_preview(strip_ansi(text_for_preview), self.bot_app.config.defaults.summary_max_chars)
                session.state_summary = state_preview
                session.state_updated_at = time.time()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
            self._persist_for_session(session)
            _so_log.info("[send_output] done session=%s", session.id)
