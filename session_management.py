"""
Module containing session management functionality for the Telegram bot.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from session import Session
from summary import summarize_text_with_reason
from state import load_active_state
from utils import (
    ansi_to_html,
    build_preview,
    is_within_root,
    make_html_file,
    strip_ansi,
)
from agent.manager import needs_failed_resume_choice, needs_resume_choice


@dataclass
class PendingInput:
    session_id: str
    text: str
    dest: dict
    image_path: Optional[str] = None


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
        self._html_render_tail_chars = 50_000
        self._summary_prepare_threshold_chars = 50_000
        self._summary_tail_chars = 50_000
        self._summary_wait_for_html_s = 5.0
        self._summary_timeout_s = 100.0

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
    ) -> None:
        _so_log = logging.getLogger("bot.send_output")
        _so_log.info("[send_output] start session=%s output_len=%d", session.id, len(output))
        # Serialize output sending per session to avoid interleaving when we pipeline CLI execution.
        async with session.send_lock:
            chat_id = dest.get("chat_id")
            self.bot_app.metrics.observe_output(len(output))

            # Fast path for small outputs: just send text (unless forced to render HTML).
            if not force_html and chat_id is not None and len(output) <= 3900:
                await self.bot_app._send_message(context, chat_id=chat_id, text=output)
                try:
                    session.state_summary = build_preview(strip_ansi(output), self.bot_app.config.defaults.summary_max_chars)
                    session.state_updated_at = time.time()
                    self.bot_app.manager._persist_sessions()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                return

            if send_header:
                header = header_override or (
                    f"[{session.id}|{session.name or session.tool.name}] "
                    f"Сессия: {session.id} | Инструмент: {session.tool.name}\n"
                    f"Каталог: {session.workdir}\n"
                    f"Длина вывода: {len(output)} символов | Очередь: {len(session.queue)}\n"
                    f"Resume: {'есть' if session.resume_token else 'нет'}\n"
                    f"Сначала отправлю вывод во вложении (HTML, последние {self._html_render_tail_chars} символов), затем пришлю summary."
                )
                if chat_id is not None:
                    await self.bot_app._send_message(context, chat_id=chat_id, text=header)

            async def _render_html_to_file() -> str:
                # Keep the log prefix stable for existing log parsing, but note that for big outputs
                # we may switch to a process pool (see below).
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
                if len(render_src) >= self._html_process_threshold_chars:
                    _so_log.info("[send_output] HTML: using process pool (len=%d)", len(render_src))
                    html_text_local = await loop.run_in_executor(self._html_process_pool, ansi_to_html, render_src)
                else:
                    html_text_local = await asyncio.to_thread(ansi_to_html, render_src)
                _so_log.info("[send_output] HTML: conversion done in %.2fs", time.time() - t0)
                return await asyncio.to_thread(make_html_file, html_text_local, self.bot_app.config.defaults.html_filename_prefix)

            async def _summarize() -> tuple[Optional[str], Optional[str]]:
                try:
                    # Limit input size for summary: only the tail matters most for CLI sessions.
                    # This also reduces CPU work during normalization and avoids polling stalls.
                    text_for_summary = output[-self._summary_tail_chars:] if len(output) > self._summary_tail_chars else output
                    s, err = await asyncio.wait_for(
                        summarize_text_with_reason(text_for_summary, config=self.bot_app.config),
                        timeout=self._summary_timeout_s,
                    )
                    return s, err
                except asyncio.TimeoutError:
                    _so_log.warning("[send_output] summarize timed out after %ss", self._summary_timeout_s)
                    return None, f"таймаут суммаризации ({int(self._summary_timeout_s)}с)"
                except Exception:
                    _so_log.exception("[send_output] summarize exception")
                    return None, "неизвестная ошибка"

            # Start both heavy computations in parallel.
            html_task = asyncio.create_task(_render_html_to_file())
            summary_task = asyncio.create_task(_summarize())
            html_sent = asyncio.Event()

            async def _send_summary_when_ready() -> None:
                summary, summary_error = await summary_task
                # Fallback preview should still be sent even if summary timed out / HTML is slow.
                try:
                    text_for_preview = output[-self._summary_tail_chars:] if len(output) > self._summary_tail_chars else output
                    preview = summary or build_preview(strip_ansi(text_for_preview), self.bot_app.config.defaults.summary_max_chars)
                except Exception:
                    preview = summary or ""
                if not chat_id or not preview:
                    return

                # Prefer HTML-first, but never "send nothing": wait briefly for HTML, then send anyway.
                if not html_sent.is_set():
                    try:
                        await asyncio.wait_for(html_sent.wait(), timeout=self._summary_wait_for_html_s)
                    except asyncio.TimeoutError:
                        pass

                if summary:
                    await self.bot_app._send_message(context, chat_id=chat_id, text=preview, md2=True)
                    return

                suffix = f" (summary недоступна: {summary_error})" if summary_error else ""
                if not html_sent.is_set():
                    # Make it explicit why HTML might still be missing.
                    suffix = (suffix + "\nHTML ещё готовится.").strip()
                await self.bot_app._send_message(
                    context,
                    chat_id=chat_id,
                    text=f"{preview}\n\n{suffix}".strip(),
                    md2=True,
                )

            summary_send_task = asyncio.create_task(_send_summary_when_ready())

            # 1) Full output first (HTML attachment)
            path = await html_task
            _so_log.info("[send_output] HTML ready, sending document...")
            try:
                if chat_id is not None:
                    with open(path, "rb") as f:
                        ok = await self.bot_app._send_document(context, chat_id=chat_id, document=f)
                    if not ok:
                        _so_log.error("[send_output] failed to send document")
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass
            html_sent.set()

            # 2) Summary may already be sent (or in-flight). Ensure completion so state is consistent.
            try:
                await summary_send_task
            except Exception:
                _so_log.exception("[send_output] summary send task failed")

            _so_log.info("[send_output] updating state...")
            try:
                # Store whatever we managed to send as a session preview, if available.
                # Prefer summary; else use local preview of the tail.
                text_for_preview = output[-self._summary_tail_chars:] if len(output) > self._summary_tail_chars else output
                state_preview = build_preview(strip_ansi(text_for_preview), self.bot_app.config.defaults.summary_max_chars)
                session.state_summary = state_preview
                session.state_updated_at = time.time()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
            try:
                self.bot_app.manager._persist_sessions()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
            _so_log.info("[send_output] done session=%s", session.id)

    async def run_prompt(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        _rp_log = logging.getLogger("bot.run_prompt")
        _rp_log.info("[run_prompt] acquiring run_lock session=%s prompt=%r", session.id, prompt[:100])
        async with session.run_lock:
            _rp_log.info("[run_prompt] lock acquired session=%s", session.id)
            session.busy = True
            session.started_at = time.time()
            session.last_output_ts = session.started_at
            session.last_tick_ts = None
            session.last_tick_value = None
            session.tick_seen = 0
            image_path = dest.get("image_path")
            try:
                _rp_log.info("[run_prompt] calling session.run_prompt session=%s", session.id)
                output = await session.run_prompt(prompt, image_path=image_path)
                _rp_log.info("[run_prompt] session.run_prompt returned session=%s output_len=%d", session.id, len(output))
                # Don't block further CLI execution on slow HTML generation/upload/summarization.
                task = asyncio.create_task(self.send_output(session, dest, output, context))

                def _cb(t: asyncio.Task) -> None:
                    try:
                        t.result()
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        logging.getLogger("bot.send_output").exception("[send_output] task failed: %s", e)

                task.add_done_callback(_cb)
                forced = getattr(session, "headless_forced_stop", None)
                if forced:
                    chat_id = dest.get("chat_id")
                    details = f"{session.id} ({session.name or session.tool.name}) @ {session.workdir}"
                    msg = f"CLI для сессии {details} завершен не штатно."
                    if chat_id is not None:
                        await self.bot_app._send_message(context, chat_id=chat_id, text=msg)
                    session.headless_forced_stop = None
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка выполнения: {e}")
            finally:
                session.busy = False
                if image_path and dest.get("cleanup_image"):
                    try:
                        os.remove(image_path)
                    except Exception:
                        pass
                if session.queue:
                    next_item = session.queue.popleft()
                    if isinstance(next_item, str):
                        next_prompt = next_item
                        next_dest = {"kind": "telegram", "chat_id": dest.get("chat_id")}
                    else:
                        next_prompt = next_item.get("text", "")
                        next_dest = next_item.get("dest") or {"kind": "telegram"}
                        image_path = next_item.get("image_path")
                        if image_path:
                            next_dest["image_path"] = image_path
                            next_dest["cleanup_image"] = True
                        if next_dest.get("kind") == "telegram" and next_dest.get("chat_id") is None:
                            next_dest["chat_id"] = dest.get("chat_id")
                    try:
                        self.bot_app.manager._persist_sessions()
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                    asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

    async def run_agent(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        _ra_log = logging.getLogger("bot.run_agent")
        _ra_log.info("[run_agent] acquiring run_lock session=%s prompt=%r", session.id, prompt[:100])
        async with session.run_lock:
            _ra_log.info("[run_agent] lock acquired session=%s", session.id)
            session.busy = True
            session.started_at = time.time()
            session.last_output_ts = session.started_at
            session.last_tick_ts = None
            session.last_tick_value = None
            session.tick_seen = 0
            try:
                _ra_log.info("[run_agent] calling agent.run session=%s", session.id)
                output = await self.bot_app.agent.run(session, prompt, self.bot_app, context, dest)
                _ra_log.info("[run_agent] agent.run returned session=%s output_len=%d", session.id, len(output or ""))
                now = time.time()
                session.last_output_ts = now
                session.last_tick_ts = now
                session.tick_seen = (session.tick_seen or 0) + 1
                # Success output of the orchestrator is not user-facing:
                # a dedicated orchestrator step must format and send the final answer (e.g. via send_output()).
                try:
                    preview = build_preview(strip_ansi(output), self.bot_app.config.defaults.summary_max_chars)
                    session.state_summary = preview
                    session.state_updated_at = time.time()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                try:
                    self.bot_app.manager._persist_sessions()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
            except asyncio.CancelledError:
                _ra_log.warning("[run_agent] CancelledError session=%s", session.id)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self.bot_app._send_message(context, chat_id=chat_id, text="Агент прерван.")
                raise
            except Exception as e:
                _ra_log.exception("[run_agent] exception session=%s: %s", session.id, e)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка агента: {e}")
            finally:
                _ra_log.info("[run_agent] finally session=%s busy->False", session.id)
                session.busy = False
                if session.queue:
                    next_item = session.queue.popleft()
                    if isinstance(next_item, str):
                        next_prompt = next_item
                        next_dest = {"kind": "telegram", "chat_id": dest.get("chat_id")}
                    else:
                        next_prompt = next_item.get("text", "")
                        next_dest = next_item.get("dest") or {"kind": "telegram"}
                        if next_dest.get("kind") == "telegram" and next_dest.get("chat_id") is None:
                            next_dest["chat_id"] = dest.get("chat_id")
                    try:
                        self.bot_app.manager._persist_sessions()
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                    if getattr(session, "manager_enabled", False):
                        self.bot_app._start_manager_task(session, next_prompt, next_dest, context)
                    elif session.agent_enabled:
                        self.bot_app._start_agent_task(session, next_prompt, next_dest, context)
                    else:
                        asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

    async def run_manager(
        self,
        session: Session,
        prompt: str,
        dest: dict,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        _rm_log = logging.getLogger("bot.run_manager")
        _rm_log.info("[run_manager] acquiring run_lock session=%s prompt=%r", session.id, prompt[:100])
        # If there's an active plan and auto-resume is disabled, ask user what to do before starting long work.
        if dest.get("kind") == "telegram":
            chat_id = dest.get("chat_id")
            if chat_id is not None:
                try:
                    from agent.manager_store import load_plan

                    plan = load_plan(session.workdir)
                except Exception:
                    plan = None
                if needs_resume_choice(plan, auto_resume=bool(self.bot_app.config.defaults.manager_auto_resume), user_text=prompt):
                    self.bot_app.manager_resume_pending[session.id] = {"prompt": prompt, "dest": dict(dest)}
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("▶️ Продолжить текущий план", callback_data="manager_resume:continue"),
                            ],
                            [
                                InlineKeyboardButton("🆕 Начать новый план", callback_data="manager_resume:new"),
                            ],
                            [InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")],
                        ]
                    )
                    await self.bot_app._send_message(
                        context,
                        chat_id=chat_id,
                        text="Найден активный план Manager. Продолжить его или начать новый (старый будет заархивирован)?",
                        reply_markup=keyboard,
                    )
                    return
                if needs_failed_resume_choice(plan, auto_resume=bool(self.bot_app.config.defaults.manager_auto_resume), user_text=prompt):
                    self.bot_app.manager_resume_pending[session.id] = {"prompt": prompt, "dest": dict(dest)}
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("▶️ Продолжить остановленный план", callback_data="manager_resume:continue"),
                            ],
                            [
                                InlineKeyboardButton("🆕 Начать новый план", callback_data="manager_resume:new"),
                            ],
                            [InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel")],
                        ]
                    )
                    await self.bot_app._send_message(
                        context,
                        chat_id=chat_id,
                        text="Найден остановленный план Manager с доступными повторами. Продолжить его или начать новый (старый будет заархивирован)?",
                        reply_markup=keyboard,
                    )
                    return
        async with session.run_lock:
            _rm_log.info("[run_manager] lock acquired session=%s", session.id)
            session.busy = True
            session.started_at = time.time()
            session.last_output_ts = session.started_at
            session.last_tick_ts = None
            session.last_tick_value = None
            session.tick_seen = 0
            try:
                _rm_log.info("[run_manager] calling manager_orchestrator.run session=%s", session.id)
                output = await self.bot_app.manager_orchestrator.run(session, prompt, self.bot_app, context, dest)
                _rm_log.info("[run_manager] manager_orchestrator.run returned session=%s output_len=%d", session.id, len(output or ""))
                now = time.time()
                session.last_output_ts = now
                session.last_tick_ts = now
                session.tick_seen = (session.tick_seen or 0) + 1
                try:
                    preview = build_preview(strip_ansi(output), self.bot_app.config.defaults.summary_max_chars)
                    session.state_summary = preview
                    session.state_updated_at = time.time()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                try:
                    self.bot_app.manager._persist_sessions()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
            except asyncio.CancelledError:
                _rm_log.warning("[run_manager] CancelledError session=%s", session.id)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self.bot_app._send_message(context, chat_id=chat_id, text="Менеджер прерван.")
                raise
            except Exception as e:
                _rm_log.exception("[run_manager] exception session=%s: %s", session.id, e)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка менеджера: {e}")
            finally:
                _rm_log.info("[run_manager] finally session=%s busy->False", session.id)
                session.busy = False
                if session.queue:
                    next_item = session.queue.popleft()
                    if isinstance(next_item, str):
                        next_prompt = next_item
                        next_dest = {"kind": "telegram", "chat_id": dest.get("chat_id")}
                    else:
                        next_prompt = next_item.get("text", "")
                        next_dest = next_item.get("dest") or {"kind": "telegram"}
                        if next_dest.get("kind") == "telegram" and next_dest.get("chat_id") is None:
                            next_dest["chat_id"] = dest.get("chat_id")
                    try:
                        self.bot_app.manager._persist_sessions()
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                    if getattr(session, "manager_enabled", False):
                        self.bot_app._start_manager_task(session, next_prompt, next_dest, context)
                    elif session.agent_enabled:
                        self.bot_app._start_agent_task(session, next_prompt, next_dest, context)
                    else:
                        asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

    def _clear_agent_session_cache(self, session_id: str) -> None:
        try:
            self.bot_app.agent.clear_session_cache(session_id)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")

    def _set_agent_project_root(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        project_root: Optional[str],
    ) -> tuple[bool, str]:
        if project_root:
            root = self.bot_app.config.defaults.workdir
            if not is_within_root(project_root, root):
                return False, "Нельзя выйти за пределы корневого каталога."
            if not os.path.isdir(project_root):
                return False, "Каталог не существует."
            project_root = os.path.realpath(project_root)
        session.project_root = project_root
        self.bot_app._interrupt_before_close(session.id, chat_id, context)
        self.bot_app._clear_agent_session_cache(session.id)
        try:
            self.bot_app.manager._persist_sessions()
        except Exception:
            pass
        if project_root:
            return True, f"Проект подключен: {project_root}"
        return True, "Проект отключен."

    def _interrupt_before_close(self, session_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.bot_app.manager.get(session_id)
        if not session:
            return
        session.interrupt()
        task = self.bot_app.agent_tasks.get(session_id)
        if task and not task.done():
            task.cancel()

    async def ensure_active_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]:
        session = self.bot_app.manager.active()
        if not session:
            if not self.bot_app.restore_offered.get(chat_id, False):
                self.bot_app.restore_offered[chat_id] = True
                active = load_active_state(self.bot_app.config.defaults.state_path)
                if active and active.tool in self.bot_app.config.tools and os.path.isdir(active.workdir):
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("✅ Восстановить", callback_data="restore_yes"),
                                InlineKeyboardButton("❌ Нет", callback_data="restore_no"),
                            ]
                        ]
                    )
                    await self.bot_app._send_message(
                        context,
                        chat_id=chat_id,
                        text=(
                            f"Найдена активная сессия: {active.tool} @ {active.workdir}. "
                            "Восстановить?"
                        ),
                        reply_markup=keyboard,
                    )
                    return None
            await self.bot_app._send_message(context,
                                             chat_id=chat_id,
                                             text="Нет активной сессии. Используйте /tools и /new <tool> <path>.",
                                             )
            return None
        return session

    def _start_agent_task(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        existing = self.bot_app.agent_tasks.get(session.id)
        if existing and not existing.done():
            # Session already has a running agent task; don't start a duplicate.
            return
        task = asyncio.create_task(self.run_agent(session, prompt, dest, context))
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            self.bot_app.agent_tasks[session.id] = task

            def _cleanup(_task: asyncio.Task, sid: str = session.id) -> None:
                current = self.bot_app.agent_tasks.get(sid)
                if current is _task:
                    self.bot_app.agent_tasks.pop(sid, None)

            task.add_done_callback(_cleanup)

    def _start_manager_task(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        existing = self.bot_app.manager_tasks.get(session.id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self.run_manager(session, prompt, dest, context))
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            self.bot_app.manager_tasks[session.id] = task

            def _cleanup(_task: asyncio.Task, sid: str = session.id) -> None:
                current = self.bot_app.manager_tasks.get(sid)
                if current is _task:
                    self.bot_app.manager_tasks.pop(sid, None)

            task.add_done_callback(_cleanup)
