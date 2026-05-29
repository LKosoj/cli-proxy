"""Write CanonicalSession into a Codex CLI rollout file."""

from __future__ import annotations

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

CODEX_BASE = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_BASE / "sessions"
CODEX_HISTORY_FILE = CODEX_BASE / "history.jsonl"


def _resolve_paths(username: Optional[str]) -> tuple[Path, Path]:
    home = home_for_user(username)
    if home is None:
        return CODEX_SESSIONS_DIR, CODEX_HISTORY_FILE
    base = home / ".codex"
    return base / "sessions", base / "history.jsonl"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _build_records(
    canonical: CanonicalSession,
    workspace: str,
    new_uuid: str,
    started_at: datetime,
) -> List[Dict[str, Any]]:
    started_iso = _iso_z(started_at)
    records: List[Dict[str, Any]] = [
        {
            "timestamp": started_iso,
            "type": "session_meta",
            "payload": {
                "id": new_uuid,
                "timestamp": started_iso,
                "cwd": workspace,
                "originator": "codex_cli_rs",
                "cli_version": "0.121.0",
                "source": "cli",
                "model_provider": "openai",
            },
        }
    ]

    for msg in canonical.messages:
        if msg.role not in ("user", "assistant"):
            continue
        if msg.timestamp:
            try:
                msg_dt = datetime.fromtimestamp(msg.timestamp, tz=timezone.utc)
            except Exception:
                msg_dt = started_at
        else:
            msg_dt = started_at
        ts_iso = _iso_z(msg_dt)
        if msg.role == "user":
            records.append({
                "timestamp": ts_iso,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": msg.content}],
                },
            })
        else:
            records.append({
                "timestamp": ts_iso,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": msg.content}],
                },
            })
    return records


def _atomic_write_jsonl(target: Path, records: List[Dict[str, Any]], username: Optional[str]) -> None:
    mkdir_chain_with_chown(target.parent, username)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")
    os.replace(tmp, target)
    chown_path(target, username)


def _append_history(history_file: Path, new_uuid: str, canonical: CanonicalSession, fallback_ts: float, username: Optional[str]) -> None:
    try:
        existed = history_file.exists()
        mkdir_chain_with_chown(history_file.parent, username)
        with open(history_file, "a", encoding="utf-8") as f:
            for msg in canonical.messages:
                if msg.role != "user":
                    continue
                ts = int(msg.timestamp) if msg.timestamp else int(fallback_ts)
                f.write(json.dumps(
                    {"session_id": new_uuid, "ts": ts, "text": msg.content},
                    ensure_ascii=False,
                ))
                f.write("\n")
        if not existed:
            chown_path(history_file, username)
    except Exception:
        logger.exception("codex writer: failed to append history.jsonl")


def write_session(canonical: CanonicalSession, workspace: str, username: Optional[str] = None) -> Optional[str]:
    """Write canonical session as a Codex rollout. Returns the new UUID session id."""
    if not canonical.messages:
        return None
    sessions_dir, history_file = _resolve_paths(username)
    new_uuid = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    fname = f"rollout-{started_at.strftime('%Y-%m-%dT%H-%M-%S')}-{new_uuid}.jsonl"
    target_dir = sessions_dir / started_at.strftime("%Y") / started_at.strftime("%m") / started_at.strftime("%d")
    target = target_dir / fname

    try:
        records = _build_records(canonical, workspace, new_uuid, started_at)
        _atomic_write_jsonl(target, records, username)
    except Exception:
        logger.exception("codex writer: failed to write rollout file")
        return None

    _append_history(history_file, new_uuid, canonical, started_at.timestamp(), username)
    return new_uuid
