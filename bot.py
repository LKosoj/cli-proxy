import asyncio
import html
import logging
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
from summary import summarize_text_with_reason
from command_registry import build_command_registry
from dirs_ui import build_dirs_keyboard, prepare_dirs
from session_ui import SessionUI
from git_ops import GitOps
from mtproto_ui import MTProtoUI
from metrics import Metrics
from mcp_bridge import MCPBridge
from state import get_state, load_active_state, update_state, clear_active_state
from toolhelp import get_toolhelp, update_toolhelp
from utils import ansi_to_html, build_preview, has_ansi, is_within_root, make_html_file, strip_ansi


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


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
        self.manager = SessionManager(config)
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
        self.toolhelp_menu: Dict[int, list] = {}
        self.restore_offered: Dict[int, bool] = {}
        self.files_menu: Dict[int, list] = {}
        self.message_buffer: Dict[int, list[str]] = {}
        self.buffer_tasks: Dict[int, asyncio.Task] = {}
        self.session_ui = SessionUI(
            self.config,
            self.manager,
            self._send_message,
            self._format_ts,
            self._short_label,
        )
        self.mtproto = MTProtoUI(self.config, self._send_message)
        self.git = GitOps(
            self.config,
            self.manager,
            self._send_message,
            self._send_document,
            self._short_label,
            self._handle_cli_input,
        )
        self.mcp = MCPBridge(self.config, self)

    def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self.config.telegram.whitelist_chat_ids

    def _setup_logging(self) -> None:
        log_path = self.config.defaults.log_path
        logging.basicConfig(
            filename=log_path,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )

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

    async def send_output(self, session: Session, dest: dict, output: str, context: ContextTypes.DEFAULT_TYPE) -> None:
        summary_error = None
        try:
            summary, summary_error = await asyncio.to_thread(
                summarize_text_with_reason, strip_ansi(output), config=self.config
            )
        except Exception:
            summary = None
            summary_error = "неизвестная ошибка"
        if summary:
            preview = summary
            summary_source = "OpenAI"
        else:
            preview = build_preview(output, self.config.defaults.summary_max_chars)
            summary_source = "preview"
            if summary_error:
                summary_source = f"{summary_source} ({summary_error})"
        header = (
            f"[{session.id}|{session.name or session.tool.name}] Сессия: {session.id} | Инструмент: {session.tool.name}\\n"
            f"Каталог: {session.workdir}\\n"
            f"Длина вывода: {len(output)} символов | Очередь: {len(session.queue)}\\n"
            f"Resume: {'есть' if session.resume_token else 'нет'} | Источник анонса: {summary_source}"
        )
        if dest.get("kind") == "mtproto":
            peer = dest.get("peer")
            if peer is not None:
                # MTProto output handled отдельно для задач с файлом.
                pass
        else:
            chat_id = dest.get("chat_id")
            if chat_id is not None:
                await self._send_message(context, chat_id=chat_id, text=header)
                if preview:
                    await self._send_message(context, chat_id=chat_id, text=preview)

        html_text = ansi_to_html(output)
        path = make_html_file(html_text, self.config.defaults.html_filename_prefix)
        try:
            if dest.get("kind") == "mtproto":
                # MTProto output handled отдельно для задач с файлом.
                pass
            else:
                chat_id = dest.get("chat_id")
                if chat_id is not None:
                    with open(path, "rb") as f:
                        await self._send_document(context, chat_id=chat_id, document=f)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        self.metrics.observe_output(len(output))
        try:
            update_state(
                self.config.defaults.state_path,
                session.tool.name,
                session.workdir,
                session.resume_token,
                preview,
                name=session.name,
            )
        except Exception as e:
            logging.exception("update_state failed: %s", e)
        try:
            self.manager._persist_sessions()
        except Exception as e:
            logging.exception("persist_sessions failed: %s", e)

    async def run_prompt(self, session: Session, prompt: str, dest: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        async with session.run_lock:
            session.busy = True
            session.started_at = time.time()
            session.last_output_ts = session.started_at
            session.last_tick_ts = None
            session.last_tick_value = None
            session.tick_seen = 0
            image_path = dest.get("image_path")
            try:
                output = await session.run_prompt(prompt, image_path=image_path)
                if dest.get("kind") == "mtproto" and dest.get("file_path"):
                    await self._send_mtproto_result(session, dest, output, context, error=None)
                else:
                    await self.send_output(session, dest, output, context)
            except Exception as e:
                logging.exception("run_prompt failed: %s", e)
                if dest.get("kind") == "mtproto" and dest.get("file_path"):
                    await self._send_mtproto_result(session, dest, "", context, error=str(e))
                else:
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
                        logging.exception("persist_sessions failed: %s", e)
                    asyncio.create_task(self.run_prompt(session, next_prompt, next_dest, context))

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
        self.metrics.inc("messages")
        text = update.message.text
        if await self.session_ui.handle_pending_message(chat_id, text, context):
            return
        mtproto_pending = await self.mtproto.consume_pending(chat_id, text, context)
        if mtproto_pending is not None:
            if mtproto_pending.get("cancelled"):
                return
            await self._dispatch_mtproto_task(chat_id, mtproto_pending, context)
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
                await self._send_message(context, chat_id=chat_id, text=f"Не удалось создать каталог: {e}")
                return
            await self._send_message(context, chat_id=chat_id, text=f"Каталог создан: {target}")
            await self._send_dirs_menu(chat_id, context, base)
            return
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
                    env=self.git.git_env(),
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
        if await self.git.handle_pending_commit_message(chat_id, text, context):
            return
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return

        if text.startswith(">"):
            forwarded = text[1:].lstrip()
            await self._handle_cli_input(session, forwarded, chat_id, context)
            return
        await self._buffer_or_send(session, text, chat_id, context)

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
        if not (lower.endswith(".txt") or lower.endswith(".md") or lower.endswith(".rst") or lower.endswith(".log")):
            await self._send_message(context, chat_id=chat_id, text="Поддерживаются только .txt, .md, .rst и .log.")
            return
        if doc.file_size and doc.file_size > 300 * 1024:
            await self._send_message(context, chat_id=chat_id, text="Файл слишком большой. Лимит 300 КБ.")
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
        await self._handle_cli_input(session, payload, chat_id, context)

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

    async def _dispatch_mtproto_task(self, chat_id: int, payload: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        session = await self.ensure_active_session(chat_id, context)
        if not session:
            return
        file_path = self._mtproto_output_path(session.workdir)
        dest = {
            "kind": "mtproto",
            "peer": payload.get("peer"),
            "title": payload.get("title"),
            "chat_id": chat_id,
            "file_path": file_path,
        }
        prompt = self._mtproto_prompt(payload.get("message", ""), file_path)
        await self._send_message(
            context,
            chat_id=chat_id,
            text=f"Задача принята. Ожидаю файл результата: {os.path.basename(file_path)}",
        )
        await self._handle_cli_input(session, prompt, chat_id, context, dest=dest)

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

    async def _buffer_or_send(
        self,
        session: Session,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if len(text) < 3000:
            if self.message_buffer.get(chat_id):
                self.message_buffer[chat_id].append(text)
                await self._flush_buffer(chat_id, session, context)
            else:
                await self._handle_cli_input(session, text, chat_id, context)
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
        await self._handle_cli_input(session, payload, chat_id, context)

    def _mtproto_output_path(self, workdir: str) -> str:
        base = self.config.defaults.mtproto_output_dir
        if os.path.isabs(base):
            out_dir = base
        else:
            out_dir = os.path.join(workdir, base)
        os.makedirs(out_dir, exist_ok=True)
        self._cleanup_mtproto_dir(out_dir)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(out_dir, f"result_{stamp}.md")

    def _mtproto_prompt(self, text: str, file_path: str) -> str:
        return (
            f"{text}\n\n"
            "Сформируй результат в Markdown и сохрани в файл по пути:\n"
            f"{file_path}\n"
            "Если файл существует — перезапиши его. "
            "Не отправляй содержимое файла в stdout."
        )

    async def _send_mtproto_result(
        self,
        session: Session,
        dest: dict,
        output: str,
        context: ContextTypes.DEFAULT_TYPE,
        error: Optional[str] = None,
    ) -> None:
        chat_id = dest.get("chat_id")
        file_path = dest.get("file_path")
        peer = dest.get("peer")
        if not file_path or peer is None:
            if chat_id is not None:
                await self._send_message(context, chat_id=chat_id, text="MTProto: не задан путь результата.")
            return
        content = ""
        file_exists = False
        for _ in range(3):
            if os.path.isfile(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if content:
                        file_exists = True
                        break
                except Exception as e:
                    logging.exception("mtproto read failed: %s", e)
            await asyncio.sleep(0.5)
        if file_exists:
            if len(content) > 12000:
                content = content[:12000]
            err = await self.mtproto.send_text(peer, content)
            if err and chat_id is not None:
                await self._send_message(context, chat_id=chat_id, text=err)
            status = "Результат отправлен в MTProto чат."
        else:
            status = "CLI не создал файл результата."
        if error and file_exists:
            status = "CLI завершился с ошибкой, но файл найден и отправлен."
        if chat_id is not None:
            await self._send_message(context, chat_id=chat_id, text=status)

    def _cleanup_mtproto_dir(self, out_dir: str) -> None:
        try:
            days = int(self.config.defaults.mtproto_cleanup_days)
        except Exception:
            days = 5
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        try:
            for name in os.listdir(out_dir):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(out_dir, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except Exception:
                    continue
        except Exception:
            pass

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
                await query.edit_message_text(f"Ошибка получения help: {e}")
            return
        if query.data.startswith("file_pick:"):
            idx = int(query.data.split(":", 1)[1])
            items = self.files_menu.get(chat_id, [])
            if idx < 0 or idx >= len(items):
                await query.edit_message_text("Файл не найден.")
                return
            path = items[idx]
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
            with open(path, "rb") as f:
                await self._send_document(context, chat_id=chat_id, document=f)
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
        if await self.mtproto.handle_callback(query, chat_id, context):
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
        git_txt = "git: занято" if getattr(s, "git_busy", False) else "git: свободно"
        conflict_txt = ""
        if getattr(s, "git_conflict", False):
            conflict_txt = f" | конфликт: {s.git_conflict_kind or 'да'}"
        run_for = f"{int(now - s.started_at)}с" if s.started_at else "нет"
        last_out = f"{int(now - s.last_output_ts)}с назад" if s.last_output_ts else "нет"
        tick_txt = f"{int(now - s.last_tick_ts)}с назад" if s.last_tick_ts else "нет"
        await self._send_message(context, 
            chat_id=chat_id,
            text=(
                f"Активная сессия: {s.id} ({s.name or s.tool.name}) @ {s.workdir}\\n"
                f"Статус: {busy_txt} | {git_txt}{conflict_txt} | В работе: {run_for}\\n"
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
        await self._send_message(context, 
            chat_id=chat_id,
            text="Выберите инструмент для просмотра /команд:",
            reply_markup=keyboard,
        )

    async def cmd_mtproto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.is_allowed(chat_id):
            return
        if not context.args:
            await self.mtproto.request_task(chat_id, context)
            return
        task = " ".join(context.args).strip()
        if task in ("-", "отмена", "Отмена"):
            await self._send_message(context, chat_id=chat_id, text="Отмена.")
            return
        await self.mtproto.show_menu(chat_id, context)
        self.mtproto.pending_task[chat_id] = task

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
        entries = []
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                try:
                    entries.append((os.path.getmtime(path), path))
                except Exception:
                    continue
        entries.sort(reverse=True)
        items = [p for _, p in entries][:20]
        if not items:
            await self._send_message(context, chat_id=chat_id, text="Файлы не найдены.")
            return
        self.files_menu[chat_id] = items
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._short_label(os.path.basename(p), 60), callback_data=f"file_pick:{i}")]
                for i, p in enumerate(items)
            ]
        )
        await self._send_message(context, 
            chat_id=chat_id,
            text="Выберите файл для отправки:",
            reply_markup=keyboard,
        )

    def _preset_commands(self) -> Dict[str, str]:
        if self.config.presets:
            return {p.name: p.prompt for p in self.config.presets}
        return {
            "tests": "Запусти тесты и дай краткий отчёт.",
            "lint": "Запусти линтер/форматтер и дай краткий отчёт.",
            "build": "Запусти сборку и дай краткий отчёт.",
            "refactor": "Сделай небольшой рефакторинг по месту и объясни изменения.",
        }

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
            html_text = ansi_to_html(content)
            if suffix not in strip_ansi(content):
                html_text = f"{html_text}<br><br>{html.escape(suffix)}"
            path = make_html_file(html_text, "toolhelp")
            try:
                with open(path, "rb") as f:
                    await self._send_document(context, chat_id=chat_id, document=f)
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass

def build_app(config: AppConfig) -> Application:
    app = Application.builder().token(config.telegram.token).build()
    bot_app = BotApp(config)

    async def _post_init(application: Application) -> None:
        await bot_app.set_bot_commands(application)
        await bot_app.mcp.start()

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        msg = str(err)
        if "ConnectError" in msg or "NetworkError" in msg or "TimedOut" in msg:
            print("Сеть недоступна или Telegram API не резолвится. Проверьте интернет/DNS/доступ к api.telegram.org.")
            return
        print(f"Ошибка бота: {err}")

    for entry in build_command_registry(bot_app):
        async def _wrap(update: Update, context: ContextTypes.DEFAULT_TYPE, _handler=entry["handler"]) -> None:
            chat_id = update.effective_chat.id
            if not bot_app.is_allowed(chat_id):
                return
            bot_app.metrics.inc("commands")
            await _handler(update, context)
        app.add_handler(CommandHandler(entry["name"], _wrap))

    app.add_handler(CallbackQueryHandler(bot_app.on_callback))
    app.add_handler(MessageHandler(filters.COMMAND, bot_app.on_unknown_command))
    app.add_handler(MessageHandler(filters.PHOTO, bot_app.on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bot_app.on_document))
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
