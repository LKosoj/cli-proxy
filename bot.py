import asyncio
import os
import shutil
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

from config import AppConfig, ToolConfig, load_config
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
        self.git_branch_menu: Dict[int, list] = {}
        self.git_pending_ref: Dict[int, str] = {}
        self.git_pull_target: Dict[int, str] = {}

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

    def _build_git_keyboard(self) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("Status", callback_data="git_status"),
                InlineKeyboardButton("Fetch", callback_data="git_fetch"),
            ],
            [
                InlineKeyboardButton("Pull", callback_data="git_pull"),
                InlineKeyboardButton("Merge", callback_data="git_merge_menu"),
            ],
            [
                InlineKeyboardButton("Rebase", callback_data="git_rebase_menu"),
                InlineKeyboardButton("Diff", callback_data="git_diff"),
            ],
            [
                InlineKeyboardButton("Log", callback_data="git_log"),
                InlineKeyboardButton("Stash", callback_data="git_stash"),
            ],
        ]
        return InlineKeyboardMarkup(rows)

    def _build_git_branches_keyboard(self, chat_id: int, action: str) -> InlineKeyboardMarkup:
        branches = self.git_branch_menu.get(chat_id, [])
        rows = []
        for i, ref in enumerate(branches):
            rows.append(
                [InlineKeyboardButton(self._short_label(ref), callback_data=f"git_{action}_pick:{i}")]
            )
        rows.append([InlineKeyboardButton("Отмена", callback_data="git_cancel")])
        return InlineKeyboardMarkup(rows)

    def _build_git_pull_keyboard(self, ref: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"Merge {ref}", callback_data="git_pull_merge"),
                    InlineKeyboardButton(f"Rebase {ref}", callback_data="git_pull_rebase"),
                ],
                [InlineKeyboardButton("Отмена", callback_data="git_pull_cancel")],
            ]
        )

    def _build_git_confirm_keyboard(self, action: str, ref: str) -> InlineKeyboardMarkup:
        label = "Выполнить merge" if action == "merge" else "Выполнить rebase"
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"{label} {ref}", callback_data=f"git_confirm_{action}")],
                [InlineKeyboardButton("Отмена", callback_data="git_cancel")],
            ]
        )

    def _build_git_conflict_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Открыть diff", callback_data="git_conflict_diff"),
                    InlineKeyboardButton("Abort", callback_data="git_conflict_abort"),
                ],
                [
                    InlineKeyboardButton("Continue", callback_data="git_conflict_continue"),
                    InlineKeyboardButton("Позвать агента", callback_data="git_conflict_agent"),
                ],
            ]
        )

    def _ensure_git_state(self, session: Session) -> None:
        if not hasattr(session, "git_busy"):
            session.git_busy = False
        if not hasattr(session, "git_conflict"):
            session.git_conflict = False
        if not hasattr(session, "git_conflict_files"):
            session.git_conflict_files = []
        if not hasattr(session, "git_conflict_kind"):
            session.git_conflict_kind = None

    async def _ensure_git_session(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[Session]:
        session = self.manager.active()
        if not session:
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Нет активной сессии. Используйте /use для выбора.",
            )
            return None
        self._ensure_git_state(session)
        return session

    async def _ensure_git_repo(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        code, output = await self._run_git(session, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or output.strip() != "true":
            await self._send_message(context, chat_id=chat_id, text="Каталог не является git-репозиторием.")
            return False
        return True

    async def _ensure_git_not_busy(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if session.busy or session.is_active_by_tick():
            await self._send_message(
                context,
                chat_id=chat_id,
                text="CLI-сессия занята. Дождитесь завершения и попробуйте снова.",
            )
            return False
        if session.git_busy:
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Git-операция уже выполняется. Подождите.",
            )
            return False
        return True

    async def _run_git(self, session: Session, args: list[str]) -> tuple[int, str]:
        env = os.environ.copy()
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=session.workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = (out or b"").decode(errors="ignore")
        return proc.returncode, output

    async def _git_current_branch(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(session, ["rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0:
            return None
        return output.strip() or None

    async def _git_upstream(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(
            session, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
        )
        if code != 0:
            return None
        return output.strip() or None

    async def _git_ref_exists(self, session: Session, ref: str) -> bool:
        code, _ = await self._run_git(session, ["rev-parse", "--verify", "--quiet", ref])
        return code == 0

    async def _git_default_remote(self, session: Session) -> Optional[str]:
        code, output = await self._run_git(session, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        if code == 0 and output.strip():
            return output.strip()
        for ref in ("origin/main", "origin/master"):
            if await self._git_ref_exists(session, ref):
                return ref
        return None

    async def _git_ahead_behind(self, session: Session, ref: str) -> Optional[tuple[int, int]]:
        code, output = await self._run_git(session, ["rev-list", "--left-right", "--count", f"HEAD...{ref}"])
        if code != 0:
            return None
        parts = output.strip().split()
        if len(parts) != 2:
            return None
        try:
            ahead = int(parts[0])
            behind = int(parts[1])
        except Exception:
            return None
        return ahead, behind

    async def _git_in_progress(self, session: Session) -> Optional[str]:
        code, _ = await self._run_git(session, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])
        if code == 0:
            return "merge"
        for key in ("rebase-merge", "rebase-apply"):
            code, output = await self._run_git(session, ["rev-parse", "--git-path", key])
            if code == 0 and output.strip():
                path = output.strip()
                if not os.path.isabs(path):
                    path = os.path.join(session.workdir, path)
                if os.path.exists(path):
                    return "rebase"
        return None

    def _git_set_conflict(self, session: Session, files: list[str], kind: Optional[str]) -> None:
        session.git_conflict = True
        session.git_conflict_files = files
        session.git_conflict_kind = kind

    def _git_clear_conflict(self, session: Session) -> None:
        session.git_conflict = False
        session.git_conflict_files = []
        session.git_conflict_kind = None

    async def _git_conflict_files(self, session: Session) -> list[str]:
        code, output = await self._run_git(session, ["diff", "--name-only", "--diff-filter=U"])
        if code != 0:
            self._git_clear_conflict(session)
            return []
        files = [line.strip() for line in output.splitlines() if line.strip()]
        if files:
            kind = await self._git_in_progress(session)
            self._git_set_conflict(session, files, kind)
        else:
            self._git_clear_conflict(session)
        return files

    async def _git_status_text(self, session: Session) -> str:
        branch = await self._git_current_branch(session) or "неизвестно"
        code, output = await self._run_git(session, ["status", "--porcelain"])
        dirty = bool(output.strip()) if code == 0 else False
        upstream = await self._git_upstream(session)
        if not upstream and branch and branch != "HEAD":
            candidate = f"origin/{branch}"
            if await self._git_ref_exists(session, candidate):
                upstream = candidate
        if not upstream:
            upstream = await self._git_default_remote(session)
        ahead_behind = await self._git_ahead_behind(session, upstream) if upstream else None
        conflicts = await self._git_conflict_files(session)
        lines = [
            f"Ветка: {branch}",
            f"Состояние: {'dirty' if dirty else 'clean'}",
        ]
        if upstream and ahead_behind:
            ahead, behind = ahead_behind
            lines.append(f"Upstream: {upstream} | ahead {ahead} / behind {behind}")
        elif upstream:
            lines.append(f"Upstream: {upstream} | ahead/behind: недоступно")
        else:
            lines.append("Upstream: нет")
        if conflicts:
            lines.append(f"Конфликт: да ({len(conflicts)} файлов)")
        else:
            lines.append("Конфликт: нет")
        return "\n".join(lines)

    async def _send_git_output(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str, output: str) -> None:
        text = output.strip()
        if not text:
            await self._send_message(context, chat_id=chat_id, text=f"{title}: готово.")
            return
        if len(text) > 4000:
            text = text[:4000]
        await self._send_message(context, chat_id=chat_id, text=f"{title}:\n{text}")

    async def _handle_git_conflict(self, session: Session, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        files = session.git_conflict_files or await self._git_conflict_files(session)
        short_list = files[:10]
        listing = "\n".join(f"- {f}" for f in short_list) if short_list else "- (список пуст)"
        if len(files) > len(short_list):
            listing += f"\n…и еще {len(files) - len(short_list)}"
        await self._send_message(
            context,
            chat_id=chat_id,
            text=f"Обнаружены конфликты:\n{listing}",
            reply_markup=self._build_git_conflict_keyboard(),
        )

    async def _execute_merge_rebase(
        self,
        session: Session,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
        ref: str,
    ) -> None:
        session.git_busy = True
        try:
            if action == "merge":
                code, output = await self._run_git(session, ["merge", ref])
            else:
                code, output = await self._run_git(session, ["rebase", ref])
            await self._send_git_output(context, chat_id, f"{action.title()} {ref}", output)
            conflicts = await self._git_conflict_files(session)
            if conflicts:
                await self._handle_git_conflict(session, chat_id, context)
                return
            if code == 0:
                self._git_clear_conflict(session)
        finally:
            session.git_busy = False

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

    def _prepare_dirs(self, chat_id: int, base: str, allow_empty: bool = False) -> Optional[str]:
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
            if allow_empty:
                self.dirs_base[chat_id] = base
                self.dirs_page[chat_id] = 0
                self.dirs_menu[chat_id] = []
                return None
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
        if query.data == "git_cancel":
            await query.edit_message_text("Операция отменена.")
            self.git_pending_ref.pop(chat_id, None)
            self.git_branch_menu.pop(chat_id, None)
            return
        if query.data == "git_pull_cancel":
            await query.edit_message_text("Pull отменен.")
            self.git_pull_target.pop(chat_id, None)
            return
        if query.data.startswith("git_") or query.data.startswith("gitpull_") or query.data.startswith("git_conflict"):
            session = await self._ensure_git_session(chat_id, context)
            if not session:
                return
            if not await self._ensure_git_repo(session, chat_id, context):
                return
            if query.data not in ("git_conflict_agent",) and not await self._ensure_git_not_busy(session, chat_id, context):
                return
            if query.data == "git_status":
                await query.edit_message_text("Получаю git status…")
                text = await self._git_status_text(session)
                await self._send_message(context, chat_id=chat_id, text=text)
                return
            if query.data == "git_fetch":
                await query.edit_message_text("Выполняю git fetch…")
                session.git_busy = True
                try:
                    code, output = await self._run_git(session, ["fetch", "--prune"])
                    await self._send_git_output(context, chat_id, "Fetch", output)
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_message(context, chat_id=chat_id, text=status)
                finally:
                    session.git_busy = False
                return
            if query.data == "git_pull":
                await query.edit_message_text("Проверяю возможность fast-forward…")
                session.git_busy = True
                try:
                    await self._run_git(session, ["fetch", "--prune"])
                    branch = await self._git_current_branch(session)
                    upstream = await self._git_upstream(session)
                    if not upstream and branch and branch != "HEAD":
                        candidate = f"origin/{branch}"
                        if await self._git_ref_exists(session, candidate):
                            upstream = candidate
                    if not upstream:
                        upstream = await self._git_default_remote(session)
                    if not upstream:
                        await self._send_message(
                            context,
                            chat_id=chat_id,
                            text="Upstream не найден. Настройте upstream или выберите ветку через Merge/Rebase.",
                        )
                        return
                    ahead_behind = await self._git_ahead_behind(session, upstream)
                    if not ahead_behind:
                        await self._send_message(
                            context,
                            chat_id=chat_id,
                            text="Не удалось определить ahead/behind. Проверьте состояние репозитория.",
                        )
                        return
                    ahead, behind = ahead_behind
                    if behind == 0 and ahead == 0:
                        await self._send_message(context, chat_id=chat_id, text="Ветка уже актуальна.")
                        return
                    if behind == 0 and ahead > 0:
                        await self._send_message(
                            context,
                            chat_id=chat_id,
                            text=f"Локальная ветка опережает {upstream} на {ahead}.",
                        )
                        return
                    if ahead == 0 and behind > 0:
                        code, output = await self._run_git(session, ["pull", "--ff-only"])
                        if code == 0:
                            await self._send_git_output(context, chat_id, "Pull --ff-only", output)
                            status = await self._git_status_text(session)
                            await self._send_message(context, chat_id=chat_id, text=status)
                            return
                    self.git_pull_target[chat_id] = upstream
                    text = f"Fast-forward невозможен. Ahead {ahead} / Behind {behind} относительно {upstream}."
                    await self._send_message(
                        context,
                        chat_id=chat_id,
                        text=text,
                        reply_markup=self._build_git_pull_keyboard(upstream),
                    )
                    return
                finally:
                    session.git_busy = False
            if query.data == "git_pull_merge":
                ref = self.git_pull_target.get(chat_id)
                if not ref:
                    await query.edit_message_text("Не выбран upstream для merge.")
                    return
                await query.edit_message_text(f"Запускаю merge {ref}…")
                await self._execute_merge_rebase(session, chat_id, context, "merge", ref)
                return
            if query.data == "git_pull_rebase":
                ref = self.git_pull_target.get(chat_id)
                if not ref:
                    await query.edit_message_text("Не выбран upstream для rebase.")
                    return
                await query.edit_message_text(f"Запускаю rebase на {ref}…")
                await self._execute_merge_rebase(session, chat_id, context, "rebase", ref)
                return
            if query.data == "git_merge_menu":
                await query.edit_message_text("Список веток для merge…")
                code, output = await self._run_git(session, ["branch", "-r"])
                if code != 0:
                    await self._send_message(context, chat_id=chat_id, text="Не удалось получить список веток.")
                    return
                branches = [b.strip() for b in output.splitlines() if b.strip() and "->" not in b]
                if not branches:
                    await self._send_message(context, chat_id=chat_id, text="Удаленных веток не найдено.")
                    return
                self.git_branch_menu[chat_id] = branches
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text="Выберите ветку для merge:",
                    reply_markup=self._build_git_branches_keyboard(chat_id, "merge"),
                )
                return
            if query.data == "git_rebase_menu":
                await query.edit_message_text("Список веток для rebase…")
                code, output = await self._run_git(session, ["branch", "-r"])
                if code != 0:
                    await self._send_message(context, chat_id=chat_id, text="Не удалось получить список веток.")
                    return
                branches = [b.strip() for b in output.splitlines() if b.strip() and "->" not in b]
                if not branches:
                    await self._send_message(context, chat_id=chat_id, text="Удаленных веток не найдено.")
                    return
                self.git_branch_menu[chat_id] = branches
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text="Выберите ветку для rebase:",
                    reply_markup=self._build_git_branches_keyboard(chat_id, "rebase"),
                )
                return
            if query.data.startswith("git_merge_pick:") or query.data.startswith("git_rebase_pick:"):
                action = "merge" if query.data.startswith("git_merge_pick:") else "rebase"
                idx = int(query.data.split(":", 1)[1])
                branches = self.git_branch_menu.get(chat_id, [])
                if idx < 0 or idx >= len(branches):
                    await query.edit_message_text("Выбор недоступен.")
                    return
                ref = branches[idx]
                ahead_behind = await self._git_ahead_behind(session, ref)
                if ahead_behind:
                    ahead, behind = ahead_behind
                    info = f"Ahead {ahead} / Behind {behind} относительно {ref}."
                else:
                    info = f"Не удалось определить ahead/behind относительно {ref}."
                self.git_pending_ref[chat_id] = ref
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=info,
                    reply_markup=self._build_git_confirm_keyboard(action, ref),
                )
                return
            if query.data == "git_confirm_merge" or query.data == "git_confirm_rebase":
                action = "merge" if query.data == "git_confirm_merge" else "rebase"
                ref = self.git_pending_ref.get(chat_id)
                if not ref:
                    await query.edit_message_text("Не выбрана ветка.")
                    return
                await query.edit_message_text(f"Запускаю {action} {ref}…")
                await self._execute_merge_rebase(session, chat_id, context, action, ref)
                self.git_pending_ref.pop(chat_id, None)
                return
            if query.data == "git_diff":
                await query.edit_message_text("Показываю git diff…")
                code, output = await self._run_git(session, ["--no-pager", "diff", "--stat"])
                if code != 0:
                    await self._send_message(context, chat_id=chat_id, text="Не удалось получить diff.")
                    return
                await self._send_git_output(context, chat_id, "Diff", output)
                return
            if query.data == "git_log":
                await query.edit_message_text("Показываю git log…")
                code, output = await self._run_git(
                    session, ["--no-pager", "log", "--oneline", "--decorate", "-n", "20"]
                )
                if code != 0:
                    await self._send_message(context, chat_id=chat_id, text="Не удалось получить log.")
                    return
                await self._send_git_output(context, chat_id, "Log", output)
                return
            if query.data == "git_stash":
                await query.edit_message_text("Выполняю git stash…")
                session.git_busy = True
                try:
                    code, output = await self._run_git(session, ["stash", "push"])
                    await self._send_git_output(context, chat_id, "Stash", output)
                    if code == 0:
                        status = await self._git_status_text(session)
                        await self._send_message(context, chat_id=chat_id, text=status)
                finally:
                    session.git_busy = False
                return
            if query.data == "git_conflict_diff":
                await query.edit_message_text("Показываю diff конфликтов…")
                code, output = await self._run_git(session, ["--no-pager", "diff"])
                if code != 0:
                    await self._send_message(context, chat_id=chat_id, text="Не удалось получить diff.")
                    return
                await self._send_git_output(context, chat_id, "Diff", output)
                return
            if query.data == "git_conflict_abort":
                await query.edit_message_text("Пробую выполнить abort…")
                mode = await self._git_in_progress(session)
                if mode == "rebase":
                    code, output = await self._run_git(session, ["rebase", "--abort"])
                elif mode == "merge":
                    code, output = await self._run_git(session, ["merge", "--abort"])
                else:
                    await self._send_message(context, chat_id=chat_id, text="Нет активного merge/rebase.")
                    return
                await self._send_git_output(context, chat_id, "Abort", output)
                await self._git_conflict_files(session)
                if not session.git_conflict:
                    self._git_clear_conflict(session)
                return
            if query.data == "git_conflict_continue":
                await query.edit_message_text("Пробую выполнить continue…")
                mode = await self._git_in_progress(session)
                if mode == "rebase":
                    code, output = await self._run_git(session, ["rebase", "--continue"])
                elif mode == "merge":
                    code, output = await self._run_git(session, ["merge", "--continue"])
                else:
                    await self._send_message(context, chat_id=chat_id, text="Нет активного merge/rebase.")
                    return
                await self._send_git_output(context, chat_id, "Continue", output)
                conflicts = await self._git_conflict_files(session)
                if conflicts:
                    await self._handle_git_conflict(session, chat_id, context)
                return
            if query.data == "git_conflict_agent":
                files = session.git_conflict_files or await self._git_conflict_files(session)
                files_text = ", ".join(files[:10]) if files else "нет файлов"
                note = (
                    "Нужна помощь с git-конфликтами. "
                    f"Список файлов: {files_text}. "
                    "Пожалуйста, предложи шаги для разрешения конфликтов и команды git."
                )
                await self._handle_cli_input(session, note, chat_id, context)
                if session.busy or session.is_active_by_tick():
                    await query.edit_message_text("Сессия занята. Выберите действие для очереди.")
                else:
                    await query.edit_message_text("Инструкция отправлена агенту.")
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

    async def cmd_git(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        session = await self._ensure_git_session(chat_id, context)
        if not session:
            return
        if not await self._ensure_git_repo(session, chat_id, context):
            return
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Git-операции:",
            reply_markup=self._build_git_keyboard(),
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
        await self._send_message(context, 
            chat_id=chat_id,
            text="Выберите инструмент для просмотра /команд:",
            reply_markup=keyboard,
        )

    async def _send_dirs_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE, base: str) -> None:
        allow_empty = self.dirs_mode.get(chat_id) == "git_clone"
        err = self._prepare_dirs(chat_id, base, allow_empty=allow_empty)
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
            "git": {"desc": "Git-операции по активной сессии (inline-меню).", "quick": True},
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
            "git",
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
    app.add_handler(CommandHandler("git", bot_app.cmd_git))
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
