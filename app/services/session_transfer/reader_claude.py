"""Read Claude Code JSONL transcripts into CanonicalSession."""

from __future__ import annotations

import logging
import os
import pwd
import re
from pathlib import Path
from typing import List, Optional, Set

from modes.sdk.runtime.json_normalizer import loads_safe

from ._user_paths import home_for_user
from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)

CLAUDE_RUN_USER = "claude-bot"


def _project_key(workdir: str) -> str:
    raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
    compact = re.sub(r"[^A-Za-z0-9]+", "-", raw)
    if raw.startswith(os.sep) and not compact.startswith("-"):
        compact = "-" + compact
    return compact.rstrip("-")


def _candidate_project_dirs(workdir: str) -> List[Path]:
    key = _project_key(workdir)
    if not key:
        return []
    roots: List[Path] = []
    seen: Set[str] = set()
    for home in (Path.home(), home_for_user(CLAUDE_RUN_USER)):
        if home is None:
            continue
        root = home / ".claude" / "projects"
        k = str(root)
        if k not in seen:
            seen.add(k)
            roots.append(root)
    # Also try the user running the bot when HOME was overridden.
    try:
        pw = pwd.getpwuid(os.getuid())
        alt = Path(pw.pw_dir) / ".claude" / "projects"
        ak = str(alt)
        if ak not in seen:
            seen.add(ak)
            roots.append(alt)
    except Exception:
        pass
    return [root / key for root in roots]


def _find_session_file(session_id: str, workdir: str) -> Optional[Path]:
    sid = session_id.strip()
    if not sid:
        return None
    for project_dir in _candidate_project_dirs(workdir):
        candidate = project_dir / f"{sid}.jsonl"
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


def _extract_text_from_content(content: object) -> str:
    """Extract plain text from Claude message content (list of blocks or string)."""
    if isinstance(content, str):
        return content.strip()
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
        elif item_type == "tool_use":
            name = str(item.get("name") or "").strip()
            if name:
                parts.append(f"[tool: {name}]")
        elif item_type == "tool_result":
            # Flatten nested tool result content.
            nested = item.get("content")
            if isinstance(nested, str) and nested.strip():
                parts.append(nested.strip())
            elif isinstance(nested, list):
                for sub in nested:
                    if isinstance(sub, dict) and str(sub.get("type") or "") == "text":
                        t = str(sub.get("text") or "").strip()
                        if t:
                            parts.append(t)
    return "\n".join(parts)


def read_session(session_id: str, workspace: str) -> Optional[CanonicalSession]:
    """Read a Claude Code session and return a CanonicalSession."""
    path = _find_session_file(session_id, workspace)
    if path is None:
        logger.warning("claude reader: session file not found for %s in %s", session_id, workspace)
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
                    text = _extract_text_from_content(message.get("content"))
                    if text:
                        messages.append(CanonicalMessage(role="user", content=text, timestamp=ts))

                elif event_type == "assistant":
                    message = data.get("message", {})
                    text = _extract_text_from_content(message.get("content"))
                    if text:
                        messages.append(CanonicalMessage(role="assistant", content=text, timestamp=ts))

    except Exception:
        logger.exception("claude reader: failed to read %s", path)
        if messages:
            return CanonicalSession(
                source_cli="claude",
                session_id=session_id,
                workspace=workspace,
                messages=messages,
            )
        return None

    if not messages:
        logger.warning("claude reader: no messages in %s", path)
        return None

    return CanonicalSession(
        source_cli="claude",
        session_id=session_id,
        workspace=workspace,
        messages=messages,
    )
