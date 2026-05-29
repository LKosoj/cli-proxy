from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ConversationScope:
    chat_id: int
    message_thread_id: Optional[int] = None

    @classmethod
    def from_parts(cls, chat_id: Any, message_thread_id: Any = None) -> "ConversationScope":
        if str(chat_id) == "desktop":
            return cls(chat_id=0, message_thread_id=None)
        return cls(
            chat_id=int(chat_id),
            message_thread_id=_optional_int(message_thread_id),
        )

    @classmethod
    def from_payload(cls, chat_id: Any, payload: Any) -> "ConversationScope":
        raw_payload = dict(payload) if isinstance(payload, dict) else {}
        nested_scope = raw_payload.get("conversation_scope")
        if not isinstance(nested_scope, dict):
            nested_scope = {}
        message_thread_id = nested_scope.get("message_thread_id", raw_payload.get("message_thread_id"))
        return cls.from_parts(chat_id=chat_id, message_thread_id=message_thread_id)

    @property
    def session_surface(self) -> str:
        return "thread" if self.message_thread_id is not None else "chat"

    @property
    def session_uid(self) -> str:
        if self.message_thread_id is None:
            return f"chat:{int(self.chat_id)}"
        return f"thread:{int(self.chat_id)}:{int(self.message_thread_id)}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "chat_id": int(self.chat_id),
            "message_thread_id": self.message_thread_id,
            "session_uid": self.session_uid,
            "session_surface": self.session_surface,
        }


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    try:
        return int(value)
    except Exception:
        return None


@dataclass(frozen=True)
class DesktopScope:
    project_slug: str
    session_id: str

    @property
    def session_surface(self) -> str:
        return "desktop"

    @property
    def session_uid(self) -> str:
        return f"desktop:{self.session_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_uid": self.session_uid,
            "session_surface": self.session_surface,
            "project_slug": self.project_slug,
        }
