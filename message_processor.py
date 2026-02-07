"""
Module containing message processing functionality for the Telegram bot.
"""

import asyncio
import logging
import os
import time
import re
from typing import Optional

from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from session import Session
from handlers import PendingInput


class MessageProcessor:
    """
    Class containing message processing functionality for the Telegram bot.
    """

    def __init__(self, bot_app):
        self.bot_app = bot_app

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

    async def process_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        text = update.message.text if update.message else None
        if not self.bot_app.is_allowed(chat_id):
            return
        self.bot_app.context_by_chat[chat_id] = context
        self.bot_app.metrics.inc("messages")
        if self._has_attachments(update.message):
            return
        if await self.bot_app.session_ui.handle_pending_message(chat_id, text, context):
            return
        if chat_id in self.bot_app.pending_dir_create:
            base = self.bot_app.pending_dir_create.pop(chat_id)
            name = text.strip()
            if name in ("-", "отмена", "Отмена"):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Создание каталога отменено.")
                return
            if not name:
                await self.bot_app._send_message(context, chat_id=chat_id, text="Имя каталога пустое.")
                return
            if not os.path.isdir(base):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Базовый каталог недоступен.")
                return
            if os.path.isabs(name):
                target = os.path.normpath(name)
            else:
                target = os.path.normpath(os.path.join(base, name))
            root = self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)
            if not self.bot_app.is_within_root(target, root):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
                return
            if not self.bot_app.is_within_root(target, base):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Путь должен быть внутри текущего каталога.")
                return
            if os.path.exists(target):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог уже существует.")
                return
            try:
                os.makedirs(target, exist_ok=False)
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self.bot_app._send_message(context, chat_id=chat_id, text=f"Не удалось создать каталог: {e}")
                return
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Каталог создан: {target}")
            await self.bot_app._send_dirs_menu(chat_id, context, base)
            return
        if self.bot_app.pending_dir_input.pop(chat_id, None):
            mode = self.bot_app.dirs_mode.get(chat_id, "new_session")
            path = text.strip()
            if not os.path.isdir(path):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог не существует.")
                return
            root = self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)
            if not self.bot_app.is_within_root(path, root):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
                return
            if mode == "agent_project":
                session_id = self.bot_app.pending_agent_project.pop(chat_id, None)
                session = self.bot_app.manager.get(session_id) if session_id else None
                if not session:
                    await self.bot_app._send_message(context, chat_id=chat_id, text="Активная сессия не найдена.")
                    return
                ok, msg = self.bot_app._set_agent_project_root(session, chat_id, context, path)
                self.bot_app.dirs_mode.pop(chat_id, None)
                await self.bot_app._send_message(context, chat_id=chat_id, text=msg if ok else "Не удалось подключить проект.")
                return
            tool = self.bot_app.pending_new_tool.get(chat_id)
            if not tool:
                await self.bot_app._send_message(context, chat_id=chat_id, text="Инструмент не выбран.")
                return
            session = self.bot_app.manager.create(tool, path)
            self.bot_app.pending_new_tool.pop(chat_id, None)
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Сессия {session.id} создана и выбрана.")
            return
        if chat_id in self.bot_app.pending_git_clone:
            base = self.bot_app.pending_git_clone.pop(chat_id)
            url = text.strip()
            if not self.bot_app.is_within_root(base, self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Нельзя выйти за пределы корневого каталога.")
                return
            if not os.path.isdir(base):
                await self.bot_app._send_message(context, chat_id=chat_id, text="Каталог не существует.")
                return
            await self.bot_app._send_message(context, chat_id=chat_id, text="Запускаю git clone…")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "clone",
                    url,
                    cwd=base,
                    env=self.bot_app.git.git_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
                output = (out or b"").decode(errors="ignore")
                if proc.returncode == 0:
                    await self.bot_app._send_message(context, chat_id=chat_id, text="Клонирование завершено.")
                    tool = self.bot_app.pending_new_tool.pop(chat_id, None)
                    if tool:
                        repo_path = None
                        match = re.search(r"Cloning into '([^']+)'", output)
                        if match:
                            repo_path = os.path.join(base, match.group(1))
                        if not repo_path:
                            repo_path = self.bot_app._guess_clone_path(url, base)
                        root = self.bot_app.dirs_root.get(chat_id, self.bot_app.config.defaults.workdir)
                        if repo_path and os.path.isdir(repo_path) and self.bot_app.is_within_root(repo_path, root):
                            session = self.bot_app.manager.create(tool, repo_path)
                            self.bot_app.dirs_mode.pop(chat_id, None)
                            await self.bot_app._send_message(
                                context,
                                chat_id=chat_id,
                                text=f"Сессия {session.id} создана и выбрана.",
                            )
                else:
                    await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка git clone:\\n{output[:4000]}")
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self.bot_app._send_message(context, chat_id=chat_id, text=f"Ошибка запуска git clone: {e}")
            return
        if await self.bot_app._plugin_awaiting_input(chat_id):
            # Safety net: if the agent was turned off while a dialog was active,
            # the plugin handler in group -1 won't fire (_AgentEnabledFilter blocks it).
            # Detect this and clean up so the user isn't stuck.
            session = self.bot_app.manager.active()
            if not session or not getattr(session, "agent_enabled", False):
                self.bot_app._cancel_plugin_dialogs(chat_id)
                # Fall through to normal on_message processing below.
            else:
                return
        session = await self.bot_app.ensure_active_session(chat_id, context)
        if not session:
            return

        stripped = text.lstrip()
        if stripped.startswith(">"):
            forwarded = stripped[1:].lstrip()
            if not forwarded.startswith("/"):
                await self.bot_app._send_message(
                    context,
                    chat_id=chat_id,
                    text="После '>' должна идти /команда.",
                )
                return
            await self.bot_app._handle_cli_input(session, forwarded, chat_id, context)
            return
        await self.bot_app._buffer_or_send(session, text, chat_id, context)

    async def process_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        self.bot_app.metrics.inc("messages")
        doc = update.message.document
        if not doc:
            return
        filename = doc.file_name or ""
        lower = filename.lower()
        session = await self.bot_app.ensure_active_session(chat_id, context)
        if not session:
            return
        try:
            file_obj = await context.bot.get_file(doc.file_id)
            data = await file_obj.download_as_bytearray()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Не удалось скачать файл: {e}")
            return
        if lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg") or (doc.mime_type or "").startswith("image/"):
            if doc.file_size and doc.file_size > self.bot_app.config.defaults.image_max_mb * 1024 * 1024:
                await self.bot_app._send_message(
                    context,
                    chat_id=chat_id,
                    text=f"Изображение слишком большое. Лимит {self.bot_app.config.defaults.image_max_mb} МБ.",
                )
                return
            await self.bot_app._flush_buffer(chat_id, session, context)
            caption = (update.message.caption or "").strip()
            await self.bot_app._handle_image_bytes(session, data, filename or "image.jpg", caption, chat_id, context)
            return
        if not (
            lower.endswith(".txt")
            or lower.endswith(".md")
            or lower.endswith(".rst")
            or lower.endswith(".log")
            or lower.endswith(".html")
            or lower.endswith(".htm")
        ):
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text="Поддерживаются только .txt, .md, .rst, .log, .html и .htm.",
            )
            return
        if doc.file_size and doc.file_size > 500 * 1024:
            await self.bot_app._send_message(context, chat_id=chat_id, text="Файл слишком большой. Лимит 500 КБ.")
            return
        await self.bot_app._flush_buffer(chat_id, session, context)
        content = data.decode("utf-8", errors="replace")
        caption = (update.message.caption or "").strip()
        parts = []
        if caption:
            parts.append(caption)
        parts.append(f"===== Вложение: {filename} =====")
        parts.append(content)
        payload = "\n\n".join(parts)
        await self.bot_app._handle_user_input(session, payload, chat_id, context)

    async def process_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not self.bot_app.is_allowed(chat_id):
            return
        self.bot_app.metrics.inc("messages")
        photos = update.message.photo or []
        if not photos:
            return
        session = await self.bot_app.ensure_active_session(chat_id, context)
        if not session:
            return
        await self.bot_app._flush_buffer(chat_id, session, context)
        photo = photos[-1]
        if photo.file_size and photo.file_size > self.bot_app.config.defaults.image_max_mb * 1024 * 1024:
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text=f"Изображение слишком большое. Лимит {self.bot_app.config.defaults.image_max_mb} МБ.",
            )
            return
        try:
            file_obj = await context.bot.get_file(photo.file_id)
            data = await file_obj.download_as_bytearray()
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Не удалось скачать изображение: {e}")
            return
        caption = (update.message.caption or "").strip()
        filename = f"{photo.file_unique_id}.jpg"
        await self.bot_app._handle_image_bytes(session, data, filename, caption, chat_id, context)

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
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text=f"CLI {session.tool.name} текущей сессии не поддерживает работу с изображениями.",
            )
            return
        safe_name = os.path.basename(filename) or "image.jpg"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base_dir = self.bot_app.config.defaults.image_temp_dir
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
            await self.bot_app._send_message(context, chat_id=chat_id, text=f"Не удалось сохранить изображение: {e}")
            return
        prompt = caption.strip()
        await self.bot_app._handle_cli_input(session, prompt, chat_id, context, image_path=image_path)

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
            self.bot_app.pending[chat_id] = PendingInput(session.id, text, dest, image_path=image_path)
            self.bot_app.metrics.inc("queued")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⛔ Отменить текущую", callback_data="cancel_current"),
                        InlineKeyboardButton("📥 В очередь", callback_data="queue_input"),
                    ],
                    [InlineKeyboardButton("❌ Отмена ввода", callback_data="discard_input")],
                ]
            )
            await self.bot_app._send_message(context,
                                             chat_id=chat_id,
                                             text="Сессия занята. Что сделать с вашим вводом?",
                                             reply_markup=keyboard,
                                             )
            return
        asyncio.create_task(self.bot_app.run_prompt(session, text, dest, context))

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
            self.bot_app.pending[chat_id] = PendingInput(session.id, text, dest)
            self.bot_app.metrics.inc("queued")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⛔ Отменить текущую", callback_data="cancel_current"),
                        InlineKeyboardButton("📥 В очередь", callback_data="queue_input"),
                    ],
                    [InlineKeyboardButton("❌ Отмена ввода", callback_data="discard_input")],
                ]
            )
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text="Сессия занята. Что сделать с вашим вводом?",
                reply_markup=keyboard,
            )
            return
        self.bot_app._start_agent_task(session, text, dest, context)

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
            self.bot_app.pending[chat_id] = PendingInput(session.id, text, dest)
            self.bot_app.metrics.inc("queued")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⛔ Отменить текущую", callback_data="cancel_current"),
                        InlineKeyboardButton("📥 В очередь", callback_data="queue_input"),
                    ],
                    [InlineKeyboardButton("❌ Отмена ввода", callback_data="discard_input")],
                ]
            )
            await self.bot_app._send_message(
                context,
                chat_id=chat_id,
                text="Сессия занята. Что сделать с вашим вводом?",
                reply_markup=keyboard,
            )
            return
        self.bot_app._start_manager_task(session, text, dest, context)

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
            if not self.bot_app.message_buffer.get(chat_id):
                await self.bot_app._handle_user_input(session, text, chat_id, context)
                return
            self.bot_app.message_buffer.setdefault(chat_id, []).append(text)
            await self.bot_app._flush_buffer(chat_id, session, context)
            return
        self.bot_app.message_buffer.setdefault(chat_id, []).append(text)
        await self.bot_app._schedule_flush(chat_id, session, context)

    async def _schedule_flush(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        task = self.bot_app.buffer_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
        self.bot_app.buffer_tasks[chat_id] = asyncio.create_task(
            self.bot_app._flush_after_delay(chat_id, session, context)
        )

    async def _flush_after_delay(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            await asyncio.sleep(2)
            await self.bot_app._flush_buffer(chat_id, session, context)
        except asyncio.CancelledError:
            return

    async def _flush_buffer(
        self, chat_id: int, session: Session, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        parts = self.bot_app.message_buffer.get(chat_id, [])
        if not parts:
            return
        self.bot_app.message_buffer[chat_id] = []
        task = self.bot_app.buffer_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
        payload = "\n\n".join(parts)
        await self.bot_app._handle_user_input(session, payload, chat_id, context)
