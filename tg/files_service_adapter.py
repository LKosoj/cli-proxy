"""Telegram file-flow helpers for the shared session files service."""

from __future__ import annotations

import inspect
import os
from typing import Any

from app.services.session_files_service import SessionFilesService
from session import Session


def session_files_service(bot_app: Any) -> SessionFilesService:
    service = getattr(bot_app, "session_files_service", None)
    if service is not None:
        return service
    return SessionFilesService(bot_app)


def session_uid_for_files(chat_id: int, session: Session) -> str:
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        return ""
    owner_chat_id = getattr(session, "chat_id", None)
    if owner_chat_id is None:
        owner_chat_id = chat_id
    return f"{int(owner_chat_id)}:{session_id}"


def files_rel_path(session: Session, path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return "."
    root = str(getattr(session, "workdir", "") or "").strip()
    if root and os.path.isabs(raw):
        try:
            raw = os.path.relpath(raw, root)
        except ValueError:
            return raw
    rel = raw.replace("\\", "/")
    return "." if rel in ("", ".") else rel


def files_display_path(session: Session, rel_path: str) -> str:
    rel = str(rel_path or ".").strip()
    root = str(getattr(session, "workdir", "") or "")
    if rel in ("", "."):
        return root
    return os.path.join(root, rel)


async def resolve_files_payload(payload: Any) -> Any:
    if inspect.isawaitable(payload):
        return await payload
    return payload
