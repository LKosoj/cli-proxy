from __future__ import annotations

from typing import Any

from sessions.conversation_scope import DesktopScope


def sanitize_scoped_key_token(value: Any) -> str:
    return str("" if value is None else value).strip().replace("/", "_").replace("\\", "_")


def build_session_scoped_key(chat_id: Any, session_id: Any) -> str:
    sid = sanitize_scoped_key_token(session_id) or "default"
    cid = sanitize_scoped_key_token(chat_id)
    if not cid:
        return sid
    return f"{cid}_{sid}"


def is_session_scoped_key(value: Any) -> bool:
    token = sanitize_scoped_key_token(value)
    if not token or "_" not in token:
        return False
    prefix, suffix = token.split("_", 1)
    return bool(prefix and suffix)


def session_scoped_key(session: Any) -> str:
    explicit = sanitize_scoped_key_token(getattr(session, "scoped_key", ""))
    if explicit and is_session_scoped_key(explicit):
        return explicit
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        return ""
    chat_id = getattr(session, "chat_id", None)
    if chat_id is None:
        scope = getattr(session, "scope", None)
        if scope is None:
            scope = getattr(session, "conversation_scope", None)
        if isinstance(scope, DesktopScope) or str(getattr(scope, "session_surface", "") or "").strip() == "desktop":
            chat_id = 0
        else:
            scope_chat_id = getattr(scope, "chat_id", None)
            if scope_chat_id is not None:
                chat_id = scope_chat_id
    return build_session_scoped_key(chat_id, session_id)
