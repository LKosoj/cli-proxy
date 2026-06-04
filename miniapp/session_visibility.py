from __future__ import annotations

from typing import Any, Dict

from session import session_runtime_uid


def collect_visible_sessions(
    bot_app: Any, *, user_id: int, is_admin: bool
) -> Dict[str, Any]:
    """Return runtime-uid → session for sessions the caller may see.

    Admin: all sessions across all chats. Non-admin: only sessions of user_id.
    """
    out: Dict[str, Any] = {}
    manager = getattr(bot_app, "manager", None)
    if manager is None:
        return out
    if is_admin:
        by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
        for by_id in by_chat.values():
            if not isinstance(by_id, dict):
                continue
            for session in by_id.values():
                suid = session_runtime_uid(session)
                if suid:
                    out[suid] = session
        return out
    try:
        by_id = dict(manager.sessions_for_chat(int(user_id)) or {})
    except Exception:
        return out
    for session in by_id.values():
        suid = session_runtime_uid(session)
        if suid:
            out[suid] = session
    return out
