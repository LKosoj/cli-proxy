"""Media/files-upload cluster extracted from BotApp.

Handles pending-upload wait, pending-rename wait, and media-group flushing.
State lives in ``bot_app.ui_state`` (ChatUiState); this handler only
reads/writes through that shared object so there is no state duplication.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Optional

from telegram.ext import ContextTypes

from app.services.telegram_ui_scope import TelegramUiKey
from i18n import t
from utils.lang import resolve_user_lang

if TYPE_CHECKING:
    from bot import BotApp

logger = logging.getLogger(__name__)


class FileUploadHandler:
    """Encapsulates the media/files-upload logic that was previously inline in BotApp."""

    def __init__(self, bot_app: "BotApp") -> None:
        self._app = bot_app

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_unique_file_path(self, target_dir: str, file_name: str) -> str:
        safe_name = os.path.basename(file_name.strip()) or "attachment.txt"
        base, ext = os.path.splitext(safe_name)
        if not base:
            base = "attachment"
        path = os.path.join(target_dir, safe_name)
        if not os.path.exists(path):
            return path
        i = 1
        while True:
            candidate = os.path.join(target_dir, f"{base}_{i}{ext}")
            if not os.path.exists(candidate):
                return candidate
            i += 1

    def _stop_files_upload_wait(self, chat_id: int, *, message_thread_id: Optional[int] = None) -> None:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        task = self._app.ui_state.files_pending_upload_tasks.pop(ui_key, None)
        if task and not task.done():
            task.cancel()
        self._app.ui_state.files_pending_upload.pop(ui_key, None)

    def _stop_files_rename_wait(self, chat_id: int, *, message_thread_id: Optional[int] = None) -> None:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        task = self._app.ui_state.files_pending_rename_tasks.pop(ui_key, None)
        if task and not task.done():
            task.cancel()
        self._app.ui_state.files_pending_rename.pop(ui_key, None)

    # ------------------------------------------------------------------
    # Files upload wait
    # ------------------------------------------------------------------

    async def _files_upload_wait_expire(
        self,
        chat_id: int,
        message_thread_id: Optional[int],
        expires_at: float,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        try:
            delay = max(0.0, expires_at - time.time())
            await asyncio.sleep(delay)
            pending = self._app.ui_state.files_pending_upload.get(ui_key)
            if not pending:
                return
            if float(pending.get("expires_at", 0.0)) != expires_at:
                return
            self._app.ui_state.files_pending_upload.pop(ui_key, None)
            self._app.ui_state.files_pending_upload_tasks.pop(ui_key, None)
            lang = resolve_user_lang(self._app.config, chat_id=chat_id)
            await self._app._send_message(
                context,
                **ui_key.reply_kwargs(),
                text=t("msg.files.upload_timeout", lang),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка таймера ожидания сохранения файла.")

    def _start_files_upload_wait(
        self,
        chat_id: int,
        target_dir: str,
        root_dir: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        self._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
        expires_at = time.time() + 120
        self._app.ui_state.files_pending_upload[ui_key] = {
            "dir": os.path.abspath(target_dir),
            "root": os.path.abspath(root_dir),
            "expires_at": expires_at,
        }
        self._app.ui_state.files_pending_upload_tasks[ui_key] = asyncio.create_task(
            self._files_upload_wait_expire(chat_id, ui_key.message_thread_id, expires_at, context)
        )

    # ------------------------------------------------------------------
    # Files rename wait
    # ------------------------------------------------------------------

    async def _files_rename_wait_expire(
        self,
        chat_id: int,
        message_thread_id: Optional[int],
        expires_at: float,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        try:
            delay = max(0.0, expires_at - time.time())
            await asyncio.sleep(delay)
            pending = self._app.ui_state.files_pending_rename.get(ui_key)
            if not pending:
                return
            if float(pending.get("expires_at", 0.0)) != expires_at:
                return
            self._app.ui_state.files_pending_rename.pop(ui_key, None)
            self._app.ui_state.files_pending_rename_tasks.pop(ui_key, None)
            lang = resolve_user_lang(self._app.config, chat_id=chat_id)
            await self._app._send_message(
                context,
                **ui_key.reply_kwargs(),
                text=t("msg.files.rename_timeout", lang),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка таймера ожидания переименования файла.")

    def _start_files_rename_wait(
        self,
        chat_id: int,
        source_path: str,
        root_dir: str,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> None:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        self._stop_files_rename_wait(chat_id, message_thread_id=ui_key.message_thread_id)
        expires_at = time.time() + 120
        self._app.ui_state.files_pending_rename[ui_key] = {
            "path": os.path.abspath(source_path),
            "root": os.path.abspath(root_dir),
            "expires_at": expires_at,
        }
        self._app.ui_state.files_pending_rename_tasks[ui_key] = asyncio.create_task(
            self._files_rename_wait_expire(chat_id, ui_key.message_thread_id, expires_at, context)
        )

    # ------------------------------------------------------------------
    # Pending file save
    # ------------------------------------------------------------------

    async def _maybe_save_pending_uploaded_file(
        self,
        chat_id: int,
        doc: Any,
        data: bytearray,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        ui_key = TelegramUiKey.from_parts(chat_id, message_thread_id)
        pending = self._app.ui_state.files_pending_upload.get(ui_key)
        if not pending:
            return False
        expires_at = float(pending.get("expires_at", 0.0))
        lang = resolve_user_lang(self._app.config, chat_id=chat_id)
        if time.time() > expires_at:
            self._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
            await self._app._send_message(
                context,
                **ui_key.reply_kwargs(),
                text=t("msg.files.upload_expired", lang),
            )
            return False
        target_dir = str(pending.get("dir") or "")
        root_dir = str(pending.get("root") or "")
        if not target_dir or not os.path.isdir(target_dir):
            self._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
            await self._app._send_message(
                context,
                **ui_key.reply_kwargs(),
                text=t("msg.files.upload_dir_unavailable", lang),
            )
            return True
        if not self._app.is_within_root(target_dir, root_dir):
            self._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
            await self._app._send_message(
                context,
                **ui_key.reply_kwargs(),
                text=t("msg.files.upload_dir_outside_root", lang),
            )
            return True
        file_name = getattr(doc, "file_name", None) or "attachment.txt"
        out_path = self._resolve_unique_file_path(target_dir, file_name)
        try:
            with open(out_path, "wb") as f:
                f.write(bytes(data))
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            self._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
            await self._app._send_message(
                context,
                **ui_key.reply_kwargs(),
                text=t("msg.files.save_failed", lang, e=e),
            )
            return True
        self._stop_files_upload_wait(chat_id, message_thread_id=ui_key.message_thread_id)
        await self._app._send_message(
            context,
            **ui_key.reply_kwargs(),
            text=t("msg.files.saved", lang, path=out_path),
        )
        return True

    # ------------------------------------------------------------------
    # Media group flushing
    # ------------------------------------------------------------------

    async def _flush_media_groups_for_chat(
        self,
        chat_id: int,
        exclude_media_group_id: Optional[str] = None,
    ) -> None:
        exclude = str(exclude_media_group_id or "").strip()
        image_keys = [
            key
            for key in list(self._app.ui_state.media_group_images.keys())
            if int(key[0]) == int(chat_id) and (not exclude or str(key[1]) != exclude)
        ]
        for key in image_keys:
            await self._app._flush_media_group(key)
        document_keys = [
            key
            for key in list(self._app.ui_state.media_group_documents.keys())
            if int(key[0]) == int(chat_id) and (not exclude or str(key[1]) != exclude)
        ]
        for key in document_keys:
            await self._app._flush_media_group(key)
