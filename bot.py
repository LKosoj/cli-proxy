import asyncio
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import AppConfig, load_config
from session import Session, SessionManager, run_tool_help
from summary import summarize_text
from state import get_state, load_active_state, update_state, clear_active_state
from toolhelp import get_toolhelp, update_toolhelp
from utils import ansi_to_html, build_preview, has_ansi, is_within_root, make_html_file, strip_ansi


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


@dataclass
class PendingInput:
    session_id: str
    text: str


class BotApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self.manager = SessionManager(config)
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
        self.pending_git_clone: Dict[int, str] = {}
        self.toolhelp_menu: Dict[int, list] = {}
        self.restore_offered: Dict[int, bool] = {}
        self.purge_menu: Dict[int, list] = {}

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.config.telegram.whitelist_chat_ids

    def _format_ts(self, ts: float) -> str:
        import datetime as _dt

        if not ts:
            return "нет"
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _short_label(self, text: str, max_len: int = 40) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    async def _send_message(self, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> None:
        for attempt in range(5):
            try:
                await context.bot.send_message(**kwargs)
                return
            except (NetworkError, TimedOut):
                if attempt == 4:
                    print("Ошибка сети при отправке сообщения в Telegram.")
                    return
                await asyncio.sleep(2 * (2 ** attempt))

    async def _send_document(self, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> None:
        for attempt in range(5):
            try:
                await context.bot.send_document(**kwargs)
                return
            except (NetworkError, TimedOut):
                if attempt == 4:
                    print("Ошибка сети при отправке файла в Telegram.")
                    return
                await asyncio.sleep(2 * (2 ** attempt))

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

    def _build_dirs_keyboard(self, chat_id: int, base: str, page: int) -> InlineKeyboardMarkup:
        self.dirs_base[chat_id] = base
        self.dirs_page[chat_id] = page
        items = self.dirs_menu.get(chat_id, [])
        page_size = 10
        start = page * page_size
        end = start + page_size
        rows = []
        for i, full in enumerate(items[start:end], start=start):
            label = self._short_label(os.path.basename(full))
            rows.append([InlineKeyboardButton(label, callback_data=f"dir_pick:{i}")])
        nav = []
        parent = os.path.dirname(base.rstrip(os.sep))
        if parent and parent != base:
            nav.append(InlineKeyboardButton("Вверх", callback_data="dir_up"))
        if start > 0:
            nav.append(InlineKeyboardButton("Назад", callback_data=f"dir_page:{page-1}"))
        if end < len(items):
            nav.append(InlineKeyboardButton("Далее", callback_data=f"dir_page:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("Использовать этот каталог", callback_data="dir_use_current")])
        rows.append([InlineKeyboardButton("git clone", callback_data="dir_git_clone")])
        rows.append([InlineKeyboardButton("Ввести путь", callback_data="dir_enter")])
        return InlineKeyboardMarkup(rows)

    def _prepare_dirs(self, chat_id: int, base: str) -> Optional[str]:
        root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
        if not is_within_root(base, root):
            return "Нельзя выйти за пределы корневого каталога."
        try:
            entries = sorted(
                d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))
            )
        except Exception as e:
            return f"Ошибка чтения каталога: {e}"
        if not entries:
            return "Подкаталогов нет. Добавьте хотя бы один каталог и попробуйте снова."
        self.dirs_base[chat_id] = base
        self.dirs_page[chat_id] = 0
        full_paths = [os.path.join(base, d) for d in entries]
        self.dirs_menu[chat_id] = full_paths
        return None

    async def send_output(self, session: Session, chat_id: int, output: str, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            summary = await asyncio.to_thread(summarize_text, strip_ansi(output), config=self.config)
            summary_source = "OpenAI"
        except Exception:
            summary = None
            summary_source = "preview"
        if summary:
            preview = summary
        else:
            preview = build_preview(output, self.config.defaults.summary_max_chars)
            summary_source = "preview"
        header = (
            f"[{session.id}|{session.name or session.tool.name}] Сессия: {session.id} | Инструмент: {session.tool.name}\\n"
            f"Каталог: {session.workdir}\\n"
            f"Длина вывода: {len(output)} символов | Очередь: {len(session.queue)}\\n"
            f"Resume: {'есть' if session.resume_token else 'нет'} | Источник анонса: {summary_source}"
        )
        await self._send_message(context, chat_id=chat_id, text=header)
        if preview:
            await self._send_message(context, chat_id=chat_id, text=preview)

        html_text = ansi_to_html(output)
        path = make_html_file(html_text, self.config.defaults.html_filename_prefix)
        try:
            with open(path, "rb") as f:
                await self._send_document(context, chat_id=chat_id, document=f)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        try:
            update_state(
                self.config.defaults.state_path,
                session.tool.name,
                session.workdir,
                session.resume_token,
                preview,
                name=session.name,
            )
        except Exception:
            pass

    async def run_prompt(self, session: Session, prompt: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        session.busy = True
        session.started_at = time.time()
        session.last_output_ts = session.started_at
        session.last_tick_ts = None
        session.last_tick_value = None
        session.tick_seen = 0
        try:
            output = await session.run_prompt(prompt)
            await self.send_output(session, chat_id, output, context)
        except Exception as e:
            await self._send_message(context, chat_id=chat_id, text=f"Ошибка выполнения: {e}")
        finally:
            session.busy = False
            if session.queue:
                next_prompt = session.queue.popleft()
                self.manager._persist_sessions()
                asyncio.create_task(self.run_prompt(session, next_prompt, chat_id, context))

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
        if not self.is_allowed(chat_id):
            return
        text = update.message.text
        if self.pending_dir_input.pop(chat_id, None):
            tool = self.pending_new_tool.get(chat_id)
            if not tool:
                await self._send_message(context, chat_id=chat_id, text="Инструмент не выбран.")
                return
            path = text.strip()
            if not os.path.isdir(path):
                await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
                return
            root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
            if not is_within_root(path, root):
                await self._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
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
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                output = (out or b"").decode(errors="ignore")
                if proc.returncode == 0:
                    await self._send_message(context, chat_id=chat_id, text="Клонирование завершено.")
                else:
                    await self._send_message(context, chat_id=chat_id, text=f"Ошибка git clone:\\n{output[:4000]}")
            except Exception as e:
                await self._send_message(context, chat_id=chat_id, text=f"Ошибка запуска git clone: {e}")
            return
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return

        if text.startswith(">"):
            forwarded = text[1:].lstrip()
            await self._handle_cli_input(session, forwarded, chat_id, context)
            return
        await self._handle_cli_input(session, text, chat_id, context)

    async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        await self._send_message(context, chat_id=chat_id, text="Команда не найдена. Откройте меню бота.")

    async def _handle_cli_input(self, session: Session, text: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        if session.busy or session.is_active_by_tick():
            self.pending[chat_id] = PendingInput(session.id, text)
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
        asyncio.create_task(self.run_prompt(session, text, chat_id, context))

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
        if not self.is_allowed(chat_id):
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
            ok = self.manager.close(sid)
            if ok:
                await query.edit_message_text("Сессия закрыта.")
            else:
                await query.edit_message_text("Сессия не найдена.")
            return
        if query.data.startswith("new_tool:"):
            tool = query.data.split(":", 1)[1]
            if tool not in self.config.tools:
                await query.edit_message_text("Инструмент не найден.")
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
                reply_markup=self._build_dirs_keyboard(chat_id, base, page),
            )
            return
        if query.data == "dir_up":
            base = self.dirs_base.get(chat_id, self.config.defaults.workdir)
            parent = os.path.dirname(base.rstrip(os.sep)) or base
            root = self.dirs_root.get(chat_id, self.config.defaults.workdir)
            if not is_within_root(parent, root):
                await query.edit_message_text("Нельзя выйти за пределы корневого каталога.")
                return
            err = self._prepare_dirs(chat_id, parent)
            if err:
                await query.edit_message_text(err)
                return
            await query.edit_message_text(
                "Выберите каталог:",
                reply_markup=self._build_dirs_keyboard(chat_id, parent, 0),
            )
            return
        if query.data == "dir_enter":
            self.pending_dir_input[chat_id] = True
            await query.edit_message_text("Отправьте путь к каталогу сообщением.")
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
            tool = self.pending_new_tool.pop(chat_id, None)
            if not tool:
                await query.edit_message_text("Инструмент не выбран.")
                return
            session = self.manager.create(tool, base)
            await query.edit_message_text(f"Сессия {session.id} создана и выбрана.")
            return
        if query.data.startswith("purge_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.purge_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Выбор недоступен.")
                return
            sid = items[idx]
            ok = self.manager.purge(sid)
            if ok:
                await query.edit_message_text("Сессия удалена из состояния.")
            else:
                await query.edit_message_text("Сессия не найдена.")
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
                await query.edit_message_text(f"Ошибка получения help: {e}")
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
            await query.edit_message_text("Текущая генерация прервана. Ввод отброшен.")
            return
        if query.data == "queue_input":
            session.queue.append(pending.text)
            self.manager._persist_sessions()
            await query.edit_message_text("Ввод поставлен в очередь.")
            return
        if query.data == "discard_input":
            await query.edit_message_text("Ввод отменен.")
            return

    async def cmd_tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        tools = ", ".join(sorted(self.config.tools.keys()))
        await self._send_message(context, chat_id=chat_id, text=f"Доступные инструменты: {tools}")
        

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        args = context.args
        if len(args) < 2:
            tools = list(sorted(self.config.tools.keys()))
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
        lines = []
        for sid, s in self.manager.sessions.items():
            active = "*" if sid == self.manager.active_session_id else " "
            label = s.name or f"{s.tool.name} @ {s.workdir}"
            lines.append(f"{active} {sid}: {label}")
        await self._send_message(context, chat_id=chat_id, text="\n".join(lines))

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
        ok = self.manager.close(context.args[0])
        if ok:
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
        run_for = f"{int(now - s.started_at)}с" if s.started_at else "нет"
        last_out = f"{int(now - s.last_output_ts)}с назад" if s.last_output_ts else "нет"
        tick_txt = f"{int(now - s.last_tick_ts)}с назад" if s.last_tick_ts else "нет"
        await self._send_message(context, 
            chat_id=chat_id,
            text=(
                f"Активная сессия: {s.id} ({s.name or s.tool.name}) @ {s.workdir}\\n"
                f"Статус: {busy_txt} | В работе: {run_for}\\n"
                f"Последний вывод: {last_out} | Последний тик: {tick_txt} | Тиков: {s.tick_seen}\\n"
                f"Очередь: {len(s.queue)} | Resume: {'есть' if s.resume_token else 'нет'}"
            ),
        )

    async def cmd_interrupt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if not s:
            await self._send_message(context, chat_id=chat_id, text="Активной сессии нет.")
            return
        s.interrupt()
        await self._send_message(context, chat_id=chat_id, text="Прерывание отправлено.")

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
        update_state(
            self.config.defaults.state_path,
            session.tool.name,
            session.workdir,
            session.resume_token,
            None,
            name=session.name,
        )
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

    async def cmd_gitclone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        base = self.config.defaults.workdir
        if not os.path.isdir(base):
            await self._send_message(context, chat_id=chat_id, text="Каталог не существует.")
            return
        self.dirs_root[chat_id] = base
        self.dirs_mode[chat_id] = "git_clone"
        await self._send_dirs_menu(chat_id, context, base)

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
        update_state(
            self.config.defaults.state_path,
            s.tool.name,
            s.workdir,
            s.resume_token,
            None,
            name=s.name,
        )
        await self._send_message(context, chat_id=chat_id, text="Resume сохранен.")

    async def cmd_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        s = self.manager.active()
        if context.args and len(context.args) >= 2:
            tool = context.args[0]
            workdir = " ".join(context.args[1:])
            st = get_state(self.config.defaults.state_path, tool, workdir)
            if not st:
                await self._send_message(context, chat_id=chat_id, text="Состояние не найдено.")
                return
            text = (
                f"Tool: {st.tool}\\n"
                f"Workdir: {st.workdir}\\n"
                f"Resume: {st.resume_token or 'нет'}\\n"
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

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        await self._send_message(context, 
            chat_id=chat_id,
            text="Меню доступно через кнопку рядом с полем ввода.",
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

    async def cmd_purge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        items = list(self.manager.sessions.keys())
        if not items:
            await self._send_message(context, chat_id=chat_id, text="Сессий нет.")
            return
        self.purge_menu[chat_id] = items
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(sid, callback_data=f"purge_pick:{i}")]
                for i, sid in enumerate(items)
            ]
        )
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Выберите сессию для удаления:",
            reply_markup=keyboard,
        )

    def _bot_commands(self) -> list[BotCommand]:
        specs = self._command_specs()
        commands = []
        for name in self._ordered_commands(specs):
            data = specs[name]
            commands.append(BotCommand(command=name, description=str(data["desc"])))
        return commands

    async def set_bot_commands(self, app: Application) -> None:
        await app.bot.set_my_commands(self._bot_commands())

    async def cmd_toolhelp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        tools = list(sorted(self.config.tools.keys()))
        self.toolhelp_menu[chat_id] = tools
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t, callback_data=f"toolhelp_pick:{t}")]
                for t in tools
            ]
        )
        await self._send_message(context, 
            chat_id=chat_id,
            text="Выберите инструмент для просмотра /команд:",
            reply_markup=keyboard,
        )

    async def _send_dirs_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, base: str) -> None:
        err = self._prepare_dirs(chat_id, base)
        if err:
            mode = self.dirs_mode.get(chat_id)
            if mode == "new_session":
                self.pending_new_tool.pop(chat_id, None)
            if mode == "git_clone":
                self.pending_git_clone.pop(chat_id, None)
            self.dirs_mode.pop(chat_id, None)
            self.dirs_menu.pop(chat_id, None)
            await self._send_message(context, chat_id=chat_id, text=err)
            return
        keyboard = self._build_dirs_keyboard(chat_id, base, 0)
        await self._send_message(context, 
            chat_id=chat_id,
            text="Выберите каталог:",
            reply_markup=keyboard,
        )

    async def _send_toolhelp_content(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, content: str) -> None:
        if not content:
            await self._send_message(context, chat_id=chat_id, text="help пустой.")
            return
        plain = strip_ansi(content)
        preview = plain[:4000]
        if preview:
            await self._send_message(context, chat_id=chat_id, text=preview)
        if has_ansi(content):
            html_text = ansi_to_html(content)
            path = make_html_file(html_text, "toolhelp")
            try:
                with open(path, "rb") as f:
                    await self._send_document(context, chat_id=chat_id, document=f)
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _command_specs(self) -> Dict[str, Dict[str, object]]:
        return {
            "tools": {"desc": "Показать доступные инструменты.", "quick": True},
            "new": {"desc": "Создать новую сессию (через меню).", "quick": True},
            "newpath": {"desc": "Задать путь для новой сессии после выбора инструмента.", "quick": False},
            "sessions": {"desc": "Список активных сессий.", "quick": True},
            "use": {"desc": "Выбрать активную сессию (через меню).", "quick": True},
            "close": {"desc": "Закрыть сессию (через меню).", "quick": True},
            "status": {"desc": "Показать статус активной сессии.", "quick": True},
            "interrupt": {"desc": "Прервать текущую генерацию.", "quick": True},
            "queue": {"desc": "Показать очередь.", "quick": True},
            "clearqueue": {"desc": "Очистить очередь активной сессии.", "quick": True},
            "rename": {"desc": "Переименовать сессию.", "quick": False},
            "purge": {"desc": "Удалить сессию из состояния.", "quick": False},
            "cwd": {"desc": "Создать новую сессию в другом каталоге.", "quick": False},
            "dirs": {"desc": "Просмотр каталогов (меню).", "quick": True},
            "gitclone": {"desc": "Клонировать репозиторий (git clone) в каталог.", "quick": True},
            "resume": {"desc": "Показать/установить resume токен.", "quick": False},
            "state": {"desc": "Просмотр состояния (меню).", "quick": True},
            "setprompt": {"desc": "Установить prompt_regex для инструмента.", "quick": False},
            "toolhelp": {"desc": "Показать /команды выбранного инструмента.", "quick": True},
            "send": {"desc": "Отправить текст напрямую в CLI.", "quick": False},
        }

    def _ordered_commands(self, specs: Dict[str, Dict[str, object]]) -> list[str]:
        order = [
            "tools",
            "new",
            "newpath",
            "sessions",
            "use",
            "close",
            "rename",
            "status",
            "interrupt",
            "queue",
            "clearqueue",
            "send",
            "resume",
            "state",
            "setprompt",
            "dirs",
            "gitclone",
            "cwd",
            "toolhelp",
            "purge",
        ]
        ordered = [c for c in order if c in specs]
        tail = [c for c in specs.keys() if c not in ordered]
        return ordered + sorted(tail)

