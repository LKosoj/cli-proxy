"""Read Codex CLI rollout JSONL sessions into CanonicalSession."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)

CODEX_SESSIONS_BASE = Path.home() / ".codex" / "sessions"

_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T[\d-]+-([0-9a-fA-F-]{36})\.jsonl$"
)


def _find_session_file(session_id: str, workdir: str) -> Optional[Path]:
    sid = session_id.strip().lower()
    if not sid or not CODEX_SESSIONS_BASE.is_dir():
        return None
    # Codex stores rollouts under YYYY/MM/DD; scan recursively for the matching uuid.
    for path in CODEX_SESSIONS_BASE.rglob("rollout-*.jsonl"):
        match = _ROLLOUT_NAME_RE.match(path.name)
        if match and match.group(1).lower() == sid:
            return path
    return None


def _parse_iso_timestamp(raw: object) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _extract_message_text(content: object) -> str:
    """Extract plain text from Codex message content list."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type in ("input_text", "output_text", "text"):
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _is_synthetic_user_text(text: str) -> bool:
    """Filter Codex injected developer/system blocks that arrive as user-role messages."""
    head = text.lstrip()[:200]
    markers = (
        "<permissions instructions>",
        "<environment_context>",
        "# AGENTS.md",
        "<INSTRUCTIONS>",
        "You are Codex",
        "<collaboration_mode>",
    )
    return any(head.startswith(m) for m in markers)


def read_session(session_id: str, workspace: str) -> Optional[CanonicalSession]:
    """Read a Codex CLI rollout file and return a CanonicalSession."""
    path = _find_session_file(session_id, workspace)
    if path is None:
        logger.warning("codex reader: session file not found for %s", session_id)
        return None

    messages: List[CanonicalMessage] = []
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
                if str(data.get("type") or "") != "response_item":
                    continue

                payload = data.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("type") or "") != "message":
                    continue

                role = str(payload.get("role") or "").strip().lower()
                if role not in ("user", "assistant"):
                    continue

                text = _extract_message_text(payload.get("content"))
                if not text:
                    continue
                if role == "user" and _is_synthetic_user_text(text):
                    continue

                ts = _parse_iso_timestamp(data.get("timestamp"))
                messages.append(CanonicalMessage(role=role, content=text, timestamp=ts))
    except Exception:
        logger.exception("codex reader: failed to read %s", path)
        if messages:
            return CanonicalSession(
                source_cli="codex",
                session_id=session_id,
                workspace=workspace,
                messages=messages,
            )
        return None

    if not messages:
        logger.warning("codex reader: no messages in %s", path)
        return None

    return CanonicalSession(
        source_cli="codex",
        session_id=session_id,
        workspace=workspace,
        messages=messages,
    )
