import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from telegram.error import NetworkError, TimedOut


RecordMessageFn = Callable[[int, int], None]


@dataclass
class TelegramIO:
    """
    Thin wrapper around python-telegram-bot I/O with retries.

    BotApp keeps the higher-level business logic; this module owns
    transport details (retries, transient network errors).
    """

    record_message: Optional[RecordMessageFn] = None

    async def send_message(self, context: Any, **kwargs):
        for attempt in range(5):
            try:
                message = await context.bot.send_message(**kwargs)
                chat_id = kwargs.get("chat_id")
                if self.record_message and chat_id and message:
                    try:
                        self.record_message(int(chat_id), int(message.message_id))
                    except Exception:
                        pass
                return message
            except (NetworkError, TimedOut):
                if attempt == 4:
                    logging.exception("Ошибка сети при отправке сообщения в Telegram.")
                    return None
                await asyncio.sleep(2 * (2 ** attempt))

    async def send_document(self, context: Any, **kwargs) -> bool:
        for attempt in range(5):
            try:
                await context.bot.send_document(**kwargs)
                return True
            except (NetworkError, TimedOut):
                if attempt == 4:
                    logging.exception("Ошибка сети при отправке файла в Telegram.")
                    return False
                await asyncio.sleep(2 * (2 ** attempt))
            except Exception:
                logging.exception("Не удалось отправить файл в Telegram.")
                return False
        return False

    async def delete_message(self, context: Any, chat_id: int, message_id: int) -> bool:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            return False

    async def edit_message(self, context: Any, chat_id: int, message_id: int, text: str) -> bool:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            return True
        except Exception:
            return False

