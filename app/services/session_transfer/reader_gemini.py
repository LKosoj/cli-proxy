"""Read Gemini CLI session JSON files into CanonicalSession."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import List, Optional, Set

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)


def _gemini_tmp_base_dir() -> Path:
    return Path.home() / ".gemini" / "tmp"


def _project_hash(workdir: str) -> str:
    raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate_project_dirs(workdir: str) -> List[Path]:
    base = _gemini_tmp_base_dir()
    if not base.is_dir():
        return []
    phash = _project_hash(workdir)
    project_name = os.path.basename(os.path.realpath(workdir).rstrip(os.sep))
    dirs: List[Path] = []
    seen: Set[str] = set()
    for child in base.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if (
            name == phash
            or name.casefold() == project_name.casefold()
            or name.casefold().startswith(f"{project_name.casefold()}-")
        ):
            key = str(child.resolve())
            if key not in seen:
                seen.add(key)
                dirs.append(child)
    return dirs


def _find_session_file(session_id: str, workdir: str) -> Optional[Path]:
    sid = session_id.strip()
    if not sid:
        return None
    phash = _project_hash(workdir)
    for project_dir in _candidate_project_dirs(workdir):
        chats = project_dir / "chats"
        if not chats.is_dir():
            continue
        candidate = chats / f"session-{sid}.json"
        if candidate.is_file():
            # Verify project hash matches.
            payload = _load_payload(candidate)
            if payload and str(payload.get("projectHash") or "").strip() == phash:
                return candidate
        # Also try without "session-" prefix in case token contains it.
        candidate2 = chats / f"{sid}.json"
        if candidate2.is_file():
            payload = _load_payload(candidate2)
            if payload and str(payload.get("projectHash") or "").strip() == phash:
                return candidate2
    return None


def _load_payload(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    try:
        data = loads_safe(text, strict_first=True)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


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
    """Read a Gemini CLI session and return a CanonicalSession."""
    path = _find_session_file(session_id, workspace)
    if path is None:
        logger.warning("gemini reader: session file not found for %s in %s", session_id, workspace)
        return None

    payload = _load_payload(path)
    if not isinstance(payload, dict):
        logger.warning("gemini reader: invalid payload in %s", path)
        return None

    raw_messages = payload.get("messages", [])
    if not isinstance(raw_messages, list):
        logger.warning("gemini reader: no messages array in %s", path)
        return None

    messages: list[CanonicalMessage] = []
    for raw_msg in raw_messages:
        if not isinstance(raw_msg, dict):
            continue
        ts = _parse_timestamp(raw_msg.get("timestamp"))
        content_text = str(raw_msg.get("content") or "").strip()
        msg_type = str(raw_msg.get("type") or "").strip().lower()

        # Gemini CLI uses "type": "gemini" for assistant messages
        # and "type": "user" for user messages.
        if msg_type == "user":
            if content_text:
                messages.append(CanonicalMessage(role="user", content=content_text, timestamp=ts))
        elif msg_type == "gemini":
            if content_text:
                messages.append(CanonicalMessage(role="assistant", content=content_text, timestamp=ts))
            # Also extract tool calls as tool messages.
            tool_calls = raw_msg.get("toolCalls", [])
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    name = str(tc.get("name") or "").strip()
                    result = tc.get("result")
                    result_text = str(result.get("output") or "") if isinstance(result, dict) else ""
                    if name:
                        messages.append(CanonicalMessage(
                            role="tool",
                            content=f"[{name}] {result_text}".strip(),
                            timestamp=_parse_timestamp(tc.get("timestamp")) or ts,
                        ))

    if not messages:
        logger.warning("gemini reader: no messages extracted from %s", path)
        return None

    return CanonicalSession(
        source_cli="gemini",
        session_id=session_id,
        workspace=workspace,
        messages=messages,
    )
