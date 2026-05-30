"""Tests for L4.1 / L4.2 / L4.3 fixes."""
import os

from app.services.session_state import SessionState
from modes.sdk.json_store import read_json_locked
from modes.sdk.runtime.memory_store import _normalize_chat_id, chat_workspace_root


# ---------------------------------------------------------------------------
# L4.1 – format_session_state
# ---------------------------------------------------------------------------

def test_format_session_state_expected_output():
    from tg.handlers import format_session_state

    st = SessionState(
        session_id="abc123",
        tool="codex",
        workdir="/tmp/proj",
        resume_token="tok",
        summary="краткое",
        updated_at=0.0,
        name="my-session",
    )
    result = format_session_state(st, updated_at_str="2025-01-01 12:00:00")
    assert "Session: abc123" in result
    assert "Tool: codex" in result
    assert "Workdir: /tmp/proj" in result
    assert "Resume: tok" in result
    assert "Name: my-session" in result
    assert "Summary: краткое" in result
    assert "Updated: 2025-01-01 12:00:00" in result


def test_format_session_state_none_fields():
    from tg.handlers import format_session_state

    st = SessionState(
        session_id=None,
        tool="gemini",
        workdir="/tmp",
        resume_token=None,
        summary=None,
        updated_at=0.0,
        name=None,
    )
    result = format_session_state(st, updated_at_str="нет")
    assert "Session: нет" in result
    assert "Resume: нет" in result
    assert "Name: нет" in result
    assert "Summary: нет" in result


# ---------------------------------------------------------------------------
# L4.2 – read_json_locked does not create file for non-existent path
# ---------------------------------------------------------------------------

def test_read_json_locked_nonexistent_returns_default_no_file(tmp_path):
    missing = str(tmp_path / "nonexistent" / "data.json")
    result = read_json_locked(missing, default={"key": "val"})
    assert result == {"key": "val"}
    assert not os.path.exists(missing)


def test_read_json_locked_nonexistent_empty_default(tmp_path):
    missing = str(tmp_path / "data.json")
    result = read_json_locked(missing)
    assert result == {}
    assert not os.path.exists(missing)


def test_read_json_locked_existing_file(tmp_path):
    p = tmp_path / "data.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    result = read_json_locked(str(p))
    assert result == {"a": 1}
    assert p.exists()


# ---------------------------------------------------------------------------
# L4.3 – _normalize_chat_id / chat_workspace_root with negative IDs
# ---------------------------------------------------------------------------

def test_normalize_chat_id_negative_not_zero():
    assert _normalize_chat_id(-100123456789) == -100123456789


def test_normalize_chat_id_zero():
    assert _normalize_chat_id(0) == 0
    assert _normalize_chat_id(None) == 0


def test_normalize_chat_id_positive():
    assert _normalize_chat_id(42) == 42


def test_chat_workspace_root_negative_differs_from_zero(tmp_path):
    workdir = str(tmp_path)
    path_neg = chat_workspace_root(workdir, -100123456789)
    path_zero = chat_workspace_root(workdir, 0)
    assert path_neg != path_zero
    assert "chat_-100123456789" in path_neg


def test_chat_workspace_root_two_different_negative_ids_differ(tmp_path):
    workdir = str(tmp_path)
    path_a = chat_workspace_root(workdir, -100111111111)
    path_b = chat_workspace_root(workdir, -100222222222)
    assert path_a != path_b
