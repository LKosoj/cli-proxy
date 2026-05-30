from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from ..models import CallbackModel, MessageModel, ToolResult


DialogMessageHandler = Callable[[MessageModel, Dict[str, Any]], Awaitable[ToolResult]]
DialogCallbackHandler = Callable[[CallbackModel, Dict[str, Any]], Awaitable[ToolResult]]
PendingQuestionsProviderFn = Callable[[], Optional[Mapping[str, Mapping[str, Any]]]]


def _dlg_key(chat_id: int, session_id: str, mode_id: str) -> Tuple[str, str, str]:
    return (str(chat_id), str(session_id), str(mode_id))


@dataclass
class DialogState:
    on_message: Optional[DialogMessageHandler] = None
    on_callback: Optional[DialogCallbackHandler] = None
    data: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    timeout_s: int = 300

    def expired(self) -> bool:
        try:
            return (time.time() - float(self.started_at)) > int(self.timeout_s)
        except Exception:
            return True

    def touch(self) -> None:
        self.started_at = time.time()


class DialogService:
    """
    Simple interactive flow router.

    Stores per-(chat_id, session_id, mode_id) dialog handlers and routes
    subsequent messages/callbacks to those handlers.
    """

    def __init__(
        self,
        *,
        pending_questions_provider: Optional[PendingQuestionsProviderFn] = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._dialogs: Dict[Tuple[str, str, str], DialogState] = {}
        self._pending_questions_provider = pending_questions_provider
        self._log = log or logging.getLogger(__name__)

    def start(
        self,
        *,
        chat_id: int,
        session_id: str,
        mode_id: str,
        on_message: Optional[DialogMessageHandler] = None,
        on_callback: Optional[DialogCallbackHandler] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout_s: int = 300,
    ) -> None:
        self._dialogs[_dlg_key(chat_id, session_id, mode_id)] = DialogState(
            on_message=on_message,
            on_callback=on_callback,
            data=data or {},
            timeout_s=int(timeout_s),
        )

    def end(self, *, chat_id: int, session_id: str, mode_id: str) -> None:
        self._dialogs.pop(_dlg_key(chat_id, session_id, mode_id), None)

    def get(self, *, chat_id: int, session_id: str, mode_id: str) -> Optional[DialogState]:
        key = _dlg_key(chat_id, session_id, mode_id)
        st = self._dialogs.get(key)
        if not st:
            return None
        if st.expired():
            self._dialogs.pop(key, None)
            return None
        return st

    def is_active(self, *, chat_id: int, session_id: str, mode_id: str) -> bool:
        return self.get(chat_id=chat_id, session_id=session_id, mode_id=mode_id) is not None

    @staticmethod
    def _session_id_candidates(session: Any, session_id: str = "") -> set[str]:
        candidates = {
            str(session_id or "").strip(),
            str(getattr(session, "id", "") or "").strip(),
            str(getattr(session, "scoped_key", "") or "").strip(),
            str(getattr(session, "session_uid", "") or "").strip(),
        }
        scope = getattr(session, "conversation_scope", None)
        candidates.add(str(getattr(scope, "session_uid", "") or "").strip())
        return {item for item in candidates if item}

    @staticmethod
    def _chat_id_from(session: Any, chat_id: Any = None) -> Optional[str]:
        raw = chat_id
        if raw is None:
            raw = getattr(session, "chat_id", None)
        if raw is None:
            scope = getattr(session, "conversation_scope", None)
            raw = getattr(scope, "chat_id", None)
        if raw is None:
            return None
        token = str(raw or "").strip()
        return token or None

    def _pending_questions_backend(self) -> Mapping[str, Mapping[str, Any]]:
        provider = self._pending_questions_provider
        if not callable(provider):
            self._log.warning("pending questions backend unavailable")
            return {}
        try:
            pending = provider()
        except Exception:
            self._log.exception("pending questions backend read failed")
            return {}
        if pending is None:
            return {}
        if not isinstance(pending, Mapping):
            self._log.warning(
                "pending questions backend returned invalid type type=%s",
                type(pending).__name__,
            )
            return {}
        return pending

    def pending_questions_list(
        self,
        *,
        session: Any = None,
        session_id: str = "",
        chat_id: Any = None,
    ) -> list[Dict[str, Any]]:
        resolved_session_ids = self._session_id_candidates(session, session_id)
        resolved_chat_id = self._chat_id_from(session, chat_id)
        out: list[Dict[str, Any]] = []
        for question_id, meta in self._pending_questions_backend().items():
            if not isinstance(meta, Mapping):
                continue
            meta_session_id = str(meta.get("session_id") or "").strip()
            if resolved_session_ids and meta_session_id not in resolved_session_ids:
                continue
            if resolved_chat_id is not None:
                meta_chat_id = meta.get("chat_id", meta.get("session_uid"))
                if meta_chat_id is not None and str(meta_chat_id or "").strip() != resolved_chat_id:
                    continue
            item = dict(meta)
            item["question_id"] = str(item.get("question_id") or question_id)
            out.append(item)
        return sorted(
            out,
            key=lambda item: (
                float(item.get("created_at") or 0.0),
                str(item.get("question_id") or ""),
            ),
        )

    def pending_questions_count(
        self,
        *,
        session: Any = None,
        session_id: str = "",
        chat_id: Any = None,
    ) -> int:
        return len(self.pending_questions_list(session=session, session_id=session_id, chat_id=chat_id))

    def pending_questions_summary(
        self,
        *,
        session: Any = None,
        session_id: str = "",
        chat_id: Any = None,
    ) -> Dict[str, Any]:
        items = self.pending_questions_list(session=session, session_id=session_id, chat_id=chat_id)
        active_question_id = ""
        if items:
            active_question_id = str(max(items, key=lambda item: float(item.get("created_at") or 0.0)).get("question_id") or "")
        return {
            "count": len(items),
            "awaiting_custom": any(bool(item.get("awaiting_custom", False)) for item in items),
            "active_question_id": active_question_id,
        }

    async def route_message(self, message: MessageModel, ctx: Dict[str, Any], *, session_id: str, mode_id: str) -> ToolResult:
        st = self.get(chat_id=message.chat_id, session_id=session_id, mode_id=mode_id)
        if not st or not st.on_message:
            return ToolResult.fail("no_active_dialog")
        st.touch()
        ctx = dict(ctx)
        ctx["dialog"] = st.data
        return await st.on_message(message, ctx)

    async def route_callback(
        self,
        callback: CallbackModel,
        ctx: Dict[str, Any],
        *,
        session_id: str,
        mode_id: str,
    ) -> ToolResult:
        st = self.get(chat_id=callback.chat_id, session_id=session_id, mode_id=mode_id)
        if not st or not st.on_callback:
            return ToolResult.fail("no_active_dialog")
        st.touch()
        ctx = dict(ctx)
        ctx["dialog"] = st.data
        return await st.on_callback(callback, ctx)
