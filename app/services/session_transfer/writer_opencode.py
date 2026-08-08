"""Write CanonicalSession into opencode's SQLite store.

opencode has no import path we can drive without its own runtime, so the rows go
straight into the database it reads on `opencode run --session <id>`: one
``session`` row, one ``message`` row per turn and one ``text`` ``part`` per
message. Ids repeat opencode's own scheme (`Identifier` in its bundle): prefix,
12 hex digits of `timestamp_ms * 4096 + counter` (bit-inverted for sessions, so
newer sessions sort first) and 14 random base62 chars.

Every message must name a provider/model that opencode can actually resolve: on
`--session <id>` it continues with the model of the last user message, so a
placeholder there fails the next turn with `ProviderModelNotFoundError`. The
model is therefore copied from the newest message already in the database, and
the transfer is refused when there is none to copy.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .canonical import CanonicalSession
from .reader_opencode import _json_object, DB_TIMEOUT_SEC, db_path

logger = logging.getLogger(__name__)

_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ID_RANDOM_LEN = 14
_ID_TIME_BITS = 48

_TRANSFER_AGENT = "build"

# Fallback for `session.version` when the database holds no session to copy it from.
_UNKNOWN_VERSION = "0.0.0"

# How far back to look for a usable provider/model pair.
_MODEL_LOOKUP_LIMIT = 50

_TITLE_MAX_LEN = 50


def _identifier(prefix: str, timestamp_ms: int, counter: int, *, descending: bool) -> str:
    value = int(timestamp_ms) * 4096 + int(counter)
    if descending:
        value = ~value
    value &= (1 << _ID_TIME_BITS) - 1
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_RANDOM_LEN))
    return f"{prefix}_{value:012x}{suffix}"


def _title_for(canonical: CanonicalSession) -> str:
    for msg in canonical.messages:
        if msg.role != "user":
            continue
        text = str(msg.content or "").strip()
        line = text.splitlines()[0].strip() if text else ""
        if line:
            return line[:_TITLE_MAX_LEN] + ("..." if len(line) > _TITLE_MAX_LEN else "")
    return "transferred session"


def _resolve_project_id(conn: sqlite3.Connection, workspace: str) -> str:
    """Project the workspace belongs to, matching how opencode itself buckets sessions.

    `.git/opencode` wins over the database: opencode rewrites that marker whenever
    it resolves a project, so a stale `project` row must not send the session into
    a bucket the CLI no longer uses for this directory.
    """
    now_ms = int(time.time() * 1000)
    try:
        project_id = (Path(workspace) / ".git" / "opencode").read_text(encoding="utf-8").strip()
    except OSError:
        project_id = ""

    if project_id:
        conn.execute(
            "INSERT OR IGNORE INTO project (id, worktree, vcs, time_created, time_updated, sandboxes)"
            " VALUES (?, ?, 'git', ?, ?, '[]')",
            (project_id, workspace, now_ms, now_ms),
        )
        return project_id

    row = conn.execute("SELECT id FROM project WHERE worktree = ?", (workspace,)).fetchone()
    if row:
        return str(row[0])

    conn.execute(
        "INSERT OR IGNORE INTO project (id, worktree, time_created, time_updated, sandboxes)"
        " VALUES ('global', '/', ?, ?, '[]')",
        (now_ms, now_ms),
    )
    return "global"


def _recent_model(conn: sqlite3.Connection) -> Optional[dict]:
    """Newest provider/model pair recorded in the database, or None if there is none.

    opencode resolves the model of a resumed session from its last user message,
    so the transferred rows must name a model this installation really has.
    """
    for (raw,) in conn.execute(
        "SELECT data FROM message ORDER BY id DESC LIMIT ?", (_MODEL_LOOKUP_LIMIT,)
    ):
        info = _json_object(raw)
        if not info:
            continue
        if str(info.get("role") or "") == "user":
            model = info.get("model")
            model = model if isinstance(model, dict) else {}
            provider_id = str(model.get("providerID") or "").strip()
            model_id = str(model.get("modelID") or "").strip()
        else:
            provider_id = str(info.get("providerID") or "").strip()
            model_id = str(info.get("modelID") or "").strip()
        if provider_id and model_id:
            return {"providerID": provider_id, "modelID": model_id}
    return None


def _session_version(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT version FROM session ORDER BY time_created DESC LIMIT 1").fetchone()
    return str(row[0]) if row and str(row[0] or "").strip() else _UNKNOWN_VERSION


def _message_rows(
    canonical: CanonicalSession,
    session_id: str,
    workspace: str,
    now_ms: int,
    model: dict,
) -> List[Tuple[dict, dict]]:
    """(message row, text part payload) pairs in the order opencode should replay them."""
    rows: List[Tuple[dict, dict]] = []
    counter = 0
    last_user_id: Optional[str] = None
    for msg in canonical.messages:
        if msg.role not in ("user", "assistant"):
            continue
        content = str(msg.content or "").strip()
        if not content:
            continue
        if msg.role == "assistant" and last_user_id is None:
            # `parentID` is mandatory for assistant messages; a reply with no
            # preceding request in the capsule cannot be represented.
            logger.info("opencode writer: dropped leading assistant message without a parent")
            continue

        counter += 1
        created_ms = int(msg.timestamp * 1000) if msg.timestamp else now_ms
        message_id = _identifier("msg", now_ms, counter, descending=False)
        if msg.role == "user":
            data = {
                "role": "user",
                "time": {"created": created_ms},
                "agent": _TRANSFER_AGENT,
                "model": dict(model),
            }
            last_user_id = message_id
        else:
            data = {
                "role": "assistant",
                "time": {"created": created_ms, "completed": created_ms},
                "parentID": last_user_id,
                "providerID": model["providerID"],
                "modelID": model["modelID"],
                "mode": _TRANSFER_AGENT,
                "agent": _TRANSFER_AGENT,
                "path": {"cwd": workspace, "root": workspace},
                "cost": 0,
                "tokens": {
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            }

        part = {
            "type": "text",
            "text": content,
            "time": {"start": created_ms, "end": created_ms},
        }
        rows.append(
            (
                {
                    "id": message_id,
                    "session_id": session_id,
                    "time_created": created_ms,
                    "data": data,
                },
                part,
            )
        )
    return rows


def write_session(
    canonical: CanonicalSession,
    workspace: str,
    username: Optional[str] = None,
) -> Optional[str]:
    """Insert canonical session into opencode's database. Returns the new session id."""
    if not canonical.messages:
        return None
    raw_workspace = str(workspace or "").strip()
    if not raw_workspace:
        # realpath("") resolves to the bot's own cwd, which is not a workspace.
        return None
    ws = os.path.realpath(raw_workspace)

    target = db_path()
    if not target.is_file():
        # Creating the schema ourselves would fork opencode's migrations; the CLI
        # must have run at least once before a session can be handed to it.
        logger.warning("opencode writer: no database at %s, run opencode once first", target)
        return None

    now_ms = int(time.time() * 1000)
    session_id = _identifier("ses", now_ms, 1, descending=True)

    try:
        conn = sqlite3.connect(str(target), timeout=DB_TIMEOUT_SEC)
    except Exception:
        logger.exception("opencode writer: cannot open db=%s", target)
        return None

    try:
        with conn:
            model = _recent_model(conn)
            if model is None:
                # Without a model opencode can name, the session would break on the
                # first prompt instead of resuming - better to skip the transfer.
                logger.warning(
                    "opencode writer: no provider/model recorded in %s, run opencode once first",
                    target,
                )
                return None
            rows = _message_rows(canonical, session_id, ws, now_ms, model)
            if not rows:
                logger.warning("opencode writer: nothing to write for workspace=%s", ws)
                return None
            conn.execute(
                "INSERT INTO session"
                " (id, project_id, slug, directory, title, version, time_created, time_updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    _resolve_project_id(conn, ws),
                    f"transferred-{secrets.token_hex(4)}",
                    ws,
                    _title_for(canonical),
                    _session_version(conn),
                    now_ms,
                    now_ms,
                ),
            )
            for index, (message, part) in enumerate(rows, start=1):
                conn.execute(
                    "INSERT INTO message (id, session_id, time_created, time_updated, data)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        message["id"],
                        session_id,
                        message["time_created"],
                        message["time_created"],
                        json.dumps(message["data"], ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "INSERT INTO part"
                    " (id, message_id, session_id, time_created, time_updated, data)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _identifier("prt", now_ms, index, descending=False),
                        message["id"],
                        session_id,
                        message["time_created"],
                        message["time_created"],
                        json.dumps(part, ensure_ascii=False),
                    ),
                )
    except Exception:
        logger.exception("opencode writer: failed to write session into %s", target)
        return None
    finally:
        conn.close()

    return session_id
