"""Write CanonicalSession into a Gemini CLI chat file."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._user_paths import chown_path, home_for_user, mkdir_chain_with_chown
from .canonical import CanonicalSession

logger = logging.getLogger(__name__)

GEMINI_TMP_BASE = Path.home() / ".gemini" / "tmp"


def _resolve_tmp_base(username: Optional[str]) -> Path:
    home = home_for_user(username)
    if home is None:
        return GEMINI_TMP_BASE
    return home / ".gemini" / "tmp"


def _project_hash(workdir: str) -> str:
    raw = os.path.realpath(workdir).rstrip(os.sep) or workdir
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _msg_dt(msg_ts: Optional[float], fallback: datetime) -> datetime:
    if not msg_ts:
        return fallback
    try:
        return datetime.fromtimestamp(msg_ts, tz=timezone.utc)
    except Exception:
        return fallback


def _build_messages(
    canonical: CanonicalSession,
    started_at: datetime,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in canonical.messages:
        if msg.role not in ("user", "assistant"):
            continue
        when = _msg_dt(msg.timestamp, started_at)
        kind = "user" if msg.role == "user" else "gemini"
        out.append({
            "id": str(uuid.uuid4()),
            "type": kind,
            "content": msg.content,
            "timestamp": _iso_z(when),
        })
    return out


def _atomic_write_json(target: Path, payload: Dict[str, Any], username: Optional[str]) -> None:
    mkdir_chain_with_chown(target.parent, username)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    chown_path(target, username)


def write_session(canonical: CanonicalSession, workspace: str, username: Optional[str] = None) -> Optional[str]:
    """Write canonical session as a Gemini CLI .json. Returns the new session id."""
    if not canonical.messages:
        return None

    new_sid = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    phash = _project_hash(workspace)
    project_dir = _resolve_tmp_base(username) / phash
    target = project_dir / "chats" / f"session-{new_sid}.json"

    payload = {
        "sessionId": new_sid,
        "projectHash": phash,
        "cwd": workspace,
        "startTime": _iso_z(started_at),
        "lastUpdated": _iso_z(started_at),
        "messages": _build_messages(canonical, started_at),
    }

    try:
        _atomic_write_json(target, payload, username)
    except Exception:
        logger.exception("gemini writer: failed to write chat file")
        return None
    return new_sid
