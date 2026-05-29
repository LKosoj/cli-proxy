"""Read Qwen Code JSONL chat files into CanonicalSession."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import List, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)

QWEN_CHAT_BASE_DIR = Path("/root/.qwen/projects")


def _project_key_candidates(workdir: str) -> List[str]:
    raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
    slash_key = raw.replace(os.sep, "-")
    compact_key = re.sub(r"[^A-Za-z0-9]+", "-", raw)
    if raw.startswith(os.sep) and not compact_key.startswith("-"):
        compact_key = "-" + compact_key
    compact_key = compact_key.rstrip("-")
    out: List[str] = []
    for key in (slash_key, compact_key):
        if key and key not in out:
            out.append(key)
    return out


def _find_session_file(session_id: str, workdir: str) -> Optional[Path]:
    sid = session_id.strip()
    if not sid:
        return None
    for key in _project_key_candidates(workdir):
        candidate = QWEN_CHAT_BASE_DIR / key / "chats" / f"{sid}.jsonl"
        if candidate.is_file():
            return candidate
    # Fallback: scan all project dirs.
    if QWEN_CHAT_BASE_DIR.is_dir():
        for project_dir in QWEN_CHAT_BASE_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / "chats" / f"{sid}.jsonl"
            if candidate.is_file():
                return candidate
    return None


def _parse_timestamp(raw: object) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    try:
        return float(text)
    except Exception:
        return None


def read_session(session_id: str, workspace: str) -> Optional[CanonicalSession]:
    """Read a Qwen Code session and return a CanonicalSession."""
    path = _find_session_file(session_id, workspace)
    if path is None:
        logger.warning("qwen reader: session file not found for %s in %s", session_id, workspace)
        return None

    messages: list[CanonicalMessage] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = loads_safe(line, strict_first=True)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue

                event_type = str(data.get("type") or "").strip()
                ts = _parse_timestamp(data.get("timestamp"))

                if event_type == "user":
                    message = data.get("message", {})
                    parts = message.get("parts", [])
                    texts = []
                    for part in (parts if isinstance(parts, list) else []):
                        if isinstance(part, dict) and "text" in part:
                            t = str(part.get("text") or "").strip()
                            if t:
                                texts.append(t)
                    text = "\n".join(texts)
                    if text:
                        messages.append(CanonicalMessage(role="user", content=text, timestamp=ts))

                elif event_type == "assistant":
                    message = data.get("message", {})
                    parts = message.get("parts", [])
                    texts = []
                    for part in (parts if isinstance(parts, list) else []):
                        if isinstance(part, dict):
                            if part.get("thought"):
                                continue
                            t = str(part.get("text") or "").strip()
                            if t:
                                texts.append(t)
                    text = "\n".join(texts)
                    if text:
                        messages.append(CanonicalMessage(role="assistant", content=text, timestamp=ts))

                elif event_type == "tool_result":
                    tool_result = data.get("toolCallResult", {})
                    result_text = str(tool_result.get("resultDisplay") or "").strip()
                    if result_text:
                        messages.append(CanonicalMessage(role="tool", content=result_text, timestamp=ts))

    except Exception:
        logger.exception("qwen reader: failed to read %s", path)
        if messages:
            return CanonicalSession(
                source_cli="qwen",
                session_id=session_id,
                workspace=workspace,
                messages=messages,
            )
        return None

    if not messages:
        logger.warning("qwen reader: no messages in %s", path)
        return None

    return CanonicalSession(
        source_cli="qwen",
        session_id=session_id,
        workspace=workspace,
        messages=messages,
    )
