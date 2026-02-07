"""
Module containing the core Telegram bot functionality.
"""

import asyncio
import html
import logging
import os
import shutil
import time
import re
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


@dataclass
class PendingInput:
    session_id: str
    text: str
    dest: dict
    image_path: Optional[str] = None


class TelegramBot:
    """
    Class containing core Telegram bot functionality.
    """
    
    def __init__(self, config: AppConfig):
        self.config = config
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

    def _available_tools(self) -> list[str]:
        return [name for name in self.config.tools.keys() if self._is_tool_available(name)]

    def _expected_tools(self) -> str:
        return ", ".join(sorted(self.config.tools.keys()))

    def _is_tool_available(self, name: str) -> bool:
        tool = self.config.tools.get(name)
        if not tool:
            return False
        exe = self._tool_exec(tool)
        return bool(exe and shutil.which(exe))

    def _tool_exec(self, tool: ToolConfig) -> Optional[str]:
        for cmd in (tool.cmd, tool.headless_cmd, tool.interactive_cmd):
            if cmd and len(cmd) > 0:
                return cmd[0]
        return None

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.config.telegram.whitelist_chat_ids

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

    def _clear_agent_session_cache(self, session_id: str) -> None:
        try:
            self.agent.clear_session_cache(session_id)
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")

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