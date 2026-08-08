"""Read Kimi Code CLI session files into CanonicalSession.

Kimi keeps a conversation in ``~/.kimi-code/sessions/<workspace key>/<session id>/``:
``state.json`` holds the metadata, ``agents/main/wire.jsonl`` holds the journal the
CLI replays on ``--resume``. The workspace key repeats kimi's own
``encodeWorkDirKey``: ``wd_<slug of the directory name>_<12 hex of sha256 of the
absolute path>``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import List, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)

KIMI_SESSIONS_BASE = Path.home() / ".kimi-code" / "sessions"

WORKDIR_SLUG_MAX_LEN = 40
WORKDIR_HASH_LEN = 12
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9._-]+")

# Kimi itself drops every other user-role origin from the model context: those
# messages are injected by the CLI (permission reminders, hook output, cron), not
# typed by the person. See `compactionUserMessageDisposition` in the kimi bundle.
_REAL_USER_ORIGINS = frozenset({"user", "skill_activation", "plugin_command"})


def _slugify_workdir_name(name: str) -> str:
    slug = _SLUG_UNSAFE_RE.sub("-", str(name or "").lower()).strip("-")[:WORKDIR_SLUG_MAX_LEN].strip("-")
    return "workspace" if slug in ("", ".", "..") else slug


def _workspace_key(workdir: str) -> str:
    """Bucket name kimi gives to *workdir* (its `encodeWorkDirKey`)."""
    raw = str(workdir or "").strip()
    if not raw:
        return ""
    normalized = os.path.realpath(raw)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:WORKDIR_HASH_LEN]
    return f"wd_{_slugify_workdir_name(os.path.basename(normalized))}_{digest}"


def _find_session_dir(session_id: str, workdir: str) -> Optional[Path]:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    key = _workspace_key(workdir)
    if key:
        direct = KIMI_SESSIONS_BASE / key / sid
        if direct.is_dir():
            return direct
    if not KIMI_SESSIONS_BASE.is_dir():
        return None
    try:
        for bucket_dir in KIMI_SESSIONS_BASE.iterdir():
            candidate = bucket_dir / sid
            if candidate.is_dir():
                return candidate
    except Exception:
        logger.exception("kimi reader: failed to scan sessions base=%s", KIMI_SESSIONS_BASE)
    return None


def _load_json(path: Path) -> Optional[dict]:
    try:
        payload = loads_safe(path.read_text(encoding="utf-8", errors="ignore"), strict_first=True)
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("kimi reader: failed to read json path=%s", path)
        return None
    return payload if isinstance(payload, dict) else None


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict) or str(item.get("type") or "").strip() != "text":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _record_seconds(raw: object) -> Optional[float]:
    """Wire records stamp `time` in epoch milliseconds."""
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not raw:
        return None
    return float(raw) / 1000.0


def _is_real_user_message(message: dict) -> bool:
    origin = message.get("origin")
    if not isinstance(origin, dict):
        return True
    return str(origin.get("kind") or "").strip() in _REAL_USER_ORIGINS


def _read_wire(path: Path) -> List[CanonicalMessage]:
    """Rebuild the conversation from an agent journal.

    Assistant text is streamed as one `content.part` event per chunk, so chunks are
    buffered and flushed as a single message whenever another message closes them.
    """
    messages: List[CanonicalMessage] = []
    pending: List[str] = []
    pending_ts: Optional[float] = None

    def flush() -> None:
        nonlocal pending, pending_ts
        text = "\n".join(pending).strip()
        if text:
            messages.append(CanonicalMessage(role="assistant", content=text, timestamp=pending_ts))
        pending = []
        pending_ts = None

    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return []
    except OSError:
        logger.exception("kimi reader: failed to open wire path=%s", path)
        return []

    try:
        with handle:
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
                timestamp = _record_seconds(record.get("time"))
                record_type = str(record.get("type") or "").strip()

                if record_type == "context.append_message":
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    role = str(message.get("role") or "").strip()
                    if role not in ("user", "assistant", "tool"):
                        continue
                    if role == "user" and not _is_real_user_message(message):
                        continue
                    text = _content_text(message.get("content"))
                    if not text:
                        continue
                    flush()
                    messages.append(CanonicalMessage(role=role, content=text, timestamp=timestamp))

                elif record_type == "context.append_loop_event":
                    event = record.get("event")
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type") or "").strip()
                    if event_type == "content.part":
                        part = event.get("part")
                        text = _content_text([part] if isinstance(part, dict) else None)
                        if text:
                            pending.append(text)
                            if pending_ts is None:
                                pending_ts = timestamp
                    elif event_type == "tool.result":
                        result = event.get("result")
                        text = _content_text(result.get("output")) if isinstance(result, dict) else ""
                        if text:
                            flush()
                            messages.append(CanonicalMessage(role="tool", content=text, timestamp=timestamp))
    except Exception:
        logger.exception("kimi reader: failed to read wire path=%s", path)

    flush()
    return messages


def read_session(session_id: str, workspace: str) -> Optional[CanonicalSession]:
    """Read a Kimi Code session and return a CanonicalSession."""
    session_dir = _find_session_dir(session_id, workspace)
    if session_dir is None:
        logger.warning("kimi reader: session dir not found for %s in %s", session_id, workspace)
        return None

    messages = _read_wire(session_dir / "agents" / "main" / "wire.jsonl")
    if not messages:
        logger.warning("kimi reader: no messages in %s", session_dir)
        return None

    state = _load_json(session_dir / "state.json") or {}
    summary = str(state.get("title") or state.get("lastPrompt") or "").strip() or None
    return CanonicalSession(
        source_cli="kimi",
        session_id=str(session_id or "").strip(),
        workspace=workspace,
        messages=messages,
        summary=summary,
    )
