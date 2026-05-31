"""Read Grok Build session files into CanonicalSession."""

from __future__ import annotations

import logging
import os
import urllib.parse
from pathlib import Path
from typing import Optional

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)

GROK_SESSIONS_BASE = Path.home() / ".grok" / "sessions"


def _workspace_key(workdir: str) -> str:
    raw = os.path.realpath(str(workdir or "")).rstrip(os.sep) or str(workdir or "")
    return urllib.parse.quote(raw, safe="") if raw else ""


def _find_session_dir(session_id: str, workdir: str) -> Optional[Path]:
    sid = str(session_id or "").strip()
    key = _workspace_key(workdir)
    if not sid or not key:
        return None
    direct = GROK_SESSIONS_BASE / key / sid
    if direct.is_dir():
        return direct
    if not GROK_SESSIONS_BASE.is_dir():
        return None
    try:
        for project_dir in GROK_SESSIONS_BASE.iterdir():
            candidate = project_dir / sid
            if candidate.is_dir():
                return candidate
    except Exception:
        logger.exception("grok reader: failed to scan sessions base=%s", GROK_SESSIONS_BASE)
    return None


def _load_json(path: Path) -> Optional[dict]:
    try:
        payload = loads_safe(path.read_text(encoding="utf-8", errors="ignore"), strict_first=True)
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("grok reader: failed to read json path=%s", path)
        return None
    return payload if isinstance(payload, dict) else None


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if str(content.get("type") or "").strip() == "text" or "text" in content:
            return str(content.get("text") or "").strip()
        return ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
        elif "text" in item:
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _read_chat_history(path: Path) -> list[CanonicalMessage]:
    messages: list[CanonicalMessage] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = loads_safe(line, strict_first=True)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = str(record.get("type") or "").strip()
                if record_type == "user":
                    if record.get("synthetic_reason"):
                        continue
                    text = _extract_text(record.get("content"))
                    if text:
                        messages.append(CanonicalMessage(role="user", content=text))
                elif record_type == "assistant":
                    text = _extract_text(record.get("content"))
                    if text:
                        messages.append(CanonicalMessage(role="assistant", content=text))
                elif record_type == "tool_result":
                    text = _extract_text(record.get("content"))
                    if text:
                        messages.append(CanonicalMessage(role="tool", content=text))
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("grok reader: failed to read chat history path=%s", path)
    return messages


def _read_updates(path: Path) -> list[CanonicalMessage]:
    messages: list[CanonicalMessage] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = loads_safe(line, strict_first=True)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                params = record.get("params")
                update = params.get("update") if isinstance(params, dict) else None
                if not isinstance(update, dict):
                    continue
                kind = str(update.get("sessionUpdate") or "").strip()
                content = update.get("content")
                text = _extract_text(content)
                if not text:
                    continue
                if kind == "user_message_chunk":
                    messages.append(CanonicalMessage(role="user", content=text))
                elif kind == "agent_message_chunk":
                    messages.append(CanonicalMessage(role="assistant", content=text))
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("grok reader: failed to read updates path=%s", path)
    return messages


def read_session(session_id: str, workspace: str) -> Optional[CanonicalSession]:
    """Read a Grok Build session and return a CanonicalSession."""
    session_dir = _find_session_dir(session_id, workspace)
    if session_dir is None:
        logger.warning("grok reader: session dir not found for %s in %s", session_id, workspace)
        return None

    messages = _read_chat_history(session_dir / "chat_history.jsonl")
    if not messages:
        messages = _read_updates(session_dir / "updates.jsonl")
    if not messages:
        logger.warning("grok reader: no messages in %s", session_dir)
        return None

    summary_payload = _load_json(session_dir / "summary.json") or {}
    summary = str(
        summary_payload.get("session_summary")
        or summary_payload.get("generated_title")
        or ""
    ).strip() or None
    return CanonicalSession(
        source_cli="grok",
        session_id=str(session_id or "").strip(),
        workspace=workspace,
        messages=messages,
        summary=summary,
    )
