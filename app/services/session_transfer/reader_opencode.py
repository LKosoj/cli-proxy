"""Read opencode sessions from its SQLite store into CanonicalSession.

opencode keeps every conversation in one database — ``$XDG_DATA_HOME/opencode/
opencode.db`` or ``~/.local/share/opencode/opencode.db``. ``session`` holds the
metadata (``directory`` is the workdir the conversation belongs to), ``message``
one row per turn and ``part`` its content blocks. ``message.data`` and
``part.data`` are JSON blobs holding everything except the columns that already
exist as fields (``id``/``session_id``/``message_id``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

from .canonical import CanonicalMessage, CanonicalSession

logger = logging.getLogger(__name__)

DB_FILENAME = "opencode.db"
# Cross-process reads must not hang on a write lock held by a running opencode.
DB_TIMEOUT_SEC = 5.0


def data_dir(home: Optional[Path] = None) -> Path:
    """Directory opencode stores its state in (mirrors its own `Global.Path.data`)."""
    if home is None:
        xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
        if xdg:
            return Path(xdg) / "opencode"
        home = Path.home()
    return home / ".local" / "share" / "opencode"


def db_path(home: Optional[Path] = None) -> Path:
    return data_dir(home) / DB_FILENAME


def connect_readonly(path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    """Open the opencode database for reading, or None when it does not exist yet."""
    target = Path(path) if path is not None else db_path()
    if not target.is_file():
        return None
    try:
        return sqlite3.connect(
            f"file:{target}?mode=ro",
            uri=True,
            timeout=DB_TIMEOUT_SEC,
        )
    except Exception:
        logger.exception("opencode reader: cannot open db=%s", target)
        return None


def _json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = loads_safe(text, strict_first=True)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _epoch_seconds(raw: object) -> Optional[float]:
    """opencode timestamps are epoch milliseconds; canonical form wants seconds."""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value / 1000.0 if value > 0 else None


def _part_rows(conn: sqlite3.Connection, session_id: str) -> dict[str, List[dict]]:
    by_message: dict[str, List[dict]] = {}
    for message_id, data in conn.execute(
        "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY id",
        (session_id,),
    ):
        by_message.setdefault(str(message_id), []).append(_json_object(data))
    return by_message


def _text_of(part: dict) -> str:
    """Text of a content block, skipping opencode's own context injections."""
    if str(part.get("type") or "") != "text":
        return ""
    if part.get("synthetic") or part.get("ignored"):
        return ""
    return str(part.get("text") or "").strip()


def _tool_text(part: dict) -> str:
    if str(part.get("type") or "") != "tool":
        return ""
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    status = str(state.get("status") or "").strip().lower()
    if status == "error":
        detail = str(state.get("error") or "").strip()
    elif status == "completed":
        detail = str(state.get("output") or "").strip()
    else:
        return ""
    if not detail:
        return ""
    tool_name = str(part.get("tool") or "tool").strip() or "tool"
    return f"{tool_name}: {detail}"


def session_directory(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute("SELECT directory FROM session WHERE id = ?", (session_id,)).fetchone()
    return str(row[0] or "").strip() if row else ""


def first_user_text(conn: sqlite3.Connection, session_id: str) -> str:
    """First real user request of a session, used as a preview in the resume picker."""
    for message_id, data in conn.execute(
        "SELECT id, data FROM message WHERE session_id = ? ORDER BY id",
        (session_id,),
    ):
        if str(_json_object(data).get("role") or "") != "user":
            continue
        for part in conn.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY id",
            (str(message_id),),
        ):
            text = _text_of(_json_object(part[0]))
            if text:
                return text
    return ""


def read_session(session_id: str, workspace: str) -> Optional[CanonicalSession]:
    sid = str(session_id or "").strip()
    if not sid:
        return None

    conn = connect_readonly()
    if conn is None:
        logger.info("opencode reader: no database at %s", db_path())
        return None

    messages: List[CanonicalMessage] = []
    try:
        parts_by_message = _part_rows(conn, sid)
        for message_id, data, time_created in conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY id",
            (sid,),
        ):
            info = _json_object(data)
            role = str(info.get("role") or "").strip()
            if role not in ("user", "assistant"):
                continue
            timestamp = _epoch_seconds(time_created)
            texts: List[str] = []
            for part in parts_by_message.get(str(message_id), []):
                text = _text_of(part)
                if text:
                    texts.append(text)
            if texts:
                messages.append(
                    CanonicalMessage(role=role, content="\n".join(texts), timestamp=timestamp)
                )
            for part in parts_by_message.get(str(message_id), []):
                tool_text = _tool_text(part)
                if tool_text:
                    messages.append(
                        CanonicalMessage(role="tool", content=tool_text, timestamp=timestamp)
                    )
    except Exception:
        logger.exception("opencode reader: failed to read session=%s", sid)
        return None
    finally:
        conn.close()

    if not messages:
        logger.warning("opencode reader: no messages for session=%s", sid)
        return None

    return CanonicalSession(
        source_cli="opencode",
        session_id=sid,
        workspace=str(workspace or "").strip(),
        messages=messages,
    )
