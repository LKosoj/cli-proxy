import sqlite3
import uuid
from contextlib import closing

from app.services.session_transfer import agy, reader_codex, writer_codex
from app.services.session_transfer.canonical import CanonicalMessage, CanonicalSession
from app.services.session_transfer.service import extract_session, write_target_session


def test_agy_transfer_both_directions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    codex_sessions = tmp_path / ".codex" / "sessions"
    monkeypatch.setattr(reader_codex, "CODEX_SESSIONS_BASE", codex_sessions)
    monkeypatch.setattr(writer_codex, "CODEX_SESSIONS_DIR", codex_sessions)
    monkeypatch.setattr(writer_codex, "CODEX_HISTORY_FILE", tmp_path / ".codex" / "history.jsonl")
    workspace = tmp_path / "проект с пробелом"
    workspace.mkdir()
    workdir = str(workspace)
    seed = CanonicalSession(
        "codex", str(uuid.uuid4()), workdir,
        [CanonicalMessage("user", "Найди обработчик очереди"), CanonicalMessage("assistant", "Нашёл обработчик.")],
    )
    codex_id = write_target_session(seed, "codex", workdir, compact=False)
    source = extract_session("codex", codex_id, workdir)
    agy_id = write_target_session(source, "agy", workdir)
    assert agy_id and agy_id != codex_id

    root = tmp_path / ".gemini" / "antigravity-cli"
    assert (root / "brain" / agy_id).is_dir()
    with closing(sqlite3.connect(root / "conversations" / f"{agy_id}.db")) as db:
        assert db.execute("SELECT cascade_id FROM trajectory_meta").fetchone() == (agy_id,)
        assert db.execute("SELECT step_type FROM steps ORDER BY idx").fetchall() == [(14,), (15,)]
        metadata, = db.execute("SELECT data FROM trajectory_metadata_blob").fetchone()
        assert workspace.as_uri().encode() in metadata

    imported = extract_session("agy", agy_id, workdir)
    assert [message.role for message in imported.messages] == ["user", "assistant"]
    assert "Найди обработчик очереди" in imported.messages[0].content
    assert "Нашёл обработчик." in imported.messages[0].content
    codex_result = write_target_session(imported, "codex", workdir)
    assert codex_result and codex_result != codex_id
    restored = extract_session("codex", codex_result, workdir)
    assert "Найди обработчик очереди" in restored.messages[0].content
    assert "Нашёл обработчик." in restored.messages[0].content


def test_reads_agy_database_independently_of_display_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".gemini" / "antigravity-cli"
    session_id = str(uuid.uuid4())
    directory = root / "conversations"
    directory.mkdir(parents=True)
    # Protobuf Step: user_response "hello", source user, timestamp 1700000000.
    user_step = bytes.fromhex("080e20032a0a0a060880e2cfaa0618049a0107120568656c6c6f")
    # Protobuf Step: planner response "world" and a call to list_dir.
    assistant_step = bytes.fromhex("080f2003a201130a05776f726c643a0a12086c6973745f646972")
    with closing(sqlite3.connect(directory / f"{session_id}.db")) as db, db:
        db.executescript(agy._SCHEMA)
        db.execute("INSERT INTO trajectory_meta VALUES (?, ?, 4, 17)", (str(uuid.uuid4()), session_id))
        db.execute(
            "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (0, 14, 3, ?, ?)",
            (bytes.fromhex("0a060880e2cfaa061804"), user_step),
        )
        db.execute("INSERT INTO steps (idx, step_type, status, step_payload) VALUES (1, 15, 3, ?)", (assistant_step,))
        db.execute("INSERT INTO steps (idx, step_type, status, step_payload) VALUES (2, 17, 3, X'00')")

    result = extract_session("agy", session_id, str(tmp_path))
    assert result.messages == [
        CanonicalMessage("user", "hello", 1700000000),
        CanonicalMessage("assistant", "world\n[tool: list_dir]"),
    ]


def test_agy_transfer_uses_configured_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "default"))
    monkeypatch.setattr(agy, "home_for_user", lambda username: tmp_path / username)
    seed = CanonicalSession("codex", "source", str(tmp_path), [CanonicalMessage("user", "Привет")])
    session_id = write_target_session(seed, "agy", str(tmp_path), compact=False, username="cli-user")
    assert session_id
    assert extract_session("agy", session_id, str(tmp_path)) is None
    result = extract_session("agy", session_id, str(tmp_path), username="cli-user")
    assert result.messages[0].content == "Привет"
