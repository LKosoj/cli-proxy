import asyncio
import html
import logging
import os
import shutil
import time
import re
import concurrent.futures
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import AppConfig, ToolConfig, load_config
from dotenv_loader import load_dotenv_near
from session import Session, SessionManager, run_tool_help
from summary import summarize_text_with_reason
from command_registry import build_command_registry
from dirs_ui import build_dirs_keyboard, prepare_dirs
from session_ui import SessionUI
from git_ops import GitOps
from metrics import Metrics
from mcp_bridge import MCPBridge
from state import get_state, load_active_state, clear_active_state
from toolhelp import get_toolhelp, update_toolhelp
from utils import (
    ansi_to_html,
    build_preview,
    has_ansi,
    is_within_root,
    make_html_file,
    sandbox_root,
    sandbox_session_dir,
    sandbox_shared_dir,
    strip_ansi,
)
from tg_markdown import to_markdown_v2
from agent import execute_shell_command, pop_pending_command, set_approval_callback
from agent.orchestrator import OrchestratorRunner
from agent.manager import ManagerOrchestrator
from agent.manager import MANAGER_CONTINUE_TOKEN, format_manager_status, needs_resume_choice
from agent.plugins.task_management import run_task_deadline_checker
from agent.tooling.registry import get_tool_registry


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

# HTML rendering of large ANSI logs is CPU-heavy and often pure-Python.
# Running it in a thread can starve the event loop due to the GIL, which looks like "polling freeze".
# For large outputs we offload conversion to a separate process.
_HTML_PROCESS_THRESHOLD_CHARS = 100_000
_HTML_PROCESS_POOL = concurrent.futures.ProcessPoolExecutor(max_workers=1)
_HTML_RENDER_TAIL_CHARS = 10_000
_SUMMARY_PREPARE_THRESHOLD_CHARS = 20_000
_SUMMARY_TAIL_CHARS = 50_000
_SUMMARY_WAIT_FOR_HTML_S = 5.0
_SUMMARY_TIMEOUT_S = 100.0


@dataclass
class PendingInput:
    session_id: str
    text: str
    dest: dict
    image_path: Optional[str] = None


class BotApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self._setup_logging()
        self._configure_agent_sandbox()
        self.manager = SessionManager(config)
        self.manager.on_session_change = self._on_session_change
        self.metrics = Metrics()
        self.pending: Dict[int, PendingInput] = {}
        self.state_menu: Dict[int, list] = {}
        self.use_menu: Dict[int, list] = {}
        self.close_menu: Dict[int, list] = {}
        self.pending_new_tool: Dict[int, str] = {}
        self.dirs_menu: Dict[int, list] = {}
        self.state_menu_page: Dict[int, int] = {}
        self.dirs_base: Dict[int, str] = {}
        self.dirs_page: Dict[int, int] = {}
        self.dirs_root: Dict[int, str] = {}
        self.dirs_mode: Dict[int, str] = {}
        self.pending_dir_input: Dict[int, bool] = {}
        self.pending_dir_create: Dict[int, str] = {}
        self.pending_git_clone: Dict[int, str] = {}
        self.pending_agent_project: Dict[int, str] = {}
        # (removed: agent_plugin_commands cache -- replaced by two-level plugin menu)
        self.toolhelp_menu: Dict[int, list] = {}
        self.restore_offered: Dict[int, bool] = {}
        self.files_menu: Dict[int, list] = {}
        self.files_dir: Dict[int, str] = {}
        self.files_page: Dict[int, int] = {}
        self.files_entries: Dict[int, list] = {}
        self.files_pending_delete: Dict[int, str] = {}
        self.message_buffer: Dict[int, list[str]] = {}
        self.buffer_tasks: Dict[int, asyncio.Task] = {}
        self.pending_questions: Dict[str, Dict[str, object]] = {}
        self.context_by_chat: Dict[int, ContextTypes.DEFAULT_TYPE] = {}
        # Agent task is scoped per session, not per chat.
        # Multiple sessions may exist in the same chat; interrupt/close must only affect its own session.
        self.agent_tasks: Dict[str, asyncio.Task] = {}
        self.manager_tasks: Dict[str, asyncio.Task] = {}
        # Pending "continue or start new" decision when manager_auto_resume=false and a plan is active.
        self.manager_resume_pending: Dict[str, Dict[str, Any]] = {}
        self.session_ui = SessionUI(
            self.config,
            self.manager,
            self._send_message,
            self._format_ts,
            self._short_label,
            self._clear_agent_session_cache,
            self._interrupt_before_close,
        )
        self.agent = OrchestratorRunner(self.config)
        self.manager_orchestrator = ManagerOrchestrator(self.config)
        set_approval_callback(self._request_command_approval)
        self.git = GitOps(
            self.config,
            self.manager,
            self._send_message,
            self._send_document,
            self._short_label,
            self._handle_cli_input,
        )
        self.mcp = MCPBridge(self.config, self)
        self._task_deadline_checker_task: Optional[asyncio.Task] = None

    def _configure_agent_sandbox(self) -> None:
        root = sandbox_root(self.config.defaults.workdir)
        shared = sandbox_shared_dir(self.config.defaults.workdir)
        chats = os.path.join(shared, "chats")
        sessions = os.path.join(root, "sessions")
        os.makedirs(root, exist_ok=True)
        os.makedirs(shared, exist_ok=True)
        os.makedirs(chats, exist_ok=True)
        os.makedirs(sessions, exist_ok=True)
        os.environ["AGENT_SANDBOX_ROOT"] = root

    def _agent_sandbox_root(self) -> str:
        return sandbox_root(self.config.defaults.workdir)

    def _agent_service_entries(self) -> set[str]:
        return {"_shared", "SESSION.json", "MEMORY.md"}

    def _clear_agent_sandbox(self) -> tuple[int, int]:
        root = self._agent_sandbox_root()
        if not os.path.isdir(root):
            return 0, 0
        removed = 0
        errors = 0
        for name in os.listdir(root):
            if name in self._agent_service_entries():
                continue
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                removed += 1
            except Exception:
                errors += 1
        return removed, errors

    def _clear_agent_session_files(self, session_id: str) -> bool:
        root = self._agent_sandbox_root()
        session_dir = sandbox_session_dir(self.config.defaults.workdir, session_id)
        try:
            real_root = os.path.realpath(root)
            real_target = os.path.realpath(session_dir)
            if not real_target.startswith(real_root + os.sep):
                return False
            if os.path.isdir(real_target):
                shutil.rmtree(real_target)
                return True
            if os.path.exists(real_target):
                os.remove(real_target)
                return True
            return True
        except Exception:
            return False

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.config.telegram.whitelist_chat_ids

    def _plugin_awaiting_input(self, chat_id: int) -> bool:
        """Check if any plugin is waiting for free-text input from the user."""
        registry = getattr(self, "_tool_registry", None)
        if registry is None:
            return False
        try:
            return registry.any_awaiting_input(chat_id)
        except Exception:
            return False

    def _cancel_plugin_dialogs(self, chat_id: int) -> None:
        """Cancel all pending plugin dialogs for the given chat."""
        registry = getattr(self, "_tool_registry", None)
        if registry:
            try:
                registry.cancel_all_inputs(chat_id)
            except Exception:
                pass

    def _on_session_change(self) -> None:
        """Called by SessionManager when the active session changes (create/switch/close).

        Cancels any active plugin dialogs for all whitelisted chats so that
        stale dialogs never block message processing after session transitions.
        """
        for chat_id in self.config.telegram.whitelist_chat_ids:
            self._cancel_plugin_dialogs(chat_id)

    def _setup_logging(self) -> None:
        import datetime as _dt
        import sys
        import threading
        from logging.handlers import TimedRotatingFileHandler

        log_path = self.config.defaults.log_path
        log_dir = os.path.dirname(log_path)
        log_base = os.path.basename(log_path)
        base_root, base_ext = os.path.splitext(log_base)
        if base_root:
            error_log_name = f"{base_root}_error{base_ext or '.log'}"
        else:
            error_log_name = "bot_error.log"
        error_log_path = os.path.join(log_dir, error_log_name)

        if base_root:
            agent_log_name = f"{base_root}_agent{base_ext or '.log'}"
        else:
            agent_log_name = "agent.log"
        agent_log_path = os.path.join(log_dir, agent_log_name)

        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.setLevel(logging.INFO)

        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=1,
            utc=True,
            atTime=_dt.time(3, 0),
            encoding="utf-8",
        )

        def _namer(default_name: str) -> str:
            return f"{log_path}.1"

        def _rotator(source: str, dest: str) -> None:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except Exception:
                pass
            os.replace(source, dest)

        handler.namer = _namer
        handler.rotator = _rotator
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(handler)

        error_handler = TimedRotatingFileHandler(
            error_log_path,
            when="midnight",
            interval=1,
            backupCount=1,
            utc=True,
            atTime=_dt.time(3, 0),
            encoding="utf-8",
        )

        def _error_namer(default_name: str) -> str:
            return f"{error_log_path}.1"

        def _error_rotator(source: str, dest: str) -> None:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except Exception:
                pass
            os.replace(source, dest)

        error_handler.namer = _error_namer
        error_handler.rotator = _error_rotator
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(error_handler)

        # --- Dedicated agent log file (orchestrator / planner / executor / agent_core) ---
        agent_handler = TimedRotatingFileHandler(
            agent_log_path,
            when="midnight",
            interval=1,
            backupCount=1,
            utc=True,
            atTime=_dt.time(3, 0),
            encoding="utf-8",
        )

        def _agent_namer(default_name: str) -> str:
            return f"{agent_log_path}.1"

        def _agent_rotator(source: str, dest: str) -> None:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except Exception:
                pass
            os.replace(source, dest)

        agent_handler.namer = _agent_namer
        agent_handler.rotator = _agent_rotator
        agent_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        # Attach to the "agent" logger hierarchy so that agent.orchestrator,
        # agent.planner, agent.executor, agent.agent_core all write here.
        agent_logger = logging.getLogger("agent")
        agent_logger.addHandler(agent_handler)
        # Prevent agent messages from also going to root (bot.log) to keep it clean.
        agent_logger.propagate = False

        prev_excepthook = sys.excepthook
        prev_threading_excepthook = threading.excepthook

        def _log_unhandled_exception(exc_type, exc_value, exc_traceback):
            logging.getLogger().error("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
            if prev_excepthook and prev_excepthook is not sys.__excepthook__:
                prev_excepthook(exc_type, exc_value, exc_traceback)

        def _log_thread_exception(args):
            logging.getLogger().error(
                "Unhandled thread exception",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            if prev_threading_excepthook and prev_threading_excepthook is not threading.__excepthook__:
                prev_threading_excepthook(args)

        sys.excepthook = _log_unhandled_exception
        threading.excepthook = _log_thread_exception

    def _format_ts(self, ts: float) -> str:
        import datetime as _dt

        if not ts:
            return "нет"
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _short_label(self, text: str, max_len: int = 40) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _tool_exec(self, tool: ToolConfig) -> Optional[str]:
        for cmd in (tool.cmd, tool.headless_cmd, tool.interactive_cmd):
            if cmd and len(cmd) > 0:
                return cmd[0]
        return None

    def _is_tool_available(self, name: str) -> bool:
        tool = self.config.tools.get(name)
        if not tool:
            return False
        exe = self._tool_exec(tool)
        return bool(exe and shutil.which(exe))

    def _available_tools(self) -> list[str]:
        return [name for name in self.config.tools.keys() if self._is_tool_available(name)]

    def _expected_tools(self) -> str:
        return ", ".join(sorted(self.config.tools.keys()))

    async def _send_message(self, context: ContextTypes.DEFAULT_TYPE, **kwargs):
        for attempt in range(5):
            try:
                # Most bot outputs should be MarkdownV2. Default to md2=True for safety:
                # it escapes special characters so arbitrary text (including exceptions/paths)
                # does not break parsing or message delivery.
                md2 = bool(kwargs.pop("md2", True))
                if md2 and "text" in kwargs and kwargs.get("text") is not None:
                    # Telegram MarkdownV2 requires escaping many characters. Use md2tgmd if available.
                    kwargs["text"] = to_markdown_v2(str(kwargs.get("text")))
                    kwargs.setdefault("parse_mode", "MarkdownV2")
                message = await context.bot.send_message(**kwargs)
                chat_id = kwargs.get("chat_id")
                if chat_id and message:
                    self.agent.record_message(chat_id, message.message_id)
                return message
            except (NetworkError, TimedOut):
                if attempt == 4:
                    print("Ошибка сети при отправке сообщения в Telegram.")
                    return
                await asyncio.sleep(2 * (2 ** attempt))

    async def _send_document(self, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> bool:
        for attempt in range(5):
            try:
                await context.bot.send_document(**kwargs)
                return True
            except (NetworkError, TimedOut) as e:
                if attempt == 4:
                    logging.exception("Ошибка сети при отправке файла в Telegram.")
                    return False
                await asyncio.sleep(2 * (2 ** attempt))
            except Exception:
                logging.exception("Не удалось отправить файл в Telegram.")
                return False
        return False

    async def _send_ask_question(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        session_id: str,
        question_id: str,
        question: str,
        options: list[str],
    ) -> None:
        self.pending_questions[question_id] = {"options": options, "chat_id": chat_id, "session_id": session_id}
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(opt, callback_data=f"ask:{question_id}:{idx}")] for idx, opt in enumerate(options)]
        )
        await self._send_message(context, chat_id=chat_id, text=question, reply_markup=keyboard)

    async def _delete_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> bool:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            return False

    async def _edit_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str, *, md2: bool = True) -> bool:
        try:
            if md2:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=to_markdown_v2(text),
                    parse_mode="MarkdownV2",
                )
            else:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            return True
        except Exception:
            return False

    def _request_command_approval(self, chat_id: int, cmd_id: str, cmd: str, reason: str) -> None:
        context = self.context_by_chat.get(chat_id)
        if not context:
            return

        async def _send() -> None:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_cmd:{cmd_id}"),
                        InlineKeyboardButton("❌ Запретить", callback_data=f"deny_cmd:{cmd_id}"),
                    ]
                ]
            )
            await self._send_message(
                context,
                chat_id=chat_id,
                text=f"Нужное подтверждение: {reason}\nКоманда:\n{cmd}",
                reply_markup=keyboard,
            )

        asyncio.create_task(_send())

    def _build_state_keyboard(self, chat_id: int) -> InlineKeyboardMarkup:
        keys = self.state_menu.get(chat_id, [])
        page = self.state_menu_page.get(chat_id, 0)
        page_size = 10
        start = page * page_size
        end = start + page_size
        rows = []
        for i, k in enumerate(keys[start:end], start=start):
            rows.append([InlineKeyboardButton(self._short_label(k), callback_data=f"state_pick:{i}")])
        nav = []
        if start > 0:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"state_page:{page-1}"))
        if end < len(keys):
            nav.append(InlineKeyboardButton("Далее", callback_data=f"state_page:{page+1}"))
        if nav:
            rows.append(nav)
        return InlineKeyboardMarkup(rows)

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
            self.metrics.observe_output(len(output))

            # Fast path for small outputs: just send text (unless forced to render HTML).
            if not force_html and chat_id is not None and len(output) <= 3900:
                await self._send_message(context, chat_id=chat_id, text=output)
                try:
                    session.state_summary = build_preview(strip_ansi(output), self.config.defaults.summary_max_chars)
                    session.state_updated_at = time.time()
                    self.manager._persist_sessions()
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
                    f"Сначала отправлю вывод во вложении (HTML, последние {_HTML_RENDER_TAIL_CHARS} символов), затем пришлю summary."
                )
                if chat_id is not None:
                    await self._send_message(context, chat_id=chat_id, text=header)

            async def _render_html_to_file() -> str:
                # Keep the log prefix stable for existing log parsing, but note that for big outputs
                # we may switch to a process pool (see below).
                _so_log.info("[send_output] generating HTML (in thread)...")
                render_src = output[-_HTML_RENDER_TAIL_CHARS:] if len(output) > _HTML_RENDER_TAIL_CHARS else output
                if len(render_src) != len(output):
                    _so_log.info(
                        "[send_output] HTML: truncating output for render (orig_len=%d -> render_len=%d)",
                        len(output),
                        len(render_src),
                    )
                loop = asyncio.get_running_loop()
                t0 = time.time()
                if len(render_src) >= _HTML_PROCESS_THRESHOLD_CHARS:
                    _so_log.info("[send_output] HTML: using process pool (len=%d)", len(render_src))
                    html_text_local = await loop.run_in_executor(_HTML_PROCESS_POOL, ansi_to_html, render_src)
                else:
                    html_text_local = await asyncio.to_thread(ansi_to_html, render_src)
                _so_log.info("[send_output] HTML: conversion done in %.2fs", time.time() - t0)
                return await asyncio.to_thread(make_html_file, html_text_local, self.config.defaults.html_filename_prefix)

            async def _summarize() -> tuple[Optional[str], Optional[str]]:
                try:
                    # Limit input size for summary: only the tail matters most for CLI sessions.
                    # This also reduces CPU work during normalization and avoids polling stalls.
                    text_for_summary = output[-_SUMMARY_TAIL_CHARS:] if len(output) > _SUMMARY_TAIL_CHARS else output
                    s, err = await asyncio.wait_for(
                        summarize_text_with_reason(text_for_summary, config=self.config),
                        timeout=_SUMMARY_TIMEOUT_S,
                    )
                    return s, err
                except asyncio.TimeoutError:
                    _so_log.warning("[send_output] summarize timed out after %ss", _SUMMARY_TIMEOUT_S)
                    return None, f"таймаут суммаризации ({int(_SUMMARY_TIMEOUT_S)}с)"
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
                    text_for_preview = output[-_SUMMARY_TAIL_CHARS:] if len(output) > _SUMMARY_TAIL_CHARS else output
                    preview = summary or build_preview(strip_ansi(text_for_preview), self.config.defaults.summary_max_chars)
                except Exception:
                    preview = summary or ""
                if not chat_id or not preview:
                    return

                # Prefer HTML-first, but never "send nothing": wait briefly for HTML, then send anyway.
                if not html_sent.is_set():
                    try:
                        await asyncio.wait_for(html_sent.wait(), timeout=_SUMMARY_WAIT_FOR_HTML_S)
                    except asyncio.TimeoutError:
                        pass

                if summary:
                    await self._send_message(context, chat_id=chat_id, text=preview, md2=True)
                    return

                suffix = f" (summary недоступна: {summary_error})" if summary_error else ""
                if not html_sent.is_set():
                    # Make it explicit why HTML might still be missing.
                    suffix = (suffix + "\nHTML ещё готовится.").strip()
                await self._send_message(
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
                        ok = await self._send_document(context, chat_id=chat_id, document=f)
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
                text_for_preview = output[-_SUMMARY_TAIL_CHARS:] if len(output) > _SUMMARY_TAIL_CHARS else output
                state_preview = build_preview(strip_ansi(text_for_preview), self.config.defaults.summary_max_chars)
                session.state_summary = state_preview
                session.state_updated_at = time.time()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
            try:
                self.manager._persist_sessions()
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
            _so_log.info("[send_output] done session=%s", session.id)

    async def run_prompt(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                        await self._send_message(context, chat_id=chat_id, text=msg)
                    session.headless_forced_stop = None
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self._send_message(context, chat_id=chat_id, text=f"Ошибка выполнения: {e}")
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
                        self.manager._persist_sessions()
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                    asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

    async def run_agent(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                output = await self.agent.run(session, prompt, self, context, dest)
                _ra_log.info("[run_agent] agent.run returned session=%s output_len=%d", session.id, len(output))
                now = time.time()
                session.last_output_ts = now
                session.last_tick_ts = now
                session.tick_seen = (session.tick_seen or 0) + 1
                # Success output of the orchestrator is not user-facing:
                # a dedicated orchestrator step must format and send the final answer (e.g. via send_output()).
                try:
                    preview = build_preview(strip_ansi(output), self.config.defaults.summary_max_chars)
                    session.state_summary = preview
                    session.state_updated_at = time.time()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                try:
                    self.manager._persist_sessions()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
            except asyncio.CancelledError:
                _ra_log.warning("[run_agent] CancelledError session=%s", session.id)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self._send_message(context, chat_id=chat_id, text="Агент прерван.")
                raise
            except Exception as e:
                _ra_log.exception("[run_agent] exception session=%s: %s", session.id, e)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self._send_message(context, chat_id=chat_id, text=f"Ошибка агента: {e}")
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
                        self.manager._persist_sessions()
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                    if session.agent_enabled:
                        self._start_agent_task(session, next_prompt, next_dest, context)
                    else:
                        asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

    async def run_manager(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                if needs_resume_choice(plan, auto_resume=bool(self.config.defaults.manager_auto_resume), user_text=prompt):
                    self.manager_resume_pending[session.id] = {"prompt": prompt, "dest": dict(dest)}
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("▶️ Продолжить текущий план", callback_data="manager_resume:continue"),
                            ],
                            [
                                InlineKeyboardButton("🆕 Начать новый план", callback_data="manager_resume:new"),
                            ],
                            [InlineKeyboardButton("Отмена", callback_data="agent_cancel")],
                        ]
                    )
                    await self._send_message(
                        context,
                        chat_id=chat_id,
                        text="Найден активный план Manager. Продолжить его или начать новый (старый будет заархивирован)?",
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
                output = await self.manager_orchestrator.run(session, prompt, self, context, dest)
                _rm_log.info("[run_manager] manager_orchestrator.run returned session=%s output_len=%d", session.id, len(output or ""))
                try:
                    preview = build_preview(strip_ansi(output or ""), self.config.defaults.summary_max_chars)
                    session.state_summary = preview
                    session.state_updated_at = time.time()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                try:
                    self.manager._persist_sessions()
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
            except asyncio.CancelledError:
                _rm_log.warning("[run_manager] CancelledError session=%s", session.id)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self._send_message(context, chat_id=chat_id, text="Менеджер прерван.")
                raise
            except Exception as e:
                _rm_log.exception("[run_manager] exception session=%s: %s", session.id, e)
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    await self._send_message(context, chat_id=chat_id, text=f"Ошибка менеджера: {e}")
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
                        self.manager._persist_sessions()
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")
                    if getattr(session, "manager_enabled", False):
                        self._start_manager_task(session, next_prompt, next_dest, context)
                    elif session.agent_enabled:
                        self._start_agent_task(session, next_prompt, next_dest, context)
                    else:
                        asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

    def _clear_agent_session_cache(self, session_id: str) -> None:
        try:
            self.agent.clear_session_cache(session_id)
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
            root = self.config.defaults.workdir
            if not is_within_root(project_root, root):
                return False, "Нельзя выйти за пределы корневого каталога."
            if not os.path.isdir(project_root):
                return False, "Каталог не существует."
            project_root = os.path.realpath(project_root)
        session.project_root = project_root
        self._interrupt_before_close(session.id, chat_id, context)
        self._clear_agent_session_cache(session.id)
        try:
            self.manager._persist_sessions()
        except Exception:
            pass
        if project_root:
            return True, f"Проект подключен: {project_root}"
        return True, "Проект отключен."

    def _interrupt_before_close(self, session_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.manager.get(session_id)
        if not session:
            return
        session.interrupt()
        task = self.agent_tasks.get(session_id)
        if task and not task.done():
            task.cancel()

    async def ensure_active_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]:
        session = self.manager.active()
        if not session:
            if not self.restore_offered.get(chat_id, False):
                self.restore_offered[chat_id] = True
                active = load_active_state(self.config.defaults.state_path)
                if active and active.tool in self.config.tools and os.path.isdir(active.workdir):
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("Восстановить", callback_data="restore_yes"),
                                InlineKeyboardButton("Нет", callback_data="restore_no"),
                            ]
                        ]
                    )
                    await self._send_message(context, 
                        chat_id=chat_id,
                        text=(
                            f"Найдена активная сессия: {active.tool} @ {active.workdir}. "
                            "Восстановить?"
                        ),
                        reply_markup=keyboard,
                    )
                    return None
            await self._send_message(context, 
                chat_id=chat_id,
                text="Нет активной сессии. Используйте /tools и /new <tool> <path>.",
            )
            return None
        return session

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        text = update.message.text if update.message else None
        if not self.is_allowed(chat_id):
            return
        self.context_by_chat[chat_id] = context
        self.metrics.inc("messages")
        if self._has_attachments(update.message):
            return
        if await self.session_ui.handle_pending_message(chat_id, text, context):
            return
        if chat_id in self.pending_dir_create:
            base = self.pending_dir_create.pop(chat_id)
            name = text.strip()
            if name in ("-", "отмена", "Отмена"):
                await self._send_message(context, chat_id=chat_id, text="Создание каталога отменено.")
                return
            if not name:
                await self._send_message(context, chat_id=chat_id, text="Имя каталога пустое.")
                return
            if not os.path.isdir(base):
                await self._send_message(context, chat_id=chat_id, text="Базовый каталог недоступен.")
                return
            if os.path.isabs(name):
                target = os.path.normpath(name)
            else:
                target = os.path.normpath(os.path.join(base, name))
            root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
            if not is_within_root(target, root):
                await self._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
                return
            if not is_within_root(target, base):
                await self._send_message(context, chat_id=chat_id, text="Путь должен быть внутри текущего каталога.")
                return
            if os.path.exists(target):
                await self._send_message(context, chat_id=chat_id, text="Каталог уже существует.")
                return
            try:
                os.makedirs(target, exist_ok=False)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self._send_message(context, chat_id=chat_id, text=f"Не удалось создать каталог: {e}")
                return
            await self._send_message(context, chat_id=chat_id, text=f"Каталог создан: {target}")
            await self._send_dirs_menu(chat_id, context, base)
            return
        if self.pending_dir_input.pop(chat_id, None):
            mode = self.dirs_mode.get(chat_id, "new_session")
            path = text.strip()
            if not os.path.isdir(path):
                await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
                return
            root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
            if not is_within_root(path, root):
                await self._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
                return
            if mode == "agent_project":
                session_id = self.pending_agent_project.pop(chat_id, None)
                session = self.manager.get(session_id) if session_id else None
                if not session:
                    await self._send_message(context, chat_id=chat_id, text="Активная сессия не найдена.")
                    return
                ok, msg = self._set_agent_project_root(session, chat_id, context, path)
                self.dirs_mode.pop(chat_id, None)
                await self._send_message(context, chat_id=chat_id, text=msg if ok else "Не удалось подключить проект.")
                return
            tool = self.pending_new_tool.get(chat_id)
            if not tool:
                await self._send_message(context, chat_id=chat_id, text="Инструмент не выбран.")
                return
            session = self.manager.create(tool, path)
            self.pending_new_tool.pop(chat_id, None)
            await self._send_message(context, chat_id=chat_id, text=f"Сессия {session.id} создана и выбрана.")
            return
        if chat_id in self.pending_git_clone:
            base = self.pending_git_clone.pop(chat_id)
            url = text.strip()
            if not is_within_root(base, self.dirs_root.get(chat_id, self.config.defaults.workdir)):
                await self._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
                return
            if not os.path.isdir(base):
                await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
                return
            await self._send_message(context, chat_id=chat_id, text="Запускаю git clone…")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "clone",
                    url,
                    cwd=base,
                    env=self.git.git_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                output = (out or b"").decode(errors="ignore")
                if proc.returncode == 0:
                    await self._send_message(context, chat_id=chat_id, text="Клонирование завершено.")
                    tool = self.pending_new_tool.pop(chat_id, None)
                    if tool:
                        repo_path = None
                        match = re.search(r"Cloning into '([^']+)'", output)
                        if match:
                            repo_path = os.path.join(base, match.group(1))
                        if not repo_path:
                            repo_path = self._guess_clone_path(url, base)
                        root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
                        if repo_path and os.path.isdir(repo_path) and is_within_root(repo_path, root):
                            session = self.manager.create(tool, repo_path)
                            self.dirs_mode.pop(chat_id, None)
                            await self._send_message(
                                context,
                                chat_id=chat_id,
                                text=f"Сессия {session.id} создана и выбрана.",
                            )
                else:
                    await self._send_message(context, chat_id=chat_id, text=f"Ошибка git clone:\\n{output[:4000]}")
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self._send_message(context, chat_id=chat_id, text=f"Ошибка запуска git clone: {e}")
            return
        if await self.git.handle_pending_commit_message(chat_id, text, context):
            return
        # If a plugin dialog is waiting for input, let the plugin handler
        # in group -1 process the message; don't forward it to the CLI session.
        # Cancel / exit is handled inside each plugin's own handler, not here.
        if self._plugin_awaiting_input(chat_id):
            # Safety net: if the agent was turned off while a dialog was active,
            # the plugin handler in group -1 won't fire (_AgentEnabledFilter blocks it).
            # Detect this and clean up so the user isn't stuck.
            session = self.manager.active()
            if not session or not getattr(session, "agent_enabled", False):
                self._cancel_plugin_dialogs(chat_id)
                # Fall through to normal on_message processing below.
            else:
                return
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return

        stripped = text.lstrip()
        if stripped.startswith(">"):
            forwarded = stripped[1:].lstrip()
            if not forwarded.startswith("/"):
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text="После '>' должна идти /команда.",
                )
                return
            await self._handle_cli_input(session, forwarded, chat_id, context)
            return
        await self._buffer_or_send(session, text, chat_id, context)

    def _has_attachments(self, message: Message) -> bool:
        return any(
            [
                message.document,
                message.photo,
                message.video,
                message.audio,
                message.voice,
                message.sticker,
                message.animation,
                message.video_note,
            ]
        )

    async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        self.metrics.inc("commands")
        await self._send_message(context, chat_id=chat_id, text="Команда не найдена. Откройте меню бота.")

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        self.metrics.inc("messages")
        doc = update.message.document
        if not doc:
            return
        filename = doc.file_name or ""
        lower = filename.lower()
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return
        try:
            file_obj = await context.bot.get_file(doc.file_id)
            data = await file_obj.download_as_bytearray()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_message(context, chat_id=chat_id, text=f"Не удалось скачать файл: {e}")
            return
        if lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg") or (doc.mime_type or "").startswith("image/"):
            if doc.file_size and doc.file_size > self.config.defaults.image_max_mb * 1024 * 1024:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=f"Изображение слишком большое. Лимит {self.config.defaults.image_max_mb} МБ.",
                )
                return
            await self._flush_buffer(chat_id, session, context)
            caption = (update.message.caption or "").strip()
            await self._handle_image_bytes(session, data, filename or "image.jpg", caption, chat_id, context)
            return
        if not (
            lower.endswith(".txt")
            or lower.endswith(".md")
            or lower.endswith(".rst")
            or lower.endswith(".log")
            or lower.endswith(".html")
            or lower.endswith(".htm")
        ):
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Поддерживаются только .txt, .md, .rst, .log, .html и .htm.",
            )
            return
        if doc.file_size and doc.file_size > 500 * 1024:
            await self._send_message(context, chat_id=chat_id, text="Файл слишком большой. Лимит 500 КБ.")
            return
        await self._flush_buffer(chat_id, session, context)
        content = data.decode("utf-8", errors="replace")
        caption = (update.message.caption or "").strip()
        parts = []
        if caption:
            parts.append(caption)
        parts.append(f"===== Вложение: {filename} =====")
        parts.append(content)
        payload = "\n\n".join(parts)
        await self._handle_user_input(session, payload, chat_id, context)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        self.metrics.inc("messages")
        photos = update.message.photo or []
        if not photos:
            return
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return
        await self._flush_buffer(chat_id, session, context)
        photo = photos[-1]
        if photo.file_size and photo.file_size > self.config.defaults.image_max_mb * 1024 * 1024:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=f"Изображение слишком большое. Лимит {self.config.defaults.image_max_mb} МБ.",
            )
            return
        try:
            file_obj = await context.bot.get_file(photo.file_id)
            data = await file_obj.download_as_bytearray()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_message(context, chat_id=chat_id, text=f"Не удалось скачать изображение: {e}")
            return
        caption = (update.message.caption or "").strip()
        filename = f"{photo.file_unique_id}.jpg"
        await self._handle_image_bytes(session, data, filename, caption, chat_id, context)

    async def _handle_image_bytes(
        self,
        session: Session,
        data: bytearray,
        filename: str,
        caption: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not session.tool.image_cmd:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=f"CLI {session.tool.name} текущей сессии не поддерживает работу с изображениями.",
            )
            return
        safe_name = os.path.basename(filename) or "image.jpg"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_dir = self.config.defaults.image_temp_dir
        if os.path.isabs(base_dir):
            img_dir = base_dir
        else:
            img_dir = os.path.join(session.workdir, base_dir)
        os.makedirs(img_dir, exist_ok=True)
        self._cleanup_image_dir(img_dir)
        out_name = f"{stamp}_{safe_name}"
        image_path = os.path.join(img_dir, out_name)
        try:
            with open(image_path, "wb") as f:
                f.write(data)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_message(context, chat_id=chat_id, text=f"Не удалось сохранить изображение: {e}")
            return
        prompt = caption.strip()
        await self._handle_cli_input(session, prompt, chat_id, context, image_path=image_path)

    def _cleanup_image_dir(self, img_dir: str) -> None:
        cutoff = time.time() - 24 * 60 * 60
        try:
            for entry in os.scandir(img_dir):
                if not entry.is_file():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.remove(entry.path)
                except Exception:
                    continue
        except Exception:
            return

    async def _handle_cli_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        dest: Optional[dict] = None,
        image_path: Optional[str] = None,
    ) -> None:
        if dest is None:
            dest = {"kind": "telegram", "chat_id": chat_id}
        if image_path:
            dest["image_path"] = image_path
            dest["cleanup_image"] = True
        if session.busy or session.is_active_by_tick() or session.run_lock.locked():
            self.pending[chat_id] = PendingInput(session.id, text, dest, image_path=image_path)
            self.metrics.inc("queued")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Отменить текущую", callback_data="cancel_current"),
                        InlineKeyboardButton("Поставить в очередь", callback_data="queue_input"),
                    ],
                    [InlineKeyboardButton("Отмена ввода", callback_data="discard_input")],
                ]
            )
            await self._send_message(context, 
                chat_id=chat_id,
                text="Сессия занята. Что сделать с вашим вводом?",
                reply_markup=keyboard,
            )
            return
        asyncio.create_task(self.run_prompt(session, text, dest, context))

    async def _handle_agent_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        dest: Optional[dict] = None,
    ) -> None:
        if dest is None:
            dest = {"kind": "telegram", "chat_id": chat_id}
        if session.busy or session.is_active_by_tick() or session.run_lock.locked():
            self.pending[chat_id] = PendingInput(session.id, text, dest)
            self.metrics.inc("queued")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Отменить текущую", callback_data="cancel_current"),
                        InlineKeyboardButton("Поставить в очередь", callback_data="queue_input"),
                    ],
                    [InlineKeyboardButton("Отмена ввода", callback_data="discard_input")],
                ]
            )
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Сессия занята. Что сделать с вашим вводом?",
                reply_markup=keyboard,
            )
            return
        self._start_agent_task(session, text, dest, context)

    async def _handle_manager_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        dest: Optional[dict] = None,
    ) -> None:
        if dest is None:
            dest = {"kind": "telegram", "chat_id": chat_id}
        if session.busy or session.is_active_by_tick() or session.run_lock.locked():
            self.pending[chat_id] = PendingInput(session.id, text, dest)
            self.metrics.inc("queued")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Отменить текущую", callback_data="cancel_current"),
                        InlineKeyboardButton("Поставить в очередь", callback_data="queue_input"),
                    ],
                    [InlineKeyboardButton("Отмена ввода", callback_data="discard_input")],
                ]
            )
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Сессия занята. Что сделать с вашим вводом?",
                reply_markup=keyboard,
            )
            return
        self._start_manager_task(session, text, dest, context)

    async def _handle_user_input(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        dest: Optional[dict] = None,
    ) -> None:
        if getattr(session, "manager_enabled", False):
            await self._handle_manager_input(session, text, chat_id, context, dest=dest)
        elif session.agent_enabled:
            await self._handle_agent_input(session, text, chat_id, context, dest=dest)
        else:
            await self._handle_cli_input(session, text, chat_id, context, dest=dest)

    async def _buffer_or_send(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if len(text) < 3000:
            if not self.message_buffer.get(chat_id):
                await self._handle_user_input(session, text, chat_id, context)
                return
            self.message_buffer.setdefault(chat_id, []).append(text)
            await self._flush_buffer(chat_id, session, context)
            return
        self.message_buffer.setdefault(chat_id, []).append(text)
        await self._schedule_flush(chat_id, session, context)

    async def _schedule_flush(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        task = self.buffer_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
        self.buffer_tasks[chat_id] = asyncio.create_task(
            self._flush_after_delay(chat_id, session, context)
        )

    async def _flush_after_delay(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            await asyncio.sleep(2)
            await self._flush_buffer(chat_id, session, context)
        except asyncio.CancelledError:
            return

    async def _flush_buffer(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        parts = self.message_buffer.get(chat_id, [])
        if not parts:
            return
        self.message_buffer[chat_id] = []
        task = self.buffer_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
        payload = "\n\n".join(parts)
        await self._handle_user_input(session, payload, chat_id, context)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        try:
            await query.answer()
        except Exception as e:
            logging.exception(f"Ошибка ответа на callback: {e}")
        chat_id = query.message.chat_id if query.message else None
        if not chat_id:
            return
        try:
            if not self.is_allowed(chat_id):
                return
            self.context_by_chat[chat_id] = context
            if query.data.startswith("approve_cmd:"):
                cmd_id = query.data.split(":", 1)[1]
                pending = pop_pending_command(cmd_id)
                if not pending:
                    await query.edit_message_text("Запрос уже обработан.")
                    return
                await query.edit_message_text("Одобрено. Выполняю команду...")
                result = await execute_shell_command(pending.command, pending.cwd)
                output = result.get("output") if result.get("success") else result.get("error")
                await self._send_message(context, chat_id=chat_id, text=output or "(пустой вывод)")
                return
            if query.data.startswith("deny_cmd:"):
                cmd_id = query.data.split(":", 1)[1]
                pop_pending_command(cmd_id)
                await query.edit_message_text("Команда отклонена.")
                return
            if query.data.startswith("ask:"):
                _, question_id, idx_str = query.data.split(":", 2)
                pending = self.pending_questions.get(question_id)
                if not pending:
                    await query.edit_message_text("Вопрос устарел.")
                    return
                options = pending.get("options") or []
                try:
                    idx = int(idx_str)
                except ValueError:
                    await query.edit_message_text("Некорректный выбор.")
                    return
                if idx < 0 or idx >= len(options):
                    await query.edit_message_text("Выбор недоступен.")
                    return
                answer = options[idx]
                resolved = self.agent.resolve_question(question_id, answer)
                self.pending_questions.pop(question_id, None)
                if not resolved:
                    await query.edit_message_text("Ответ уже получен.")
                    return
                await query.edit_message_text(f"Вы выбрали: {answer}")
                return
            if query.data.startswith("agent_set:"):
                session = self.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                mode = query.data.split(":", 1)[1]
                session.agent_enabled = mode == "on"
                if session.agent_enabled:
                    # manager and agent are mutually exclusive
                    session.manager_enabled = False
                try:
                    self.manager._persist_sessions()
                except Exception:
                    pass
                # When the agent is turned off, cancel any pending plugin
                # dialogs so that on_message doesn't silently swallow text.
                if not session.agent_enabled:
                    cb_chat_id = query.message.chat_id if query.message else None
                    if cb_chat_id:
                        self._cancel_plugin_dialogs(cb_chat_id)
                status = "включен" if session.agent_enabled else "выключен"
                await query.edit_message_text(f"Агент {status}.")
                return
            if query.data.startswith("manager_set:"):
                session = self.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                mode = query.data.split(":", 1)[1]
                if mode == "on":
                    # Preconditions check (TZ section 16)
                    if not self.config.defaults.openai_api_key or not self.config.defaults.openai_model:
                        if query.message:
                            await self._edit_message(
                                context,
                                chat_id=query.message.chat_id,
                                message_id=query.message.message_id,
                                text="Для работы Manager нужен OpenAI API. Настройте openai_api_key и openai_model в config.yaml.",
                            )
                        return
                    if not session.cli or not session.alive:
                        if query.message:
                            await self._edit_message(
                                context,
                                chat_id=query.message.chat_id,
                                message_id=query.message.message_id,
                                text="Сначала создайте сессию через /new.",
                            )
                        return
                session.manager_enabled = mode == "on"
                if session.manager_enabled:
                    session.agent_enabled = False
                try:
                    self.manager._persist_sessions()
                except Exception:
                    pass
                # When manager is turned off, cancel running manager tasks.
                if not session.manager_enabled:
                    task = self.manager_tasks.get(session.id)
                    if task and not task.done():
                        task.cancel()
                status = "включен" if session.manager_enabled else "выключен"
                if query.message:
                    await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text=f"Менеджер {status}.")
                return
            if query.data == "manager_resume:continue":
                session = self.manager.active()
                if not session:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Активной сессии нет.")
                    return
                pending = self.manager_resume_pending.pop(session.id, None)
                if not pending:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Выбор устарел.")
                    return
                if query.message:
                    await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Продолжаю текущий план...")
                self._start_manager_task(session, MANAGER_CONTINUE_TOKEN, pending.get("dest") or {"kind": "telegram"}, context)
                return
            if query.data == "manager_resume:new":
                session = self.manager.active()
                if not session:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Активной сессии нет.")
                    return
                pending = self.manager_resume_pending.pop(session.id, None)
                if not pending:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Выбор устарел.")
                    return
                try:
                    self.manager_orchestrator.reset(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                if query.message:
                    await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Начинаю новый план...")
                self._start_manager_task(session, str(pending.get("prompt") or ""), pending.get("dest") or {"kind": "telegram"}, context)
                return
            if query.data == "manager_pause":
                session = self.manager.active()
                if not session:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Активной сессии нет.")
                    return
                try:
                    self.manager_orchestrator.pause(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                if query.message:
                    await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="План приостановлен.")
                return
            if query.data == "manager_reset":
                session = self.manager.active()
                if not session:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Активной сессии нет.")
                    return
                try:
                    self.manager_orchestrator.reset(session)
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                if query.message:
                    await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="План сброшен.")
                return
            if query.data == "manager_status":
                session = self.manager.active()
                if not session:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="Активной сессии нет.")
                    return
                try:
                    from agent.manager_store import load_plan

                    plan = load_plan(session.workdir)
                except Exception:
                    plan = None
                if not plan:
                    if query.message:
                        await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text="План не найден.")
                    return
                text = format_manager_status(plan)
                if query.message:
                    await self._edit_message(context, chat_id=query.message.chat_id, message_id=query.message.message_id, text=text)
                return
            if query.data in ("agent_project_connect", "agent_project_change"):
                session = self.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                self.pending_agent_project[chat_id] = session.id
                self.dirs_root[chat_id] = self.config.defaults.workdir
                self.dirs_mode[chat_id] = "agent_project"
                await query.edit_message_text("Выберите каталог проекта.")
                await self._send_dirs_menu(chat_id, context, self.config.defaults.workdir)
                return
            if query.data == "agent_project_disconnect":
                session = self.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                ok, msg = self._set_agent_project_root(session, chat_id, context, None)
                await query.edit_message_text(msg if ok else "Не удалось отключить проект.")
                return
            if query.data == "agent_cancel":
                await query.edit_message_text("Отменено.")
                return
            if query.data == "agent_clean_all":
                session = self.manager.active()
                if session:
                    self._interrupt_before_close(session.id, chat_id, context)
                    self._clear_agent_session_cache(session.id)
                removed, errors = self._clear_agent_sandbox()
                msg = f"Песочница очищена. Удалено: {removed}."
                if errors:
                    msg += f" Ошибок: {errors}."
                await query.edit_message_text(msg)
                return
            if query.data == "agent_clean_session":
                session = self.manager.active()
                if not session:
                    await query.edit_message_text("Активной сессии нет.")
                    return
                self._interrupt_before_close(session.id, chat_id, context)
                self._clear_agent_session_cache(session.id)
                ok = self._clear_agent_session_files(session.id)
                msg = "Файлы текущей сессии удалены." if ok else "Не удалось очистить файлы сессии."
                await query.edit_message_text(msg)
                return
            if query.data == "agent_plugin_commands":
                session = self.manager.active()
                if not session or not getattr(session, "agent_enabled", False):
                    await query.edit_message_text("Агент не активен.")
                    return
                try:
                    from agent.profiles import build_default_profile
                    tool_registry = getattr(self, "_tool_registry", None)
                    if tool_registry is None:
                        await query.edit_message_text("Реестр инструментов недоступен.")
                        return
                    profile = build_default_profile(self.config, tool_registry)
                    commands = self.agent.get_plugin_commands(profile)
                    plugin_menu = commands.get("plugin_menu") or []
                    if not plugin_menu:
                        await query.edit_message_text("Нет доступных плагинов.")
                        return
                    rows = [
                        [InlineKeyboardButton(entry["label"], callback_data=f"agent_plugin:{entry['plugin_id']}")]
                        for entry in plugin_menu
                    ]
                    rows.append([InlineKeyboardButton("Назад", callback_data="agent_cancel")])
                    await query.edit_message_text("Плагины:", reply_markup=InlineKeyboardMarkup(rows))
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    await query.edit_message_text("Не удалось получить список плагинов.")
                return
            if query.data.startswith("agent_plugin:"):
                pid = query.data.split(":", 1)[1]
                session = self.manager.active()
                if not session or not getattr(session, "agent_enabled", False):
                    await query.edit_message_text("Агент не активен.")
                    return
                try:
                    from agent.profiles import build_default_profile
                    tool_registry = getattr(self, "_tool_registry", None)
                    if tool_registry is None:
                        await query.edit_message_text("Реестр инструментов недоступен.")
                        return
                    profile = build_default_profile(self.config, tool_registry)
                    commands = self.agent.get_plugin_commands(profile)
                    plugin_menu = commands.get("plugin_menu") or []
                    entry = next((e for e in plugin_menu if e["plugin_id"] == pid), None)
                    if not entry:
                        await query.edit_message_text("Плагин недоступен.")
                        return
                    plugin = entry.get("plugin")
                    actions = entry.get("actions") or []
                    rows = []
                    for act in actions:
                        if plugin and hasattr(plugin, "action_button"):
                            btn = plugin.action_button(act["label"], act["action"])
                        else:
                            btn = InlineKeyboardButton(act["label"], callback_data=f"cb:{pid}:{act['action']}")
                        rows.append([btn])
                    rows.append([InlineKeyboardButton("Назад к плагинам", callback_data="agent_plugin_commands")])
                    label = entry.get("label", pid)
                    await query.edit_message_text(f"{label}:", reply_markup=InlineKeyboardMarkup(rows))
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    await query.edit_message_text("Ошибка при загрузке плагина.")
                return
            if query.data.startswith("state_pick:"):
                idx = int(query.data.split(":", 1)[1])
                keys = self.state_menu.get(chat_id, [])
                if idx < 0 or idx >= len(keys):
                    await query.edit_message_text("Выбор недоступен.")
                    return
                from state import load_state

                data = load_state(self.config.defaults.state_path)
                key = keys[idx]
                st = data.get(key)
                if not st:
                    await query.edit_message_text("Состояние не найдено.")
                    return
                text = (
                    f"Session: {st.session_id or 'нет'}\\n"
                    f"Tool: {st.tool}\\n"
                    f"Workdir: {st.workdir}\\n"
                    f"Resume: {st.resume_token or 'нет'}\\n"
                    f"Name: {st.name or 'нет'}\\n"
                    f"Summary: {st.summary or 'нет'}\\n"
                    f"Updated: {self._format_ts(st.updated_at)}"
                )
                await query.edit_message_text(text)
                return
            if query.data.startswith("state_page:"):
                page = int(query.data.split(":", 1)[1])
                keys = self.state_menu.get(chat_id, [])
                if not keys:
                    await query.edit_message_text("Состояние не найдено.")
                    return
                self.state_menu_page[chat_id] = page
                await query.edit_message_text(
                    "Выберите запись состояния:",
                    reply_markup=self._build_state_keyboard(chat_id),
                )
                return
        except Exception as e:
            logging.exception(f"Ошибка обработки кнопки: {e}")
            await self._send_message(context, chat_id=chat_id, text=f"Ошибка обработки кнопки: {e}")
            return
        if query.data.startswith("use_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.use_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            sid = items[idx]
            ok = self.manager.set_active(sid)
            if ok:
                s = self.manager.get(sid)
                label = s.name or f"{s.tool.name} @ {s.workdir}"
                await query.edit_message_text(f"Активная сессия: {sid} | {label}")
            else:
                await query.edit_message_text("Сессия не найдена.")
            return
        if query.data.startswith("close_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.close_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            sid = items[idx]
            self._interrupt_before_close(sid, chat_id, context)
            ok = self.manager.close(sid)
            if ok:
                self._clear_agent_session_cache(sid)
                await query.edit_message_text("Сессия закрыта.")
            else:
                await query.edit_message_text("Сессия не найдена.")
            return
        if query.data.startswith("new_tool:"):
            tool = query.data.split(":", 1)[1]
            if tool not in self.config.tools:
                await query.edit_message_text("Инструмент не найден.")
                return
            if not self._is_tool_available(tool):
                await query.edit_message_text(
                    "Инструмент не установлен. Сначала установите его. "
                    f"Ожидаемые: {self._expected_tools()}"
                )
                return
            self.pending_new_tool[chat_id] = tool
            await query.edit_message_text(f"Выбран инструмент {tool}. Выберите каталог.")
            self.dirs_root[chat_id] = self.config.defaults.workdir
            self.dirs_mode[chat_id] = "new_session"
            await self._send_dirs_menu(chat_id, context, self.config.defaults.workdir)
            return
        if query.data.startswith("dir_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.dirs_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            path = items[idx]
            mode = self.dirs_mode.get(chat_id, "new_session")
            if mode == "git_clone":
                self.pending_git_clone[chat_id] = path
                await query.edit_message_text("Отправьте ссылку для git clone.")
                return
            if mode == "agent_project":
                session_id = self.pending_agent_project.pop(chat_id, None)
                session = self.manager.get(session_id) if session_id else None
                if not session:
                    await query.edit_message_text("Активная сессия не найдена.")
                    return
                ok, msg = self._set_agent_project_root(session, chat_id, context, path)
                self.dirs_mode.pop(chat_id, None)
                await query.edit_message_text(msg if ok else "Не удалось подключить проект.")
                return
            tool = self.pending_new_tool.pop(chat_id, None)
            if not tool:
                await query.edit_message_text("Инструмент не выбран.")
                return
            session = self.manager.create(tool, path)
            await query.edit_message_text(f"Сессия {session.id} создана и выбрана.")
            return
        if query.data.startswith("dir_page:"):
            base = self.dirs_base.get(chat_id, self.config.defaults.workdir)
            page = int(query.data.split(":", 1)[1])
            await query.edit_message_text(
                "Выберите каталог:",
                reply_markup=build_dirs_keyboard(
                    self.dirs_menu,
                    self.dirs_base,
                    self.dirs_page,
                    self._short_label,
                    chat_id,
                    base,
                    page,
                ),
            )
            return
        if query.data == "dir_up":
            base = self.dirs_base.get(chat_id, self.config.defaults.workdir)
            parent = os.path.dirname(base.rstrip(os.sep)) or base
            root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
            if not is_within_root(parent, root):
                await query.edit_message_text("Нельзя выйти за пределы корневого каталога.")
                return
            err = prepare_dirs(
                self.dirs_menu,
                self.dirs_base,
                self.dirs_page,
                self.dirs_root,
                chat_id,
                parent,
            )
            if err:
                await query.edit_message_text(err)
                return
            await query.edit_message_text(
                "Выберите каталог:",
                reply_markup=build_dirs_keyboard(
                    self.dirs_menu,
                    self.dirs_base,
                    self.dirs_page,
                    self._short_label,
                    chat_id,
                    parent,
                    0,
                ),
            )
            return
        if query.data == "dir_enter":
            self.pending_dir_input[chat_id] = True
            await query.edit_message_text("Отправьте путь к каталогу сообщением.")
            return
        if query.data == "dir_create":
            base = self.dirs_base.get(chat_id, self.config.defaults.workdir)
            self.pending_dir_create[chat_id] = base
            await query.edit_message_text(
                "Отправьте имя нового каталога или путь относительно текущего. Для отмены введите '-'."
            )
            return
        if query.data == "dir_git_clone":
            base = self.dirs_base.get(chat_id, self.config.defaults.workdir)
            self.pending_git_clone[chat_id] = base
            await query.edit_message_text("Отправьте ссылку для git clone.")
            return
        if query.data == "dir_use_current":
            base = self.dirs_base.get(chat_id, self.config.defaults.workdir)
            root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
            if not is_within_root(base, root):
                await query.edit_message_text("Нельзя выйти за пределы корневого каталога.")
                return
            mode = self.dirs_mode.get(chat_id, "new_session")
            if mode == "git_clone":
                self.pending_git_clone[chat_id] = base
                await query.edit_message_text("Отправьте ссылку для git clone.")
                return
            if mode == "agent_project":
                session_id = self.pending_agent_project.pop(chat_id, None)
                session = self.manager.get(session_id) if session_id else None
                if not session:
                    await query.edit_message_text("Активная сессия не найдена.")
                    return
                ok, msg = self._set_agent_project_root(session, chat_id, context, base)
                self.dirs_mode.pop(chat_id, None)
                await query.edit_message_text(msg if ok else "Не удалось подключить проект.")
                return
            tool = self.pending_new_tool.pop(chat_id, None)
            if not tool:
                await query.edit_message_text("Инструмент не выбран.")
                return
            session = self.manager.create(tool, base)
            await query.edit_message_text(f"Сессия {session.id} создана и выбрана.")
            return
        if query.data == "restore_yes":
            active = load_active_state(self.config.defaults.state_path)
            if not active:
                await query.edit_message_text("Сохраненная активная сессия не найдена.")
                return
            if active.tool not in self.config.tools or not os.path.isdir(active.workdir):
                await query.edit_message_text("Сохраненная сессия недоступна.")
                return
            session = self.manager.create(active.tool, active.workdir)
            await query.edit_message_text(f"Сессия {session.id} восстановлена.")
            return
        if query.data == "restore_no":
            try:
                clear_active_state(self.config.defaults.state_path)
            except Exception:
                pass
            await query.edit_message_text("Восстановление отменено.")
            return
        if query.data.startswith("toolhelp_pick:"):
            tool = query.data.split(":", 1)[1]
            entry = get_toolhelp(self.config.defaults.toolhelp_path, tool)
            if entry:
                await self._send_toolhelp_content(chat_id, context, entry.content)
                return
            await query.edit_message_text("Загружаю help…")
            try:
                workdir = self.config.defaults.workdir
                active = self.manager.active()
                if active and active.tool.name == tool:
                    workdir = active.workdir
                content = await asyncio.to_thread(
                    run_tool_help,
                    self.config.tools[tool],
                    workdir,
                    self.config.defaults.idle_timeout_sec,
                )
                update_toolhelp(self.config.defaults.toolhelp_path, tool, content)
                await query.edit_message_text("Help получен, отправляю…")
                await self._send_toolhelp_content(chat_id, context, content)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await query.edit_message_text(f"Ошибка получения help: {e}")
            return
        if query.data.startswith("file_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.files_entries.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Файл не найден.")
                return
            item = items[idx]
            path = item.get("path") if isinstance(item, dict) else item
            session = self.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            if not is_within_root(path, session.workdir):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            if not os.path.isfile(path):
                await query.edit_message_text("Файл не найден.")
                return
            size = os.path.getsize(path)
            if size > 45 * 1024 * 1024:
                await query.edit_message_text("Файл слишком большой для отправки.")
                return
            await query.edit_message_text(f"Отправляю файл: {os.path.basename(path)}")
            try:
                with open(path, "rb") as f:
                    ok = await self._send_document(context, chat_id=chat_id, document=f)
                if not ok:
                    await query.edit_message_text("Ошибка отправки файла. Проверьте логи бота.")
            except Exception as e:
                logging.exception(f"Ошибка отправки файла из меню: {e}")
                await query.edit_message_text("Ошибка отправки файла. Проверьте логи бота.")
            return
        if query.data.startswith("file_nav:"):
            action = query.data.split(":", 1)[1]
            session = self.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            if action == "cancel":
                await query.edit_message_text("Операция отменена.")
                return
            if action.startswith("open:"):
                idx = int(action.split(":", 1)[1])
                entries = self.files_entries.get(chat_id, [])
                if idx < 0 or idx >= len(entries):
                    await query.edit_message_text("Папка не найдена.")
                    return
                entry = entries[idx]
                path = entry.get("path") if isinstance(entry, dict) else None
                if not path or not os.path.isdir(path):
                    await query.edit_message_text("Папка не найдена.")
                    return
                if not is_within_root(path, session.workdir):
                    await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                    return
                self.files_dir[chat_id] = path
                self.files_page[chat_id] = 0
                await self._send_files_menu(chat_id, session, context, edit_message=query)
                return
            if action == "up":
                current = self.files_dir.get(chat_id, session.workdir)
                root = session.workdir
                if os.path.abspath(current) == os.path.abspath(root):
                    await query.edit_message_text("Уже в корне рабочей директории.")
                    return
                parent = os.path.dirname(current)
                if not is_within_root(parent, root):
                    await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                    return
                self.files_dir[chat_id] = parent
                self.files_page[chat_id] = 0
                await self._send_files_menu(chat_id, session, context, edit_message=query)
                return
            if action == "prev":
                page = max(0, self.files_page.get(chat_id, 0) - 1)
                self.files_page[chat_id] = page
                await self._send_files_menu(chat_id, session, context, edit_message=query)
                return
            if action == "next":
                page = self.files_page.get(chat_id, 0) + 1
                self.files_page[chat_id] = page
                await self._send_files_menu(chat_id, session, context, edit_message=query)
                return
        if query.data.startswith("file_del:"):
            idx = int(query.data.split(":", 1)[1])
            entries = self.files_entries.get(chat_id, [])
            if idx < 0 or idx >= len(entries):
                await query.edit_message_text("Элемент не найден.")
                return
            entry = entries[idx]
            path = entry.get("path") if isinstance(entry, dict) else None
            session = self.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            if not path or not is_within_root(path, session.workdir):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            name = os.path.basename(path)
            self.files_pending_delete[chat_id] = path
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да", callback_data="file_del_confirm"),
                        InlineKeyboardButton("Отмена", callback_data="file_del_cancel"),
                    ]
                ]
            )
            await query.edit_message_text(f"Удалить {name}? Подтвердите:", reply_markup=keyboard)
            return
        if query.data == "file_del_current":
            session = self.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            current = self.files_dir.get(chat_id, session.workdir)
            root = session.workdir
            if os.path.abspath(current) == os.path.abspath(root):
                await query.edit_message_text("Нельзя удалить корневую рабочую директорию.")
                return
            if not is_within_root(current, root):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            self.files_pending_delete[chat_id] = current
            name = os.path.basename(current)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Да", callback_data="file_del_confirm"),
                        InlineKeyboardButton("Отмена", callback_data="file_del_cancel"),
                    ]
                ]
            )
            await query.edit_message_text(f"Удалить папку {name} рекурсивно? Подтвердите:", reply_markup=keyboard)
            return
        if query.data == "file_del_confirm":
            session = self.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            path = self.files_pending_delete.pop(chat_id, None)
            if not path:
                await query.edit_message_text("Нет операции удаления.")
                return
            if not is_within_root(path, session.workdir):
                await query.edit_message_text("Нельзя выйти за пределы рабочей директории.")
                return
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                await query.edit_message_text("Удалено.")
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await query.edit_message_text(f"Ошибка удаления: {e}")
            current = self.files_dir.get(chat_id, session.workdir)
            if not os.path.isdir(current) or not is_within_root(current, session.workdir):
                current = session.workdir
                self.files_dir[chat_id] = current
                self.files_page[chat_id] = 0
            await self._send_files_menu(chat_id, session, context, edit_message=None)
            return
        if query.data == "file_del_cancel":
            self.files_pending_delete.pop(chat_id, None)
            session = self.manager.active()
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            await query.edit_message_text("Удаление отменено.")
            await self._send_files_menu(chat_id, session, context, edit_message=None)
            return
        if query.data.startswith("preset_run:"):
            code = query.data.split(":", 1)[1]
            if code == "cancel":
                await query.edit_message_text("Отменено.")
                return
            session = await self.ensure_active_session(chat_id, context)
            if not session:
                await query.edit_message_text("Активной сессии нет.")
                return
            presets = self._preset_commands()
            prompt = presets.get(code)
            if not prompt:
                await query.edit_message_text("Шаблон не найден.")
                return
            await query.edit_message_text(f"Отправляю задачу: {code}")
            await self._handle_cli_input(session, prompt, chat_id, context)
            return
        if await self.git.handle_callback(query, chat_id, context):
            return
        if await self.session_ui.handle_callback(query, chat_id, context):
            return
        pending = self.pending.pop(chat_id, None)
        if not pending:
            await query.edit_message_text("Нет ожидающего ввода.")
            return
        session = self.manager.get(pending.session_id)
        if not session:
            await query.edit_message_text("Сессия уже закрыта.")
            return

        if query.data == "cancel_current":
            session.interrupt()
            if pending.image_path:
                try:
                    os.remove(pending.image_path)
                except Exception:
                    pass
            await query.edit_message_text("Текущая генерация прервана. Ввод отброшен.")
            return
        if query.data == "queue_input":
            item = {"text": pending.text, "dest": pending.dest}
            if pending.image_path:
                item["image_path"] = pending.image_path
            session.queue.append(item)
            self.manager._persist_sessions()
            await query.edit_message_text("Ввод поставлен в очередь.")
            return
        if query.data == "discard_input":
            if pending.image_path:
                try:
                    os.remove(pending.image_path)
                except Exception:
                    pass
            await query.edit_message_text("Ввод отменен.")
            return

    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        tools = sorted(self._available_tools())
        if not tools:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "CLI не найдены. Сначала установите нужные инструменты. "
                    f"Ожидаемые: {self._expected_tools()}"
                ),
            )
            return
        await self._send_message(context, chat_id=chat_id, text=f"Доступные инструменты: {', '.join(tools)}")
        

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        args = context.args
        if len(args) < 2:
            tools = list(sorted(self._available_tools()))
            if not tools:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=(
                        "CLI не найдены. Сначала установите нужные инструменты. "
                        f"Ожидаемые: {self._expected_tools()}"
                    ),
                )
                return
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(t, callback_data=f"new_tool:{t}")]
                    for t in tools
                ]
            )
            await self._send_message(context, 
                chat_id=chat_id,
                text="Выберите инструмент для новой сессии:",
                reply_markup=keyboard,
            )
            return
        tool, path = args[0], " ".join(args[1:])
        if tool not in self.config.tools:
            await self._send_message(context, chat_id=chat_id, text="Неизвестный инструмент.")
            return
        if not self._is_tool_available(tool):
            await self._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "Инструмент не установлен. Сначала установите его. "
                    f"Ожидаемые: {self._expected_tools()}"
                ),
            )
            return
        if not os.path.isdir(path):
            await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        session = self.manager.create(tool, path)
        await self._send_message(context, chat_id=chat_id, text=f"Сессия {session.id} создана и выбрана.")

    async def cmd_newpath(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        tool = self.pending_new_tool.pop(chat_id, None)
        if not tool:
            await self._send_message(context, chat_id=chat_id, text="Сначала выберите инструмент через /new.")
            return
        if not context.args:
            await self._send_message(context, chat_id=chat_id, text="Использование: /newpath <path>")
            return
        path = " ".join(context.args)
        if not os.path.isdir(path):
            await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
        if not is_within_root(path, root):
            await self._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
            return
        session = self.manager.create(tool, path)
        await self._send_message(context, chat_id=chat_id, text=f"Сессия {session.id} создана и выбрана.")

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not self.manager.sessions:
            await self._send_message(context, chat_id=chat_id, text="Активных сессий нет.")
            return
        keyboard = self.session_ui.build_sessions_menu()
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Выберите сессию:",
            reply_markup=keyboard,
        )

    async def cmd_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not context.args:
            items = list(self.manager.sessions.keys())
            if not items:
                await self._send_message(context, chat_id=chat_id, text="Сессий нет.")
                return
            self.use_menu[chat_id] = items
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"{sid}: {(self.manager.get(sid).name or (self.manager.get(sid).tool.name + ' @ ' + self.manager.get(sid).workdir))}",
                            callback_data=f"use_pick:{i}",
                        )
                    ]
                    for i, sid in enumerate(items)
                ]
            )
            await self._send_message(context, 
                chat_id=chat_id, text="Выберите сессию:", reply_markup=keyboard
            )
            return
        ok = self.manager.set_active(context.args[0])
        if ok:
            s = self.manager.get(context.args[0])
            label = s.name or f"{s.tool.name} @ {s.workdir}"
            await self._send_message(context, chat_id=chat_id, text=f"Активная сессия: {s.id} | {label}")
        else:
            await self._send_message(context, chat_id=chat_id, text="Сессия не найдена.")

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not context.args:
            items = list(self.manager.sessions.keys())
            if not items:
                await self._send_message(context, chat_id=chat_id, text="Сессий нет.")
                return
            self.close_menu[chat_id] = items
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(sid, callback_data=f"close_pick:{i}")]
                    for i, sid in enumerate(items)
                ]
            )
            await self._send_message(context, 
                chat_id=chat_id, text="Выберите сессию для закрытия:", reply_markup=keyboard
            )
            return
        self._interrupt_before_close(context.args[0], chat_id, context)
        ok = self.manager.close(context.args[0])
        if ok:
            self._clear_agent_session_cache(context.args[0])
            await self._send_message(context, chat_id=chat_id, text="Сессия закрыта.")
        else:
            await self._send_message(context, chat_id=chat_id, text="Сессия не найдена.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        now = time.time()
        busy_txt = "занята" if s.busy else "свободна"
        git_txt = "git: занято" if getattr(s, "git_busy", False) else "git: свободно"
        conflict_txt = ""
        if getattr(s, "git_conflict", False):
            conflict_txt = f" | конфликт: {s.git_conflict_kind or 'да'}"
        run_for = f"{int(now - s.started_at)}с" if s.started_at else "нет"
        last_out = f"{int(now - s.last_output_ts)}с назад" if s.last_output_ts else "нет"
        tick_txt = f"{int(now - s.last_tick_ts)}с назад" if s.last_tick_ts else "нет"
        agent_txt = "включен" if getattr(s, "agent_enabled", False) else "выключен"
        manager_txt = "включен" if getattr(s, "manager_enabled", False) else "выключен"
        project_root = getattr(s, "project_root", None)
        project_txt = project_root if project_root else "не подключен"
        await self._send_message(context, 
            chat_id=chat_id,
            text=(
                f"Активная сессия: {s.id} ({s.name or s.tool.name}) @ {s.workdir}\\n"
                f"Статус: {busy_txt} | {git_txt}{conflict_txt} | В работе: {run_for} | Агент: {agent_txt} | Manager: {manager_txt}\\n"
                f"Проект: {project_txt}\\n"
                f"Последний вывод: {last_out} | Последний тик: {tick_txt} | Тиков: {s.tick_seen}\\n"
                f"Очередь: {len(s.queue)} | Resume: {'есть' if s.resume_token else 'нет'}"
            ),
        )

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        enabled = bool(getattr(s, "agent_enabled", False))
        project_root = getattr(s, "project_root", None)
        project_line = f"Проект: {project_root}" if project_root else "Проект: не подключен"
        if enabled:
            rows = [[InlineKeyboardButton("Выключить агента", callback_data="agent_set:off")]]
            if project_root:
                rows.append([InlineKeyboardButton("Сменить проект", callback_data="agent_project_change")])
                rows.append([InlineKeyboardButton("Отключить проект", callback_data="agent_project_disconnect")])
            else:
                rows.append([InlineKeyboardButton("Подключить проект", callback_data="agent_project_connect")])
            rows.append([InlineKeyboardButton("Плагины", callback_data="agent_plugin_commands")])
            rows.append([InlineKeyboardButton("Очистить песочницу (кроме служебных)", callback_data="agent_clean_all")])
            rows.append([InlineKeyboardButton("Очистить текущую сессию (кроме служебных)", callback_data="agent_clean_session")])
            rows.append([InlineKeyboardButton("Отмена", callback_data="agent_cancel")])
            keyboard = InlineKeyboardMarkup(rows)
            text = f"Агент сейчас включен.\n{project_line}\nВыберите действие:"
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Включить агента", callback_data="agent_set:on")],
                    [InlineKeyboardButton("Отмена", callback_data="agent_cancel")],
                ]
            )
            text = f"Агент сейчас выключен.\n{project_line}\nВключить?"
        await self._send_message(context, chat_id=chat_id, text=text, reply_markup=keyboard)

    async def cmd_manager(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        enabled = bool(getattr(s, "manager_enabled", False))
        if enabled:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Выключить менеджера", callback_data="manager_set:off")],
                    [InlineKeyboardButton("Статус плана", callback_data="manager_status")],
                    [InlineKeyboardButton("Приостановить", callback_data="manager_pause")],
                    [InlineKeyboardButton("Сбросить план", callback_data="manager_reset")],
                    [InlineKeyboardButton("Отмена", callback_data="agent_cancel")],
                ]
            )
            text = "🏗 Менеджер проекта\n\nРежим: включен\n\nВыберите действие:"
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Включить менеджера", callback_data="manager_set:on")],
                    [InlineKeyboardButton("Отмена", callback_data="agent_cancel")],
                ]
            )
            text = "🏗 Менеджер проекта\n\nРежим: выключен\n\nВключить?"
        await self._send_message(context, chat_id=chat_id, text=text, reply_markup=keyboard)

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        s.interrupt()
        mtask = self.manager_tasks.get(s.id)
        if mtask and not mtask.done():
            mtask.cancel()
        task = self.agent_tasks.get(s.id)
        if task and not task.done():
            task.cancel()
        await self._send_message(context, chat_id=chat_id, text="Прерывание отправлено.")

    def _start_agent_task(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        existing = self.agent_tasks.get(session.id)
        if existing and not existing.done():
            # Session already has a running agent task; don't start a duplicate.
            return
        task = asyncio.create_task(self.run_agent(session, prompt, dest, context))
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            self.agent_tasks[session.id] = task

            def _cleanup(_task: asyncio.Task, sid: str = session.id) -> None:
                current = self.agent_tasks.get(sid)
                if current is _task:
                    self.agent_tasks.pop(sid, None)

            task.add_done_callback(_cleanup)

    def _start_manager_task(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        existing = self.manager_tasks.get(session.id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self.run_manager(session, prompt, dest, context))
        chat_id = dest.get("chat_id")
        if chat_id is not None:
            self.manager_tasks[session.id] = task

            def _cleanup(_task: asyncio.Task, sid: str = session.id) -> None:
                current = self.manager_tasks.get(sid)
                if current is _task:
                    self.manager_tasks.pop(sid, None)

            task.add_done_callback(_cleanup)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        if not s.queue:
            await self._send_message(context, chat_id=chat_id, text="Очередь пуста.")
            return
        await self._send_message(context, chat_id=chat_id, text=f"В очереди {len(s.queue)} сообщений.")

    async def cmd_clearqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        s.queue.clear()
        self.manager._persist_sessions()
        await self._send_message(context, chat_id=chat_id, text="Очередь очищена.")

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not context.args:
            await self._send_message(context, chat_id=chat_id, text="Использование: /rename <name> или /rename <id> <name>")
            return
        session = None
        if len(context.args) >= 2 and context.args[0] in self.manager.sessions:
            session = self.manager.get(context.args[0])
            name = " ".join(context.args[1:])
        else:
            session = self.manager.active()
            name = " ".join(context.args)
        if not session:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        session.name = name.strip()
        self.manager._persist_sessions()
        await self._send_message(context, chat_id=chat_id, text="Имя сессии обновлено.")

    async def cmd_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        path = " ".join(context.args) if context.args else self.config.defaults.workdir
        if not os.path.isdir(path):
            await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        self.dirs_root[chat_id] = path
        self.dirs_mode[chat_id] = "browse"
        await self._send_dirs_menu(chat_id, context, path)

    async def cmd_cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not context.args:
            await self._send_message(context, chat_id=chat_id, text="Использование: /cwd <path>")
            return
        path = " ".join(context.args)
        if not os.path.isdir(path):
            await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        session = self.manager.create(s.tool.name, path)
        await self._send_message(context, chat_id=chat_id, text=f"Новая сессия {session.id} создана и выбрана.")

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        session = await self.git.ensure_git_session(chat_id, context)
        if not session:
            return
        if not await self.git.ensure_git_repo(session, chat_id, context):
            return
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Git-операции:",
            reply_markup=self.git.build_git_keyboard(),
        )

    async def cmd_setprompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        args = context.args
        if len(args) < 2:
            await self._send_message(context, chat_id=chat_id, text="Использование: /setprompt <tool> <regex>")
            return
        tool_name = args[0]
        regex = " ".join(args[1:])
        tool = self.config.tools.get(tool_name)
        if not tool:
            await self._send_message(context, chat_id=chat_id, text="Инструмент не найден.")
            return
        tool.prompt_regex = regex
        from config import save_config

        save_config(self.config)
        await self._send_message(context, chat_id=chat_id, text="prompt_regex сохранен.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        if not context.args:
            token = s.resume_token or "нет"
            await self._send_message(context, chat_id=chat_id, text=f"Текущий resume: {token}")
            return
        token = " ".join(context.args).strip()
        s.resume_token = token
        self.manager._persist_sessions()
        await self._send_message(context, chat_id=chat_id, text="Resume сохранен.")

    async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if context.args:
            # Prefer session_id to avoid ambiguity when multiple sessions share tool/workdir.
            st = None
            sid = context.args[0]
            if sid in self.manager.sessions:
                s0 = self.manager.get(sid)
                if s0:
                    st = get_state(self.config.defaults.state_path, s0.tool.name, s0.workdir, session_id=s0.id)
            if not st and len(context.args) >= 2:
                tool = context.args[0]
                workdir = " ".join(context.args[1:])
                st = get_state(self.config.defaults.state_path, tool, workdir)
            if not st:
                await self._send_message(context, chat_id=chat_id, text="Состояние не найдено (используйте /state <session_id>).")
                return
            text = (
                f"Session: {st.session_id or 'нет'}\\n"
                f"Tool: {st.tool}\\n"
                f"Workdir: {st.workdir}\\n"
                f"Resume: {st.resume_token or 'нет'}\\n"
                f"Name: {st.name or 'нет'}\\n"
                f"Summary: {st.summary or 'нет'}\\n"
                f"Updated: {self._format_ts(st.updated_at)}"
            )
            await self._send_message(context, chat_id=chat_id, text=text)
            return
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        try:
            from state import load_state

            data = load_state(self.config.defaults.state_path)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._send_message(context, chat_id=chat_id, text=f"Ошибка чтения состояния: {e}")
            return
        if not data:
            await self._send_message(context, chat_id=chat_id, text="Состояние не найдено.")
            return
        keys = list(data.keys())
        self.state_menu[chat_id] = keys
        self.state_menu_page[chat_id] = 0
        keyboard = self._build_state_keyboard(chat_id)
        await self._send_message(context, 
            chat_id=chat_id,
            text="Выберите запись состояния:",
            reply_markup=keyboard,
        )

    async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not context.args:
            await self._send_message(context, chat_id=chat_id, text="Использование: /send <текст>")
            return
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return
        text = " ".join(context.args)
        await self._handle_cli_input(session, text, chat_id, context)

    def _bot_commands(self) -> list[BotCommand]:
        commands = []
        for entry in build_command_registry(self):
            if not entry["menu"]:
                continue
            commands.append(BotCommand(command=entry["name"], description=str(entry["desc"])))
        return commands

    async def set_bot_commands(self, app: Application) -> None:
        await app.bot.set_my_commands(self._bot_commands())

    async def cmd_toolhelp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        tools = list(sorted(self._available_tools()))
        if not tools:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "CLI не найдены. Сначала установите нужные инструменты. "
                    f"Ожидаемые: {self._expected_tools()}"
                ),
            )
            return
        self.toolhelp_menu[chat_id] = tools
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t, callback_data=f"toolhelp_pick:{t}")]
                for t in tools
            ]
        )
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Выберите инструмент для просмотра /команд:",
            reply_markup=keyboard,
        )

    async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return
        base = session.workdir
        if not os.path.isdir(base):
            await self._send_message(context, chat_id=chat_id, text="Рабочий каталог недоступен.")
            return
        self.files_dir[chat_id] = base
        self.files_page[chat_id] = 0
        await self._send_files_menu(chat_id, session, context, edit_message=None)

    def _list_dir_entries(self, base: str) -> list[dict]:
        entries: list[dict] = []
        try:
            for name in os.listdir(base):
                path = os.path.join(base, name)
                try:
                    is_dir = os.path.isdir(path)
                except Exception:
                    continue
                entries.append({"name": name, "path": path, "is_dir": is_dir})
        except Exception:
            return []
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    async def _send_files_menu(
        self,
        chat_id: int,
        session: Session,
        context: ContextTypes.DEFAULT_TYPE,
        edit_message: Optional[object],
    ) -> None:
        base = self.files_dir.get(chat_id, session.workdir)
        if not os.path.isdir(base):
            base = session.workdir
            self.files_dir[chat_id] = base
            self.files_page[chat_id] = 0
        entries = self._list_dir_entries(base)
        self.files_entries[chat_id] = entries
        page = max(0, self.files_page.get(chat_id, 0))
        page_size = 20
        start = page * page_size
        end = start + page_size
        page_entries = entries[start:end]
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        if page >= total_pages:
            page = max(0, total_pages - 1)
            self.files_page[chat_id] = page
            start = page * page_size
            end = start + page_size
            page_entries = entries[start:end]
        rows = []
        for idx, entry in enumerate(page_entries, start=start):
            if entry["is_dir"]:
                open_cb = f"file_nav:open:{idx}"
                label = f"📁 {entry['name']}"
            else:
                open_cb = f"file_pick:{idx}"
                label = f"📄 {entry['name']}"
            rows.append(
                [
                    InlineKeyboardButton(self._short_label(label, 60), callback_data=open_cb),
                    InlineKeyboardButton("🗑", callback_data=f"file_del:{idx}"),
                ]
            )
        nav_row = []
        nav_row.append(InlineKeyboardButton("⬅️ вверх", callback_data="file_nav:up"))
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data="file_nav:prev"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("▶️", callback_data="file_nav:next"))
        if nav_row:
            rows.append(nav_row)
        if os.path.abspath(base) != os.path.abspath(session.workdir):
            rows.append([InlineKeyboardButton("🗑 Удалить эту папку", callback_data="file_del_current")])
        rows.append([InlineKeyboardButton("Отмена", callback_data="file_nav:cancel")])
        text = f"Каталог: {base}\nСтраница {page + 1}/{total_pages}"
        keyboard = InlineKeyboardMarkup(rows)
        if edit_message:
            await edit_message.edit_message_text(text, reply_markup=keyboard)
        else:
            await self._send_message(context, chat_id=chat_id, text=text, reply_markup=keyboard)

    def _preset_commands(self) -> Dict[str, str]:
        if self.config.presets:
            return {p.name: p.prompt for p in self.config.presets}
        return {
            "tests": "Запусти тесты и дай краткий отчёт.",
            "lint": "Запусти линтер/форматтер и дай краткий отчёт.",
            "build": "Запусти сборку и дай краткий отчёт.",
            "refactor": "Сделай небольшой рефакторинг по месту и объясни изменения.",
        }

    def _guess_clone_path(self, url: str, base: str) -> Optional[str]:
        u = url.strip()
        if not u:
            return None
        path = u
        if u.startswith("git@") and ":" in u:
            path = u.split(":", 1)[1]
        elif "://" in u:
            path = u.split("://", 1)[1]
            if "/" in path:
                path = path.split("/", 1)[1]
        name = path.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not name:
            return None
        return os.path.join(base, name)

    async def cmd_preset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        presets = self._preset_commands()
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(k, callback_data=f"preset_run:{k}")] for k in presets.keys()]
            + [[InlineKeyboardButton("Отмена", callback_data="preset_run:cancel")]]
        )
        await self._send_message(context, chat_id=chat_id, text="Выберите шаблон:", reply_markup=keyboard)

    async def cmd_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        await self._send_message(context, chat_id=chat_id, text=self.metrics.snapshot())

    async def run_prompt_raw(self, prompt: str, session_id: Optional[str] = None) -> str:
        session = self.manager.get(session_id) if session_id else self.manager.active()
        if not session:
            raise RuntimeError("no_active_session")
        if session.run_lock.locked():
            raise RuntimeError("session_busy")
        async with session.run_lock:
            session.busy = True
            session.started_at = time.time()
            session.last_output_ts = session.started_at
            session.last_tick_ts = None
            session.last_tick_value = None
            session.tick_seen = 0
            try:
                output = await session.run_prompt(prompt)
                self.metrics.observe_output(len(output))
                return output
            finally:
                session.busy = False

    async def _send_dirs_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, base: str) -> None:
        allow_empty = self.dirs_mode.get(chat_id) == "git_clone"
        err = prepare_dirs(
            self.dirs_menu,
            self.dirs_base,
            self.dirs_page,
            self.dirs_root,
            chat_id,
            base,
            allow_empty=allow_empty,
        )
        if err:
            mode = self.dirs_mode.get(chat_id)
            if mode == "new_session":
                self.pending_new_tool.pop(chat_id, None)
            if mode == "git_clone":
                self.pending_git_clone.pop(chat_id, None)
            if mode == "agent_project":
                self.pending_agent_project.pop(chat_id, None)
            self.dirs_mode.pop(chat_id, None)
            self.dirs_menu.pop(chat_id, None)
            await self._send_message(context, chat_id=chat_id, text=err)
            return
        keyboard = build_dirs_keyboard(
            self.dirs_menu,
            self.dirs_base,
            self.dirs_page,
            self._short_label,
            chat_id,
            base,
            0,
        )
        title = "Выберите каталог проекта:" if self.dirs_mode.get(chat_id) == "agent_project" else "Выберите каталог:"
        await self._send_message(context, 
            chat_id=chat_id,
            text=title,
            reply_markup=keyboard,
        )

    async def _send_toolhelp_content(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str) -> None:
        if not content:
            await self._send_message(context, chat_id=chat_id, text="help пустой.")
            return
        plain = strip_ansi(content)
        suffix = (
            "Чтобы отправить /команду в CLI, используйте /send /команда "
            "или префикс '> /команда' в обычном сообщении."
        )
        if suffix not in plain:
            plain = f"{plain}\n\n{suffix}"
        preview = plain[:4000]
        if preview:
            await self._send_message(context, chat_id=chat_id, text=preview)
        if has_ansi(content):
            html_text = await asyncio.to_thread(ansi_to_html, content)
            if suffix not in strip_ansi(content):
                html_text = f"{html_text}<br><br>{html.escape(suffix)}"
            path = await asyncio.to_thread(make_html_file, html_text, "toolhelp")
            try:
                with open(path, "rb") as f:
                    ok = await self._send_document(context, chat_id=chat_id, document=f)
                if not ok:
                    await self._send_message(context, chat_id=chat_id, text="Ошибка отправки файла help. Проверьте логи бота.")
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass

