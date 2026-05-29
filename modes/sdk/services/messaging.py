from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


SendMessageFn = Callable[..., Awaitable[Any]]
EditMessageFn = Callable[..., Awaitable[Any]]
DeleteMessageFn = Callable[..., Awaitable[Any]]
SendDocumentFn = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


@dataclass
class MessagingService:
    """
    Transport-agnostic messaging facade.

    Core should provide callables compatible with the bot's transport layer.
    For this repository, BotApp methods look like:
      - bot._send_message(context, chat_id=..., text=..., md2=..., **kwargs)
      - bot._edit_message(context, chat_id=..., message_id=..., text=..., md2=..., **kwargs)
    """

    send_message: Optional[SendMessageFn] = None
    edit_message: Optional[EditMessageFn] = None
    delete_message: Optional[DeleteMessageFn] = None
    send_document: Optional[SendDocumentFn] = None
    transport_context: Any = None

    @staticmethod
    def _message_id_from_ref(message_ref: Any) -> Optional[int]:
        if message_ref is None:
            return None
        raw_value = getattr(message_ref, "message_id", None)
        if raw_value is None and isinstance(message_ref, dict):
            raw_value = message_ref.get("message_id")
        if raw_value is None:
            return None
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _numeric_log_value(value: Any) -> str:
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return value
        return "" if value is None else "<non-scalar>"

    def _log_progress_failure(
        self,
        operation: str,
        *,
        chat_id: Any = None,
        message_id: Any = None,
        text: Any = None,
        extra_count: int = 0,
        error: BaseException,
    ) -> None:
        text_len = len(str(text)) if text is not None else 0
        logger.error(
            (
                "messaging.progress.failure operation=%s target_id=%s "
                "message_id=%s text_len=%d extra_count=%d error_type=%s"
            ),
            str(operation or ""),
            self._numeric_log_value(chat_id),
            self._numeric_log_value(message_id),
            text_len,
            int(extra_count),
            type(error).__name__,
        )

    async def _send_text_unlogged(self, chat_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        if not self.send_message:
            raise RuntimeError("MessagingService.send_message is not configured")
        return await self.send_message(self.transport_context, chat_id=chat_id, text=str(text), md2=bool(md2), **kwargs)

    async def _edit_text_unlogged(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        md2: bool = True,
        **kwargs: Any,
    ) -> Any:
        if not self.edit_message:
            raise RuntimeError("MessagingService.edit_message is not configured")
        return await self.edit_message(
            self.transport_context,
            chat_id=chat_id,
            message_id=int(message_id),
            text=str(text),
            md2=bool(md2),
            **kwargs,
        )

    async def _remove_unlogged(
        self,
        chat_id: int,
        message_id: Optional[int] = None,
        *,
        message_ref: Any = None,
        **kwargs: Any,
    ) -> Any:
        if not self.delete_message:
            raise RuntimeError("MessagingService.delete_message is not configured")
        target_message_id = message_id
        if target_message_id is None:
            target_message_id = self._message_id_from_ref(message_ref)
        if target_message_id is None:
            raise ValueError("message_id is required")
        return await self.delete_message(self.transport_context, chat_id=chat_id, message_id=int(target_message_id), **kwargs)

    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        try:
            return await self._send_text_unlogged(chat_id, text, md2=md2, **kwargs)
        except Exception as exc:
            self._log_progress_failure(
                "send_text",
                chat_id=chat_id,
                text=text,
                extra_count=len(kwargs),
                error=exc,
            )
            raise

    async def send_plain_text(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        # Boundary helper: production callers use this instead of copying md2=False.
        return await self.send_text(chat_id, text, md2=False, **kwargs)

    async def edit_text(self, chat_id: int, message_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        try:
            return await self._edit_text_unlogged(chat_id, message_id, text, md2=md2, **kwargs)
        except Exception as exc:
            self._log_progress_failure(
                "edit_text",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                extra_count=len(kwargs),
                error=exc,
            )
            raise

    async def send_or_edit(
        self,
        *,
        chat_id: int,
        text: str,
        message_ref: Any = None,
        message_id: Optional[int] = None,
        query: Any = None,
        md2: bool = True,
        **kwargs: Any,
    ) -> Any:
        target_chat_id = chat_id
        target_message_id = message_id
        if target_message_id is None:
            target_message_id = self._message_id_from_ref(message_ref)
        if target_message_id is None and query and getattr(query, "message", None):
            target_chat_id = query.message.chat_id
            target_message_id = query.message.message_id
        try:
            if target_message_id is not None:
                return await self._edit_text_unlogged(target_chat_id, target_message_id, text, md2=md2, **kwargs)
            return await self._send_text_unlogged(chat_id, text, md2=md2, **kwargs)
        except Exception as exc:
            self._log_progress_failure(
                "send_or_edit",
                chat_id=target_chat_id,
                message_id=target_message_id,
                text=text,
                extra_count=len(kwargs),
                error=exc,
            )
            raise

    async def remove(
        self,
        chat_id: int,
        message_id: Optional[int] = None,
        *,
        message_ref: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            return await self._remove_unlogged(chat_id, message_id, message_ref=message_ref, **kwargs)
        except Exception as exc:
            target_message_id = message_id
            if target_message_id is None:
                target_message_id = self._message_id_from_ref(message_ref)
            self._log_progress_failure(
                "remove",
                chat_id=chat_id,
                message_id=target_message_id,
                extra_count=len(kwargs),
                error=exc,
            )
            raise

    async def send_doc(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        if not self.send_document:
            raise RuntimeError("MessagingService.send_document is not configured")
        return await self.send_document(
            self.transport_context,
            chat_id=chat_id,
            document=document,
            **kwargs,
        )
