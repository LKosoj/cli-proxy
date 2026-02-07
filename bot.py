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

# Export utility functions at module level for easy patching in tests
from utils import ansi_to_html as ansi_to_html, make_html_file as make_html_file
from summary import summarize_text_with_reason as summarize_text_with_reason
from tg_markdown import to_markdown_v2
from agent import execute_shell_command, pop_pending_command, set_approval_callback
from agent.orchestrator import OrchestratorRunner
from agent.manager import ManagerOrchestrator
from agent.manager import MANAGER_CONTINUE_TOKEN, format_manager_status, needs_resume_choice
from agent.plugins.task_management import run_task_deadline_checker
from agent.tooling.registry import get_tool_registry

from handlers import BotHandlers
from callbacks import CallbackHandler
from message_processor import MessageProcessor
from session_management import SessionManagement


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
        
        # Initialize modules
        from handlers import BotHandlers
        from callbacks import CallbackHandler  
        from message_processor import MessageProcessor
        from session_management import SessionManagement
        
        self.handlers = BotHandlers(self)
        self.callbacks = CallbackHandler(self)
        self.message_processor = MessageProcessor(self)
        self.session_management = SessionManagement(self)
        
        # Store references to modules to allow patching in tests
        import utils
        import summary
        self._utils_module = utils
        self._summary_module = summary

    @property
    def ansi_to_html(self):
        # Access from module level to allow patching in tests
        import sys
        bot_module = sys.modules.get('bot', sys.modules[__name__])
        if hasattr(bot_module, 'ansi_to_html'):
            return bot_module.ansi_to_html
        from utils import ansi_to_html
        return ansi_to_html

    @property
    def make_html_file(self):
        # Access from module level to allow patching in tests
        import sys
        bot_module = sys.modules.get('bot', sys.modules[__name__])
        if hasattr(bot_module, 'make_html_file'):
            return bot_module.make_html_file
        from utils import make_html_file
        return make_html_file

    @property
    def summarize_text_with_reason(self):
        # Access from module level to allow patching in tests
        import sys
        bot_module = sys.modules.get('bot', sys.modules[__name__])
        if hasattr(bot_module, 'summarize_text_with_reason'):
            return bot_module.summarize_text_with_reason
        from summary import summarize_text_with_reason
        return summarize_text_with_reason

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
        await self.session_management.send_output(session, dest, output, context, send_header=send_header, header_override=header_override, force_html=force_html)

    async def run_prompt(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.session_management.run_prompt(session, prompt, dest, context)

    async def run_agent(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.session_management.run_agent(session, prompt, dest, context)

    async def run_manager(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.session_management.run_manager(session, prompt, dest, context)

    def _clear_agent_session_cache(self, session_id: str) -> None:
        self.session_management._clear_agent_session_cache(session_id)

    def _set_agent_project_root(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        project_root: Optional[str],
    ) -> tuple[bool, str]:
        return self.session_management._set_agent_project_root(session, chat_id, context, project_root)

    def _interrupt_before_close(self, session_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.session_management._interrupt_before_close(session_id, chat_id, context)

    async def ensure_active_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]:
        return await self.session_management.ensure_active_session(chat_id, context)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.message_processor.on_message(update, context)

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
        await self.callbacks.on_callback(update, context)
    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_tools(update, context)
        

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_new(update, context)

    async def cmd_newpath(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_newpath(update, context)

    async def cmd_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_sessions(update, context)

    async def cmd_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_use(update, context)

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_close(update, context)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_status(update, context)

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_agent(update, context)

    async def cmd_manager(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_manager(update, context)

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_interrupt(update, context)

    def _start_agent_task(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.session_management._start_agent_task(session, prompt, dest, context)

    def _start_manager_task(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.session_management._start_manager_task(session, prompt, dest, context)

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_queue(update, context)

    async def cmd_clearqueue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_clearqueue(update, context)

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_rename(update, context)

    async def cmd_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_dirs(update, context)

    async def cmd_cwd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_cwd(update, context)

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_git(update, context)

    async def cmd_setprompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_setprompt(update, context)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_resume(update, context)

    async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_state(update, context)

    async def cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_send(update, context)

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
        await self.handlers.cmd_toolhelp(update, context)

    async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_files(update, context)

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
        await self.handlers._send_files_menu(chat_id, session, context, edit_message)

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
        await self.handlers.cmd_preset(update, context)

    async def cmd_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handlers.cmd_metrics(update, context)

    async def run_prompt_raw(self, prompt: str, session_id: Optional[str] = None) -> str:
        return await self.session_management.run_prompt_raw(prompt, session_id)

    async def _send_dirs_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, base: str) -> None:
        await self.handlers._send_dirs_menu(chat_id, context, base)

    async def _send_toolhelp_content(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str) -> None:
        await self.handlers._send_toolhelp_content(chat_id, context, content)

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
