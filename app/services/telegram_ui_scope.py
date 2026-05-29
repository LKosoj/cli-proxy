from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


def _normalize_thread_id(value: Any) -> Optional[int]:
    try:
        thread_id = int(value) if value is not None else 0
    except (TypeError, ValueError):
        thread_id = 0
    return thread_id if thread_id > 0 else None


@dataclass(frozen=True)
class TelegramUiKey:
    chat_id: int
    message_thread_id: Optional[int] = None

    @classmethod
    def from_parts(cls, chat_id: Any, message_thread_id: Any = None) -> "TelegramUiKey":
        return cls(
            chat_id=int(chat_id),
            message_thread_id=_normalize_thread_id(message_thread_id),
        )

    @classmethod
    def from_update(cls, update: Any) -> Optional["TelegramUiKey"]:
        chat = getattr(update, "effective_chat", None)
        if chat is None or getattr(chat, "id", None) is None:
            return None
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        return cls.from_parts(
            getattr(chat, "id"),
            getattr(message, "message_thread_id", None),
        )

    @classmethod
    def from_query(cls, query: Any) -> Optional["TelegramUiKey"]:
        message = getattr(query, "message", None)
        if message is None:
            return None
        raw_chat_id = getattr(message, "chat_id", None)
        if raw_chat_id is None:
            raw_chat_id = getattr(getattr(message, "chat", None), "id", None)
        if raw_chat_id is None:
            return None
        return cls.from_parts(raw_chat_id, getattr(message, "message_thread_id", None))

    @classmethod
    def from_route(cls, route: Any, *, fallback_chat_id: Any) -> "TelegramUiKey":
        return cls.from_parts(
            getattr(route, "reply_chat_id", fallback_chat_id),
            getattr(route, "message_thread_id", None),
        )

    @classmethod
    def from_dest(cls, dest: Optional[dict], *, fallback_chat_id: Any) -> "TelegramUiKey":
        payload = dict(dest or {})
        return cls.from_parts(
            payload.get("chat_id", fallback_chat_id),
            payload.get("message_thread_id"),
        )

    def reply_kwargs(self) -> dict[str, int]:
        kwargs: dict[str, int] = {"chat_id": int(self.chat_id)}
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = int(self.message_thread_id)
        return kwargs
