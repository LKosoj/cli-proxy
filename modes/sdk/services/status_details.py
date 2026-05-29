from __future__ import annotations

from typing import Any, Dict, Optional


def collect_pending_questions_for_session(
    pending_questions: Optional[Dict[str, Dict[str, object]]],
    *,
    session_id: str,
) -> Dict[str, Any]:
    items: list[tuple[str, Dict[str, object]]] = []
    sid = str(session_id or "")
    for qid, meta in (pending_questions or {}).items():
        if not isinstance(meta, dict):
            continue
        if str(meta.get("session_id") or "") != sid:
            continue
        items.append((str(qid), meta))

    count = len(items)
    awaiting_custom = any(bool(meta.get("awaiting_custom", False)) for _, meta in items)
    active_question_id = ""
    if items:
        active_question_id = max(
            items,
            key=lambda item: float(item[1].get("created_at") or 0.0),
        )[0]
    return {
        "count": count,
        "awaiting_custom": awaiting_custom,
        "active_question_id": active_question_id,
    }


def extract_queue_origin(session: Any) -> Dict[str, Any]:
    queue = list(getattr(session, "queue", []) or [])
    if not queue:
        return {
            "has_item": False,
            "kind": "",
            "chat_id": None,
            "user_id": None,
            "text_preview": "",
        }

    head = queue[0]
    if isinstance(head, dict):
        dest = head.get("dest")
        if not isinstance(dest, dict):
            dest = {}
        text = str(head.get("text") or "")
        return {
            "has_item": True,
            "kind": str(dest.get("kind") or "unknown"),
            "chat_id": dest.get("chat_id"),
            "user_id": dest.get("user_id"),
            "text_preview": text[:80],
        }

    text = str(head or "")
    return {
        "has_item": True,
        "kind": "unknown",
        "chat_id": None,
        "user_id": None,
        "text_preview": text[:80],
    }


def format_queue_origin(origin: Dict[str, Any]) -> str:
    if not bool(origin.get("has_item")):
        return "нет"
    parts = [str(origin.get("kind") or "unknown")]
    chat_id = origin.get("chat_id")
    user_id = origin.get("user_id")
    text_preview = str(origin.get("text_preview") or "").strip()
    if chat_id is not None:
        parts.append(f"chat={chat_id}")
    if user_id is not None:
        parts.append(f"user={user_id}")
    if text_preview:
        parts.append(f"text={text_preview}")
    return " | ".join(parts)