def build_app(config: AppConfig) -> Application:
    app = Application.builder().token(config.telegram.token).build()
    bot_app = BotApp(config)
    app.bot_data["bot_app"] = bot_app

    async def _post_init(application: Application) -> None:
        await bot_app.set_bot_commands(application)
        await bot_app.mcp.start()
        if not bot_app._task_deadline_checker_task:
            bot_app._task_deadline_checker_task = asyncio.create_task(
                run_task_deadline_checker(application, bot_app.is_allowed),
                name="task_deadline_checker",
            )

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        msg = str(err)
        if "ConnectError" in msg or "NetworkError" in msg or "TimedOut" in msg:
            print("Сеть недоступна или Telegram API не резолвится. Проверьте интернет/DNS/доступ к api.telegram.org.")
            return
        print(f"Ошибка бота: {err}")

    core_registry = build_command_registry(bot_app)
    core_command_names = {e["name"] for e in core_registry}
    for entry in core_registry:
        async def _wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _handler=entry["handler"]) -> None:
            chat_id = update.effective_chat.id
            if not bot_app.is_allowed(chat_id):
                return
            bot_app.metrics.inc("commands")
            await _handler(update, context)
        app.add_handler(CommandHandler(entry["name"], _wrap))

    # Install plugin-provided Telegram UI handlers before the generic catch-all handlers,
    # otherwise plugins that rely on ConversationHandler/MessageHandler will never trigger.
    try:
        from agent.profiles import build_default_profile

        tool_registry = get_tool_registry(config)
        bot_app._tool_registry = tool_registry
        profile = build_default_profile(config, tool_registry)
        ui = bot_app.agent.get_plugin_ui(profile)
        plugin_commands = ui.get("plugin_commands") or []
        message_handlers = ui.get("message_handlers") or []
        inline_handlers = ui.get("inline_handlers") or []

        # 1) Callback query handlers declared by plugins (pattern-based).
        for cmd in plugin_commands:
            if cmd.get("callback_query_handler") and cmd.get("callback_pattern"):
                handler_fn = cmd["callback_query_handler"]
                pattern = cmd["callback_pattern"]
                kwargs = cmd.get("handler_kwargs") or {}

                async def _cb_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _fn=handler_fn, _kw=kwargs, _pat=pattern) -> None:
                    chat_id = update.effective_chat.id if update.effective_chat else None
                    if not chat_id or not bot_app.is_allowed(chat_id):
                        return
                    try:
                        res = _fn(update, context, **(_kw or {}))
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as e:
                        logging.exception(f"tool failed {str(e)}")

                app.add_handler(CallbackQueryHandler(_cb_wrap, pattern=pattern))

        # 2) Command handlers declared by plugins.
        for cmd in plugin_commands:
            command_name = cmd.get("command")
            if not command_name:
                continue
            if command_name in core_command_names:
                logging.warning(f"Skipping plugin command '{command_name}' because it collides with a core command")
                continue
            handler_fn = cmd.get("handler")
            if not callable(handler_fn):
                continue
            kwargs = cmd.get("handler_kwargs") or {}

            async def _cmd_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _fn=handler_fn, _kw=kwargs) -> None:
                chat_id = update.effective_chat.id
                if not bot_app.is_allowed(chat_id):
                    return
                session = bot_app.manager.active()
                if not session or not getattr(session, "agent_enabled", False):
                    await bot_app._send_message(context, chat_id=chat_id, text="Агент не активен.")
                    return
                try:
                    res = _fn(update, context, **(_kw or {}))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                    await bot_app._send_message(context, chat_id=chat_id, text="Ошибка при выполнении команды плагина.")

            app.add_handler(CommandHandler(command_name, _cmd_wrap, filters=filters.COMMAND))

        # 3) Message handlers declared by plugins (via DialogMixin or dict configs).
        #
        # Plugin handlers go into group=-1 (before core handlers in group 0).
        # When a plugin is actively waiting for user input (awaiting_input() == True),
        # the handler processes the message and raises ApplicationHandlerStop to prevent
        # the core on_message from also processing it.
        # When no plugin is waiting, the filter won't match and the update falls
        # through to on_message in group 0.
        from telegram.ext import ApplicationHandlerStop

        _PLUGIN_GROUP = -1

        class _AgentEnabledFilter(filters.MessageFilter):
            """Only match when the active session has agent_enabled=True."""
            def filter(self, message) -> bool:
                session = bot_app.manager.active()
                return bool(session and getattr(session, "agent_enabled", False))

        _agent_filter = _AgentEnabledFilter()

        for cfg in message_handlers:
            # Dict configs: {"filters": filters.X, "handler": callable, "handler_kwargs": {...}}
            if "filters" not in cfg:
                continue
            filter_obj = cfg.get("filters")
            handler_fn = cfg.get("handler")
            if not callable(handler_fn):
                continue
            kwargs = cfg.get("handler_kwargs") or {}

            async def _msg_wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _fn=handler_fn, _kw=kwargs) -> None:
                chat_id = update.effective_chat.id if update.effective_chat else None
                if not chat_id or not bot_app.is_allowed(chat_id):
                    return
                handled = False
                try:
                    res = _fn(update, context, **(_kw or {}))
                    if asyncio.iscoroutine(res):
                        handled = await res
                    else:
                        handled = res
                except Exception as e:
                    logging.exception(f"tool failed {str(e)}")
                # Only stop propagation if the plugin actually consumed the message.
                if handled:
                    raise ApplicationHandlerStop()

            app.add_handler(MessageHandler(_agent_filter & filter_obj, _msg_wrap), group=_PLUGIN_GROUP)

        # 4) Inline handlers (if any) follow the same pattern; left for later expansion.
        _ = inline_handlers
    except Exception as e:
        logging.exception(f"tool failed {str(e)}")

    app.add_handler(CallbackQueryHandler(bot_app.on_callback))
    app.add_handler(MessageHandler(filters.COMMAND, bot_app.on_unknown_command))
    app.add_handler(MessageHandler(filters.PHOTO, bot_app.on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bot_app.on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_app.on_message))
    app.post_init = _post_init
    app.add_error_handler(_on_error)
    return app


def main() -> None:
    # Ensure .env is loaded early for the whole process (plugins may read os.environ).
    # load_config() also loads .env near config, but this keeps behavior robust if config
    # path changes or config loading is refactored.
    try:
        load_dotenv_near(CONFIG_PATH, filename=".env", override=False)
    except Exception:
        pass
    config = load_config(CONFIG_PATH)
    app = build_app(config)
    app.run_polling()


if __name__ == "__main__":
    main()
