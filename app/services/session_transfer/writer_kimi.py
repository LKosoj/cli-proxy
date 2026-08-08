"""Write CanonicalSession into Kimi Code CLI session files.

The layout mirrors what kimi writes itself: a session directory holding
``state.json`` plus ``agents/main/wire.jsonl``, and one line in the shared
``session_index.jsonl`` so the CLI can resolve the id back to that directory.
The journal repeats the shape of kimi's own legacy importer - a ``metadata``
header followed by one ``context.append_message`` per message - plus the
``profile.bind`` record that headless ``--resume`` needs (see
``_profile_bind_record``).
"""

from __future__ import annotations

import json
import logging
import os
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from modes.sdk.runtime.json_normalizer import loads_safe

from ._user_paths import chown_path, home_for_user, mkdir_chain_with_chown
from .canonical import CanonicalSession
from .reader_kimi import _workspace_key

logger = logging.getLogger(__name__)

KIMI_HOME_BASE = Path.home() / ".kimi-code"
WIRE_PROTOCOL_VERSION = "1.5"
SESSION_STATE_VERSION = 2
MAX_TITLE_LEN = 50
MAX_LAST_PROMPT_LEN = 200
DEFAULT_PROFILE_NAME = "agent"
DEFAULT_THINKING_EFFORT = "off"


def _resolve_home_base(username: Optional[str]) -> Path:
    home = home_for_user(username)
    if home is None:
        return KIMI_HOME_BASE
    return home / ".kimi-code"


def _msg_ms(msg_ts: Optional[float], fallback_ms: int) -> int:
    if not msg_ts:
        return fallback_ms
    try:
        return int(float(msg_ts) * 1000)
    except Exception:
        return fallback_ms


def _first_user_text(canonical: CanonicalSession) -> str:
    for msg in canonical.messages:
        if msg.role != "user":
            continue
        text = " ".join(str(msg.content or "").strip().split())
        if text:
            return text
    return ""


def _first_profile_bind(wire: Path) -> Optional[Dict[str, Any]]:
    try:
        with wire.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    record = loads_safe(line, strict_first=True)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = str(record.get("type") or "").strip()
                if record_type == "profile.bind":
                    return record
                # Kimi binds the profile while it creates the agent, so a journal
                # that already reached its first turn carries no bind at all.
                if record_type == "turn.prompt":
                    return None
    except OSError:
        logger.exception("kimi writer: failed to read wire path=%s", wire)
    return None


def _latest_rendered_bind(bucket_dir: Path) -> Optional[Dict[str, Any]]:
    """Newest binding with a system prompt kimi rendered for this very workspace.

    A binding without a prompt is one we wrote ourselves below, and reusing it would
    carry the empty prompt into every later transfer of the same workspace.
    """
    wires: List[Tuple[float, Path]] = []
    try:
        for session_dir in bucket_dir.iterdir():
            wire = session_dir / "agents" / "main" / "wire.jsonl"
            try:
                wires.append((wire.stat().st_mtime, wire))
            except OSError:
                continue
    except OSError:
        return None
    for _, wire in sorted(wires, key=lambda item: item[0], reverse=True):
        record = _first_profile_bind(wire)
        if record is not None and str(record.get("systemPrompt") or "").strip():
            return record
    return None


def _default_model_alias(home_base: Path) -> str:
    """`default_model` from kimi's config: the alias it binds for a new session."""
    path = home_base / "config.toml"
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception("kimi writer: failed to read config path=%s", path)
        return ""
    alias = config.get("default_model")
    return alias.strip() if isinstance(alias, str) else ""


def _profile_bind_record(home_base: Path, workspace_key: str, started_ms: int) -> Optional[Dict[str, Any]]:
    """Binding kimi replays on resume: model, agent profile and system prompt.

    Headless `--resume` replays the journal and runs the turn without binding a
    profile of its own, so a session whose wire carries no `profile.bind` dies with
    `model.not_configured`. Kimi renders that record per workspace, so the one it
    already wrote here is reused as is; with no kimi session in this workspace yet
    only the configured default model is known, and the system prompt stays empty
    until kimi renders it again.
    """
    reused = _latest_rendered_bind(home_base / "sessions" / workspace_key)
    if reused is not None:
        return {**reused, "time": started_ms}
    alias = _default_model_alias(home_base)
    if not alias:
        logger.warning("kimi writer: no default model configured in %s", home_base / "config.toml")
        return None
    return {
        "type": "profile.bind",
        "modelAlias": alias,
        "profileName": DEFAULT_PROFILE_NAME,
        "thinkingEffort": DEFAULT_THINKING_EFFORT,
        "systemPrompt": "",
        "disallowedTools": [],
        "time": started_ms,
    }


