import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover - optional dependency
    TelegramClient = None
    StringSession = None


class MTProtoUI:
    def __init__(self, config, send_message) -> None:
        self.config = config
        self._send_message = send_message
        self.pending_target: dict[int, int] = {}
        self.pending_task: dict[int, str] = {}
        self._client: Optional["TelegramClient"] = None
        self._client_lock = asyncio.Lock()

    def _targets(self):
        return self.config.mtproto.targets

    def build_menu(self) -> InlineKeyboardMarkup:
        rows = []
        for idx, target in enumerate(self._targets()):
            rows.append([InlineKeyboardButton(target.title, callback_data=f"mt_pick:{idx}")])
        rows.append(
            [
                InlineKeyboardButton("Отмена", callback_data="mt_cancel"),
                InlineKeyboardButton("Закрыть меню", callback_data="mt_close_menu"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    async def request_task(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Введите задание для MTProto (или '-' для отмены).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="mt_cancel")]]
            ),
        )

    async def _get_client(self) -> tuple[Optional["TelegramClient"], Optional[str]]:
        if TelegramClient is None or StringSession is None:
            return None, "Telethon не установлен. Установите пакет telethon."
        cfg = self.config.mtproto
        if not cfg.enabled:
            return None, "MTProto отключен в конфигурации."
        if not cfg.api_id or not cfg.api_hash:
            return None, "Не заданы mtproto.api_id/mtproto.api_hash."
        if self._client:
            return self._client, None
        async with self._client_lock:
            if self._client:
                return self._client, None
            session = (
                StringSession(cfg.session_string)
                if cfg.session_string
                else cfg.session_path
            )
            try:
                client = TelegramClient(session, cfg.api_id, cfg.api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return None, (
                        "MTProto не авторизован. Укажите session_string "
                        "или авторизуйте session_path вручную."
                    )
                self._client = client
                return client, None
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                return None, f"Ошибка MTProto: {e}"

    async def show_menu(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.config.mtproto.enabled:
            await self._send_message(
                context,
                chat_id=chat_id,
                text="MTProto отключен в конфигурации.",
            )
            return
        if not self._targets():
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Нет целей MTProto. Добавьте mtproto.targets в config.yaml.",
            )
            return
        await self._send_message(
            context,
            chat_id=chat_id,
            text="Выберите чат для отправки сообщения:",
            reply_markup=self.build_menu(),
        )

    async def handle_callback(self, query, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        data = query.data or ""
        if data == "mt_cancel":
            self.pending_target.pop(chat_id, None)
            self.pending_task.pop(chat_id, None)
            await query.edit_message_text("Отменено.")
            return True
        if data == "mt_close_menu":
            await query.edit_message_text("MTProto меню закрыто.")
            return True
        if data.startswith("mt_pick:"):
            try:
                idx = int(data.split(":", 1)[1])
            except Exception:
                await query.edit_message_text("Неверный выбор.")
                return True
            targets = self._targets()
            if idx < 0 or idx >= len(targets):
                await query.edit_message_text("Цель не найдена.")
                return True
            self.pending_target[chat_id] = idx
            if chat_id in self.pending_task:
                await query.edit_message_text(f"Выбрана цель «{targets[idx].title}».")
                return True
            await query.edit_message_text(
                f"Введите сообщение для «{targets[idx].title}» (или '-' для отмены)."
            )
            return True
        return False

    async def consume_pending(self, chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
        if chat_id not in self.pending_target:
            return None
        idx = self.pending_target.pop(chat_id)
        message = text.strip()
        if chat_id in self.pending_task:
            message = self.pending_task.pop(chat_id)
        if message in ("-", "отмена", "Отмена"):
            await self._send_message(context, chat_id=chat_id, text="Отправка отменена.")
            return {"cancelled": True}
        targets = self._targets()
        if idx < 0 or idx >= len(targets):
            await self._send_message(context, chat_id=chat_id, text="Цель не найдена.")
            return {"cancelled": True}
        target = targets[idx]
        return {"peer": target.peer, "title": target.title, "message": message}

    async def send_text(self, peer, text: str) -> Optional[str]:
        client, err = await self._get_client()
        if err or client is None:
            return err or "MTProto недоступен."
        try:
            await client.send_message(peer, text)
            return None
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return f"Ошибка MTProto: {e}"

    async def send_file(self, peer, path: str) -> Optional[str]:
        client, err = await self._get_client()
        if err or client is None:
            return err or "MTProto недоступен."
        try:
            await client.send_file(peer, path)
            return None
        except Exception as e:
            logging.exception(f"tool failed {str(e)}")
            return f"Ошибка MTProto: {e}"