def build_app(config: AppConfig) -> Application:
    app = Application.builder().token(config.telegram.token).build()
    bot_app = BotApp(config)

    async def _post_init(application: Application) -> None:
        await bot_app.set_bot_commands(application)

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        msg = str(err)
        if "ConnectError" in msg or "NetworkError" in msg or "TimedOut" in msg:
            print("Сеть недоступна или Telegram API не резолвится. Проверьте интернет/DNS/доступ к api.telegram.org.")
            return
        print(f"Ошибка бота: {err}")

    app.add_handler(CommandHandler("tools", bot_app.cmd_tools))
    app.add_handler(CommandHandler("new", bot_app.cmd_new))
    app.add_handler(CommandHandler("newpath", bot_app.cmd_newpath))
    app.add_handler(CommandHandler("sessions", bot_app.cmd_sessions))
    app.add_handler(CommandHandler("use", bot_app.cmd_use))
    app.add_handler(CommandHandler("close", bot_app.cmd_close))
    app.add_handler(CommandHandler("status", bot_app.cmd_status))
    app.add_handler(CommandHandler("interrupt", bot_app.cmd_interrupt))
    app.add_handler(CommandHandler("queue", bot_app.cmd_queue))
    app.add_handler(CommandHandler("clearqueue", bot_app.cmd_clearqueue))
    app.add_handler(CommandHandler("cwd", bot_app.cmd_cwd))
    app.add_handler(CommandHandler("rename", bot_app.cmd_rename))
    app.add_handler(CommandHandler("setprompt", bot_app.cmd_setprompt))
    app.add_handler(CommandHandler("dirs", bot_app.cmd_dirs))
    app.add_handler(CommandHandler("gitclone", bot_app.cmd_gitclone))
    app.add_handler(CommandHandler("resume", bot_app.cmd_resume))
    app.add_handler(CommandHandler("state", bot_app.cmd_state))
    app.add_handler(CommandHandler("menu", bot_app.cmd_menu))
    app.add_handler(CommandHandler("start", bot_app.cmd_menu))
    app.add_handler(CommandHandler("send", bot_app.cmd_send))
    app.add_handler(CommandHandler("purge", bot_app.cmd_purge))
    app.add_handler(CommandHandler("toolhelp", bot_app.cmd_toolhelp))

    app.add_handler(CallbackQueryHandler(bot_app.on_callback))
    app.add_handler(MessageHandler(filters.COMMAND, bot_app.on_unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_app.on_message))
    app.post_init = _post_init
    app.add_error_handler(_on_error)
    return app


def main() -> None:
    config = load_config(CONFIG_PATH)
    app = build_app(config)
    app.run_polling()


if __name__ == "__main__":
    main()