def _build_wire_records(
    canonical: CanonicalSession,
    started_ms: int,
    bind: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = [
        {"type": "metadata", "protocol_version": WIRE_PROTOCOL_VERSION, "created_at": started_ms}
    ]
    if bind is not None:
        records.append(bind)
    for msg in canonical.messages:
        # Only chat turns: a `tool` message without the assistant tool call that
        # produced it makes the very next request invalid for the model provider.
        if msg.role not in ("user", "assistant"):
            continue
        text = str(msg.content or "").strip()
        if not text:
            continue
        message: Dict[str, Any] = {
            "role": msg.role,
            "content": [{"type": "text", "text": text}],
            "toolCalls": [],
        }
        if msg.role == "user":
            message["origin"] = {"kind": "user"}
        records.append(
            {
                "type": "context.append_message",
                "message": message,
                "time": _msg_ms(msg.timestamp, started_ms),
            }
        )
    return records


def _build_state(
    canonical: CanonicalSession,
    *,
    session_id: str,
    workspace: str,
    wire_dir: Path,
    started_ms: int,
) -> Dict[str, Any]:
    first_user_text = _first_user_text(canonical)
    title = str(canonical.summary or "").strip() or first_user_text or "Transferred session"
    return {
        "id": session_id,
        "version": SESSION_STATE_VERSION,
        "cwd": os.path.realpath(workspace),
        "createdAt": started_ms,
        "updatedAt": started_ms,
        "archived": False,
        "title": title[:MAX_TITLE_LEN],
        "isCustomTitle": True,
        "lastPrompt": first_user_text[:MAX_LAST_PROMPT_LEN],
        "agents": {"main": {"homedir": str(wire_dir), "type": "main"}},
        "custom": {
            "transferred_from_cli": canonical.source_cli,
            "source_session_id": canonical.session_id,
        },
    }


def _write_json(path: Path, payload: Dict[str, Any], username: Optional[str]) -> None:
    mkdir_chain_with_chown(path.parent, username)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    chown_path(path, username)


def _write_jsonl(path: Path, records: List[Dict[str, Any]], username: Optional[str]) -> None:
    mkdir_chain_with_chown(path.parent, username)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")
    os.replace(tmp, path)
    chown_path(path, username)


def _append_session_index(path: Path, entry: Dict[str, Any], username: Optional[str]) -> None:
    """The index is append-only and shared by every kimi session of this home."""
    mkdir_chain_with_chown(path.parent, username)
    existed = path.exists()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False))
        handle.write("\n")
    if not existed:
        chown_path(path, username)


def write_session(canonical: CanonicalSession, workspace: str, username: Optional[str] = None) -> Optional[str]:
    """Write canonical session as a Kimi Code session. Returns the new session id."""
    if not canonical.messages:
        return None
    key = _workspace_key(workspace)
    if not key:
        return None

    new_sid = f"session_{uuid.uuid4()}"
    started_ms = int(time.time() * 1000)
    home_base = _resolve_home_base(username)
    session_dir = home_base / "sessions" / key / new_sid
    wire_dir = session_dir / "agents" / "main"

    records = _build_wire_records(canonical, started_ms, _profile_bind_record(home_base, key, started_ms))
    if not any(str(record.get("type") or "") == "context.append_message" for record in records):
        logger.warning("kimi writer: canonical session has no chat messages to write")
        return None

    _write_jsonl(wire_dir / "wire.jsonl", records, username)
    _write_json(
        session_dir / "state.json",
        _build_state(
            canonical,
            session_id=new_sid,
            workspace=workspace,
            wire_dir=wire_dir,
            started_ms=started_ms,
        ),
        username,
    )
    _append_session_index(
        home_base / "session_index.jsonl",
        {"sessionId": new_sid, "sessionDir": str(session_dir), "workDir": os.path.realpath(workspace)},
        username,
    )
    return new_sid
