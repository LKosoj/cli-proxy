from __future__ import annotations

import datetime
import logging
from typing import Any, Optional, Sequence

from app.services.logging_service import build_session_log_context


def log_cli_dialog(
    session: Any,
    prompt: str,
    output: str,
    *,
    chat_id: Optional[int] = None,
    image_path: Optional[str] = None,
    image_paths: Optional[Sequence[str]] = None,
) -> None:
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        req = (prompt or "").replace("\n", "\\n")

        tool_name = (getattr(getattr(session, "tool", None), "name", "") or "").strip().lower()
        if tool_name == "codex" and image_paths:
            joined = ", ".join(str(p) for p in image_paths if str(p).strip())
            if joined:
                req = f"{req} [images: {joined}]" if req else f"[images: {joined}]"
        elif tool_name == "codex" and image_path:
            req = f"{req} [image: {image_path}]" if req else f"[image: {image_path}]"

        resp = (output or "").replace("\n", "\\n")
        cli_name = (getattr(getattr(session, "tool", None), "name", "") or "cli").strip() or "cli"
        user_tag = f"user:{chat_id}" if chat_id is not None else "user"
        line1 = f"[{ts}][{user_tag}][{req}]"
        line2 = f"[{ts}][{cli_name}][{resp}]"
        logging.getLogger("bot.cli_dialog").info(
            "%s\n%s",
            line1,
            line2,
            extra=build_session_log_context(session=session, chat_id=chat_id),
        )
    except Exception:
        logging.getLogger(__name__).exception("tool failed cli dialog log write")
