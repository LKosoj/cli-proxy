"""Files-menu callback actions."""

import io
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.session_files_service import FileTypeError, FilesServiceError, PathValidationError
from tg.files_service_adapter import files_rel_path, resolve_files_payload, session_files_service, session_uid_for_files


class FileActionsMixin:
    def _ui_key(self, chat_id: int, query):
        return self.bot_app.telegram_ui_key_from_query(query) or self.bot_app.telegram_ui_key(int(chat_id))

    def _current_scope(self, chat_id: int, query):
        resolver = getattr(self.bot_app, "resolve_telegram_callback_scope", None)
        if callable(resolver):
            reply_chat_id, thread_id, owner_chat_id, session = resolver(query)
            return reply_chat_id, thread_id, owner_chat_id, session
        return chat_id, None, chat_id, None

    def _current_scope_session(self, chat_id: int, query):
        _reply_chat_id, _thread_id, _owner_chat_id, session = self._current_scope(chat_id, query)
        return session

    @staticmethod
    def _path_validation_text(exc: FilesServiceError, *, default: str) -> str:
        if isinstance(exc, PathValidationError) and "escapes" in str(exc):
            return "Нельзя выйти за пределы рабочей директории."
        return default

    async def _cb_file_save_here(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        target_dir = self.bot_app.ui_state.files_dir.get(ui_key, session.workdir)
        session_uid = session_uid_for_files(owner_chat_id, session)
        rel_path = files_rel_path(session, target_dir)
        try:
            meta = await resolve_files_payload(
                session_files_service(self.bot_app).meta(
                    owner_chat_id,
                    session_uid,
                    rel_path,
                    protect_sensitive=False,
                )
            )
        except FilesServiceError as exc:
            await self._edit_msg(context, query, self._path_validation_text(exc, default="Каталог недоступен."))
            return True
        if not bool(meta.get("is_dir")):
            await self._edit_msg(context, query, "Каталог недоступен.")
            return True
        self.bot_app._start_files_upload_wait(
            chat_id,
            target_dir,
            session.workdir,
            context,
            message_thread_id=ui_key.message_thread_id,
        )
        await self.bot_app._send_files_menu(
            chat_id,
            session,
            context,
            edit_message=query,
            message_thread_id=ui_key.message_thread_id,
        )
        return True

    async def _cb_file_pick(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        idx = int(str(data).split(":", 1)[1])
        items = self.bot_app.ui_state.files_entries.get(ui_key, [])
        if idx < 0 or idx >= len(items):
            await self._edit_msg(context, query, "Файл не найден.")
            return True
        item = items[idx]
        path = item.get("path") if isinstance(item, dict) else item
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        session_uid = session_uid_for_files(owner_chat_id, session)
        rel_path = item.get("rel_path") if isinstance(item, dict) else None
        rel_path = str(rel_path or files_rel_path(session, path))
        try:
            payload = await resolve_files_payload(
                session_files_service(self.bot_app).download(
                    owner_chat_id,
                    session_uid,
                    rel_path,
                    allow_binary=True,
                    max_size_bytes=45 * 1024 * 1024,
                    protect_sensitive=False,
                )
            )
        except FileTypeError:
            await self._edit_msg(context, query, "Файл слишком большой для отправки.")
            return True
        except FilesServiceError as exc:
            await self._edit_msg(context, query, self._path_validation_text(exc, default="Файл не найден."))
            return True
        filename = str(payload.get("filename") or os.path.basename(str(path or "")) or "download.txt")
        await self._edit_msg(context, query, f"Отправляю файл: {filename}")
        try:
            document = io.BytesIO(bytes(payload.get("content") or b""))
            document.name = filename
            ok = await self.bot_app._send_document(context, document=document, **ui_key.reply_kwargs())
            if not ok:
                await self._edit_msg(context, query, "Ошибка отправки файла. Проверьте логи бота.")
        except Exception as e:
            logging.exception(f"Ошибка отправки файла из меню: {e}")
            await self._edit_msg(context, query, "Ошибка отправки файла. Проверьте логи бота.")
        return True

    async def _cb_file_nav(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        action = str(data).split(":", 1)[1]
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        if action == "cancel":
            self.bot_app._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
            self.bot_app._stop_files_rename_wait(chat_id, message_thread_id=ui_key.message_thread_id)
            await self._edit_msg(context, query, "Операция отменена.")
            return True
        if action.startswith("open:"):
            idx = int(action.split(":", 1)[1])
            entries = self.bot_app.ui_state.files_entries.get(ui_key, [])
            if idx < 0 or idx >= len(entries):
                await self._edit_msg(context, query, "Папка не найдена.")
                return True
            entry = entries[idx]
            path = entry.get("path") if isinstance(entry, dict) else None
            rel_path = entry.get("rel_path") if isinstance(entry, dict) else None
            rel_path = str(rel_path or files_rel_path(session, path))
            try:
                meta = await resolve_files_payload(
                    session_files_service(self.bot_app).meta(
                        owner_chat_id,
                        session_uid_for_files(owner_chat_id, session),
                        rel_path,
                        protect_sensitive=False,
                    )
                )
            except FilesServiceError as exc:
                await self._edit_msg(context, query, self._path_validation_text(exc, default="Папка не найдена."))
                return True
            if not bool(meta.get("is_dir")):
                await self._edit_msg(context, query, "Папка не найдена.")
                return True
            self.bot_app.ui_state.files_dir[ui_key] = path
            self.bot_app.ui_state.files_page[ui_key] = 0
            await self.bot_app._send_files_menu(
                chat_id,
                session,
                context,
                edit_message=query,
                message_thread_id=ui_key.message_thread_id,
            )
            return True
        if action == "up":
            current = self.bot_app.ui_state.files_dir.get(ui_key, session.workdir)
            root = session.workdir
            if os.path.abspath(current) == os.path.abspath(root):
                await self._edit_msg(context, query, "Уже в корне рабочей директории.")
                return True
            parent = os.path.dirname(current)
            rel_path = files_rel_path(session, parent)
            try:
                meta = await resolve_files_payload(
                    session_files_service(self.bot_app).meta(
                        owner_chat_id,
                        session_uid_for_files(owner_chat_id, session),
                        rel_path,
                        protect_sensitive=False,
                    )
                )
            except FilesServiceError as exc:
                await self._edit_msg(context, query, self._path_validation_text(exc, default="Папка не найдена."))
                return True
            if not bool(meta.get("is_dir")):
                await self._edit_msg(context, query, "Папка не найдена.")
                return True
            self.bot_app.ui_state.files_dir[ui_key] = parent
            self.bot_app.ui_state.files_page[ui_key] = 0
            await self.bot_app._send_files_menu(
                chat_id,
                session,
                context,
                edit_message=query,
                message_thread_id=ui_key.message_thread_id,
            )
            return True
        if action == "prev":
            page = max(0, self.bot_app.ui_state.files_page.get(ui_key, 0) - 1)
            self.bot_app.ui_state.files_page[ui_key] = page
            await self.bot_app._send_files_menu(
                chat_id,
                session,
                context,
                edit_message=query,
                message_thread_id=ui_key.message_thread_id,
            )
            return True
        if action == "next":
            page = self.bot_app.ui_state.files_page.get(ui_key, 0) + 1
            self.bot_app.ui_state.files_page[ui_key] = page
            await self.bot_app._send_files_menu(
                chat_id,
                session,
                context,
                edit_message=query,
                message_thread_id=ui_key.message_thread_id,
            )
            return True
        return True

    async def _cb_file_del(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        idx = int(str(data).split(":", 1)[1])
        entries = self.bot_app.ui_state.files_entries.get(ui_key, [])
        if idx < 0 or idx >= len(entries):
            await self._edit_msg(context, query, "Элемент не найден.")
            return True
        entry = entries[idx]
        path = entry.get("path") if isinstance(entry, dict) else None
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        if not path:
            await self._edit_msg(context, query, "Элемент не найден.")
            return True
        rel_path = files_rel_path(session, path)
        try:
            await resolve_files_payload(
                session_files_service(self.bot_app).meta(
                    owner_chat_id,
                    session_uid_for_files(owner_chat_id, session),
                    rel_path,
                    protect_sensitive=False,
                )
            )
        except FilesServiceError as exc:
            await self._edit_msg(context, query, self._path_validation_text(exc, default="Элемент не найден."))
            return True
        name = os.path.basename(path)
        self.bot_app.ui_state.files_pending_delete[ui_key] = path
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да", callback_data="file_del_confirm"),
                    InlineKeyboardButton("❌ Отмена", callback_data="file_del_cancel"),
                ]
            ]
        )
        await self._edit_msg(context, query, f"Удалить {name}? Подтвердите:", reply_markup=keyboard)
        return True

    async def _cb_file_rename(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        idx = int(str(data).split(":", 1)[1])
        entries = self.bot_app.ui_state.files_entries.get(ui_key, [])
        if idx < 0 or idx >= len(entries):
            await self._edit_msg(context, query, "Элемент не найден.")
            return True
        entry = entries[idx]
        path = entry.get("path") if isinstance(entry, dict) else None
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        if not path:
            await self._edit_msg(context, query, "Элемент не найден.")
            return True
        rel_path = files_rel_path(session, path)
        try:
            await resolve_files_payload(
                session_files_service(self.bot_app).meta(
                    owner_chat_id,
                    session_uid_for_files(owner_chat_id, session),
                    rel_path,
                    protect_sensitive=False,
                )
            )
        except FilesServiceError as exc:
            await self._edit_msg(context, query, self._path_validation_text(exc, default="Элемент не найден."))
            return True
        self.bot_app._start_files_rename_wait(
            chat_id,
            path,
            session.workdir,
            context,
            message_thread_id=ui_key.message_thread_id,
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена", callback_data="file_rename_cancel")]]
        )
        await self._edit_msg(
            context,
            query,
            "Введите новое имя (без пути). Таймаут 2 минуты.",
            reply_markup=keyboard,
        )
        return True

    async def _cb_file_del_current(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        current = self.bot_app.ui_state.files_dir.get(ui_key, session.workdir)
        root = session.workdir
        if os.path.abspath(current) == os.path.abspath(root):
            await self._edit_msg(context, query, "Нельзя удалить корневую рабочую директорию.")
            return True
        rel_path = files_rel_path(session, current)
        try:
            meta = await resolve_files_payload(
                session_files_service(self.bot_app).meta(
                    owner_chat_id,
                    session_uid_for_files(owner_chat_id, session),
                    rel_path,
                    protect_sensitive=False,
                )
            )
        except FilesServiceError as exc:
            await self._edit_msg(context, query, self._path_validation_text(exc, default="Папка не найдена."))
            return True
        if not bool(meta.get("is_dir")):
            await self._edit_msg(context, query, "Папка не найдена.")
            return True
        self.bot_app.ui_state.files_pending_delete[ui_key] = current
        name = os.path.basename(current)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Да", callback_data="file_del_confirm"),
                    InlineKeyboardButton("❌ Отмена", callback_data="file_del_cancel"),
                ]
            ]
        )
        await self._edit_msg(context, query, f"Удалить папку {name} рекурсивно? Подтвердите:", reply_markup=keyboard)
        return True

    async def _cb_file_del_confirm(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        _reply_chat_id, _thread_id, owner_chat_id, session = self._current_scope(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        path = self.bot_app.ui_state.files_pending_delete.pop(ui_key, None)
        if not path:
            await self._edit_msg(context, query, "Нет операции удаления.")
            return True
        try:
            await resolve_files_payload(
                session_files_service(self.bot_app).delete(
                    owner_chat_id,
                    session_uid_for_files(owner_chat_id, session),
                    files_rel_path(session, path),
                    recursive=True,
                    protect_sensitive=False,
                )
            )
            await self._edit_msg(context, query, "Удалено.")
        except FilesServiceError as exc:
            if isinstance(exc, PathValidationError) and "escapes" in str(exc):
                await self._edit_msg(context, query, "Нельзя выйти за пределы рабочей директории.")
            else:
                await self._edit_msg(context, query, f"Ошибка удаления: {exc}")
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            await self._edit_msg(context, query, f"Ошибка удаления: {e}")
        current = self.bot_app.ui_state.files_dir.get(ui_key, session.workdir)
        try:
            meta = await resolve_files_payload(
                session_files_service(self.bot_app).meta(
                    owner_chat_id,
                    session_uid_for_files(owner_chat_id, session),
                    files_rel_path(session, current),
                    protect_sensitive=False,
                )
            )
            current_available = bool(meta.get("is_dir"))
        except FilesServiceError:
            current_available = False
        if not current_available:
            current = session.workdir
            self.bot_app.ui_state.files_dir[ui_key] = current
            self.bot_app.ui_state.files_page[ui_key] = 0
        await self.bot_app._send_files_menu(
            chat_id,
            session,
            context,
            edit_message=None,
            message_thread_id=ui_key.message_thread_id,
        )
        return True

    async def _cb_file_del_cancel(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        self.bot_app.ui_state.files_pending_delete.pop(ui_key, None)
        session = self._current_scope_session(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Сессия не определена для текущего scope.")
            return True
        await self._edit_msg(context, query, "Удаление отменено.")
        await self.bot_app._send_files_menu(
            chat_id,
            session,
            context,
            edit_message=None,
            message_thread_id=ui_key.message_thread_id,
        )
        return True

    async def _cb_file_rename_cancel(self, *, data: str, chat_id: int, query, context) -> bool:
        ui_key = self._ui_key(chat_id, query)
        self.bot_app._stop_files_rename_wait(chat_id, message_thread_id=ui_key.message_thread_id)
        session = self._current_scope_session(chat_id, query)
        if not session:
            await self._edit_msg(context, query, "Режим переименования отменен.")
            return True
        await self._edit_msg(context, query, "Режим переименования отменен.")
        await self.bot_app._send_files_menu(
            chat_id,
            session,
            context,
            edit_message=None,
            message_thread_id=ui_key.message_thread_id,
        )
        return True
