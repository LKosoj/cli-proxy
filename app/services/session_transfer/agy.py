"""Read and write the native Antigravity CLI conversation database.

The SQLite schema and the small protobuf subset below were checked against
agy 1.5.16 and the independent format notes in txcript and session-migrate.
The display transcript only covers recorded turns, so it can omit imported
history. The database is also what agy opens by explicit conversation ID.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

from ._user_paths import chown_path, home_for_user, mkdir_chain_with_chown
from .canonical import CanonicalMessage, CanonicalSession, tool_call_marker

logger = logging.getLogger(__name__)

_USER_INPUT, _PLANNER_RESPONSE, _DONE = 14, 15, 3
_PROJECT = "default-cli-project"
_SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE trajectory_meta (
    trajectory_id TEXT PRIMARY KEY, cascade_id TEXT, trajectory_type INTEGER, source INTEGER
);
CREATE TABLE steps (
    idx INTEGER PRIMARY KEY, step_type INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 0,
    has_subtrajectory NUMERIC NOT NULL DEFAULT false, metadata BLOB, error_details BLOB,
    permissions BLOB, task_details BLOB, render_info BLOB, step_payload BLOB,
    step_format INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_steps_status ON steps(status);
CREATE INDEX idx_steps_step_type ON steps(step_type);
CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER NOT NULL DEFAULT 0);
CREATE TABLE executor_metadata (idx INTEGER PRIMARY KEY, data BLOB);
CREATE TABLE parent_references (idx INTEGER PRIMARY KEY, data BLOB);
CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY DEFAULT 'main', data BLOB);
CREATE TABLE battle_mode_infos (idx INTEGER PRIMARY KEY, data BLOB);
"""


def _root(username: str | None) -> Path | None:
    home = home_for_user(username) if username else Path.home()
    return home / ".gemini" / "antigravity-cli" if home else None


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number: int, value: int | str | bytes) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    data = value.encode() if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(data)) + data


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("invalid Antigravity protobuf varint")


def _fields(data: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire in (1, 2, 5):
            size, offset = _read_varint(data, offset) if wire == 2 else (8 if wire == 1 else 4, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("truncated Antigravity protobuf field")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError("unsupported Antigravity protobuf field")
        fields.setdefault(number, []).append(value)
    return fields


def _nested(fields: dict, number: int) -> dict:
    value = fields.get(number, (b"",))[0]
    return _fields(value) if isinstance(value, bytes) else {}


def _text(fields: dict, number: int) -> str:
    value = fields.get(number, (b"",))[0]
    return value.decode(errors="replace").strip() if isinstance(value, bytes) else ""


def _message(step_type: int, blob: bytes, metadata: bytes) -> CanonicalMessage | None:
    payload = _fields(blob)
    timestamp = _nested(_fields(metadata), 1)
    seconds = timestamp.get(1, (0,))[0]
    nanos = timestamp.get(2, (0,))[0]
    created_at = float(seconds) + float(nanos) / 1e9 if seconds else None
    if step_type == _USER_INPUT:
        role = "user"
        body = _nested(payload, 19)
        content = _text(body, 2) or _text(_nested(body, 3), 1) or _text(body, 1)
    else:
        role = "assistant"
        body = _nested(payload, 20)
        parts = [_text(body, 1) or _text(body, 8)]
        for raw_call in body.get(7, []):
            if isinstance(raw_call, bytes):
                name = _text(_fields(raw_call), 2)
                if name:
                    parts.append(tool_call_marker(name))
        content = "\n".join(part for part in parts if part)
    return CanonicalMessage(role, content, created_at) if content else None


def read_session(session_id: str, workspace: str, username: str | None = None) -> CanonicalSession | None:
    try:
        if str(uuid.UUID(session_id)) != session_id:
            return None
    except ValueError:
        return None
    root = _root(username)
    if root is None:
        return None
    path = root / "conversations" / f"{session_id}.db"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM trajectory_meta WHERE cascade_id = ?", (session_id,)).fetchone():
            logger.warning("agy transfer: conversation ID differs from file name session=%s", session_id)
            return None
        rows = db.execute(
            "SELECT step_type, step_payload, metadata FROM steps "
            "WHERE step_type IN (?, ?) AND status = ? ORDER BY idx",
            (_USER_INPUT, _PLANNER_RESPONSE, _DONE),
        )
        messages = []
        for step_type, payload, metadata in rows:
            if payload and (message := _message(step_type, payload, metadata or b"")):
                messages.append(message)
    return CanonicalSession("agy", session_id, workspace, messages) if messages else None


def _step(
    index: int, message: CanonicalMessage, session_id: str, trajectory_id: str, now: float,
) -> tuple[int, int, int, bytes, bytes]:
    user = message.role == "user"
    step_type, payload_field, source = (_USER_INPUT, 19, 4) if user else (_PLANNER_RESPONSE, 20, 2)
    timestamp = _field(1, int(message.timestamp or now))
    reference = _field(1, trajectory_id) + _field(2, index) + _field(4, session_id)
    metadata = _field(1, timestamp) + _field(3, source) + _field(12, str(uuid.uuid4())) + _field(20, reference)
    text = message.content
    if user:
        body = _field(2, text) + _field(3, _field(1, text))
    else:
        body = _field(1, text) + _field(6, f"bot-{uuid.uuid4()}") + _field(8, text)
    blob = _field(1, step_type) + _field(4, _DONE) + _field(5, metadata) + _field(payload_field, body)
    return index, step_type, _DONE, metadata, blob


def write_session(canonical: CanonicalSession, workspace: str, username: str | None = None) -> str | None:
    messages = [
        message for message in canonical.messages
        if message.role in ("user", "assistant") and message.content.strip()
    ]
    root = _root(username)
    if root is None or not messages:
        return None

    session_id, trajectory_id = str(uuid.uuid4()), str(uuid.uuid4())
    directory = root / "conversations"
    mkdir_chain_with_chown(directory, username)
    descriptor, temp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    os.close(descriptor)
    temp = Path(temp_name)
    now = time.time()
    uri = Path(workspace).resolve().as_uri()
    try:
        with closing(sqlite3.connect(temp)) as db, db:
            db.executescript(_SCHEMA)
            db.execute("INSERT INTO trajectory_meta VALUES (?, ?, 4, 17)", (trajectory_id, session_id))
            trajectory = (
                _field(1, _field(1, uri)) + _field(2, _field(1, int(now)))
                + _field(6, session_id) + _field(7, uri) + _field(18, _PROJECT)
            )
            db.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (trajectory,))
            db.executemany(
                "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
                (_step(index, msg, session_id, trajectory_id, now) for index, msg in enumerate(messages)),
            )
        # The CLI needs this directory when opening the database by ID.
        mkdir_chain_with_chown(root / "brain" / session_id, username)
        chown_path(temp, username)
        temp.replace(directory / f"{session_id}.db")
    finally:
        temp.unlink(missing_ok=True)
    return session_id
