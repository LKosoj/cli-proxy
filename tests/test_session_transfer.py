"""Tests for cross-CLI session transfer (read source CLI session, write into target CLI)."""

import json
import time
from unittest.mock import patch

from app.services.session_transfer import service as transfer_service
from app.services.session_transfer.canonical import CanonicalMessage, CanonicalSession
from app.services.session_transfer.service import (
    extract_session,
    write_target_session,
)
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import SessionCliSwitchResult, SessionManager, switch_session_active_cli_if_needed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(tmp_path, *, a_enabled=True, b_enabled=True):
    tools = {
        "a": ToolConfig(name="a", mode="headless", cmd=["bash", "-lc", "cat"], enabled=a_enabled),
        "b": ToolConfig(name="b", mode="headless", cmd=["bash", "-lc", "cat"], enabled=b_enabled),
    }
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_cli="a",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def _make_session(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    return mgr.create(1, "a", str(tmp_path))


def _make_canonical(n_messages: int = 5, source_cli: str = "claude") -> CanonicalSession:
    messages = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append(CanonicalMessage(role=role, content=f"Message {i}", timestamp=time.time()))
    return CanonicalSession(
        source_cli=source_cli,
        session_id="test-session-id",
        workspace="/tmp/test",
        messages=messages,
    )


# ---------------------------------------------------------------------------
# CanonicalSession / CanonicalMessage
# ---------------------------------------------------------------------------


class TestCanonical:
    def test_canonical_session_creation(self):
        cs = _make_canonical(3)
        assert cs.source_cli == "claude"
        assert cs.session_id == "test-session-id"
        assert len(cs.messages) == 3
        assert cs.messages[0].role == "user"
        assert cs.messages[1].role == "assistant"

    def test_canonical_message_defaults(self):
        msg = CanonicalMessage(role="user", content="hello")
        assert msg.timestamp is None

    def test_empty_canonical_session(self):
        cs = CanonicalSession(source_cli="qwen", session_id="x", workspace="/tmp")
        assert cs.messages == []
        assert cs.summary is None


# ---------------------------------------------------------------------------
# Service entrypoints
# ---------------------------------------------------------------------------


class TestService:
    def test_extract_unknown_cli(self):
        assert extract_session("unknown_cli", "id", "/tmp") is None

    def test_extract_empty_args(self):
        assert extract_session("", "", "") is None
        assert extract_session("claude", "", "/tmp") is None

    def test_write_unknown_cli(self):
        assert write_target_session(_make_canonical(2), "unknown_cli", "/tmp") is None

    def test_write_empty_canonical(self):
        empty = CanonicalSession(source_cli="claude", session_id="x", workspace="/tmp")
        assert write_target_session(empty, "qwen", "/tmp") is None

    def test_write_target_session_compacts_by_default(self, tmp_path, monkeypatch):
        captured = {}

        def _writer(canonical, workspace, username=None):
            captured["canonical"] = canonical
            return "target-token"

        monkeypatch.setattr(transfer_service, "_ensure_writers", lambda: {"codex": _writer})
        messages = [
            CanonicalMessage(
                role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}: " + ("x" * 1000),
                timestamp=time.time(),
            )
            for i in range(30)
        ]
        canonical = CanonicalSession(
            source_cli="claude",
            session_id="source-session",
            workspace=str(tmp_path),
            messages=messages,
        )

        result = transfer_service.write_target_session(
            canonical,
            "codex",
            str(tmp_path),
            max_chars=4500,
        )

        assert result == "target-token"
        compact = captured["canonical"]
        assert compact is not canonical
        assert len(compact.messages) == 2
        assert compact.messages[0].role == "user"
        assert compact.messages[1].role == "assistant"
        capsule = compact.messages[0].content
        assert "# Cross-CLI Session Capsule" in capsule
        assert "- original_messages: 30" in capsule
        assert "canonical.jsonl" in capsule
        assert len(capsule) <= 4500

        packs = list((tmp_path / ".cli-proxy" / "session-transfer").iterdir())
        assert len(packs) == 1
        assert (packs[0] / "manifest.json").is_file()
        assert (packs[0] / "capsule.md").read_text(encoding="utf-8") == capsule
        transcript_lines = (packs[0] / "canonical.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(transcript_lines) == 30

    def test_write_target_session_can_keep_full_native_mode(self, tmp_path, monkeypatch):
        captured = {}

        def _writer(canonical, workspace, username=None):
            captured["canonical"] = canonical
            return "target-token"

        monkeypatch.setattr(transfer_service, "_ensure_writers", lambda: {"codex": _writer})
        canonical = _make_canonical(3)

        result = transfer_service.write_target_session(
            canonical,
            "codex",
            str(tmp_path),
            compact=False,
        )

        assert result == "target-token"
        assert captured["canonical"] is canonical
        assert not (tmp_path / ".cli-proxy" / "session-transfer").exists()


# ---------------------------------------------------------------------------
# SessionCliSwitchResult / switch helper
# ---------------------------------------------------------------------------


class TestSwitchResult:
    def test_transfer_available_defaults_false(self):
        r = SessionCliSwitchResult(switched=True, previous_cli="a", active_cli="b")
        assert r.transfer_available is False
        assert r.source_session_id is None

    def test_transfer_available_true(self):
        r = SessionCliSwitchResult(
            switched=True,
            previous_cli="a",
            active_cli="b",
            transfer_available=True,
            source_session_id="tok-a",
        )
        assert r.transfer_available is True
        assert r.source_session_id == "tok-a"


class TestSwitchWithTransfer:
    def test_switch_returns_transfer_available_when_token_exists(self, tmp_path):
        cfg = _cfg(tmp_path, a_enabled=False, b_enabled=True)
        cfg_initial = _cfg(tmp_path, a_enabled=True, b_enabled=True)
        mgr_initial = SessionManager(cfg_initial)
        s = mgr_initial.create(1, "a", str(tmp_path))
        s.resume_token = "tok-a"
        s.config = cfg
        result = switch_session_active_cli_if_needed(s)
        assert result.switched is True
        assert result.previous_cli == "a"
        assert result.active_cli == "b"
        assert result.transfer_available is True
        assert result.source_session_id == "tok-a"

    def test_switch_no_transfer_when_no_token(self, tmp_path):
        cfg = _cfg(tmp_path, a_enabled=False, b_enabled=True)
        cfg_initial = _cfg(tmp_path, a_enabled=True, b_enabled=True)
        mgr_initial = SessionManager(cfg_initial)
        s = mgr_initial.create(1, "a", str(tmp_path))
        s.config = cfg
        result = switch_session_active_cli_if_needed(s)
        assert result.switched is True
        assert result.transfer_available is False


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


class TestReaderClaude:
    def test_reads_claude_jsonl(self, tmp_path):
        from app.services.session_transfer.reader_claude import read_session

        session_id = "test-uuid-123"
        project_dir = tmp_path / ".claude" / "projects" / "-tmp"
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = project_dir / f"{session_id}.jsonl"

        events = [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"content": [{"type": "text", "text": "Hello"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
             "message": {"content": [{"type": "text", "text": "Hi there!"}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:02Z",
             "message": {"content": [{"type": "text", "text": "Help me"}]}},
        ]
        with open(jsonl_path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        with patch(
            "app.services.session_transfer.reader_claude._candidate_project_dirs",
            return_value=[project_dir],
        ):
            result = read_session(session_id, "/tmp")

        assert result is not None
        assert result.source_cli == "claude"
        assert len(result.messages) == 3
        assert result.messages[0].role == "user"
        assert result.messages[0].content == "Hello"
        assert result.messages[1].role == "assistant"
        assert result.messages[1].content == "Hi there!"

    def test_returns_none_for_missing_file(self):
        from app.services.session_transfer.reader_claude import read_session

        with patch(
            "app.services.session_transfer.reader_claude._candidate_project_dirs",
            return_value=[],
        ):
            assert read_session("nonexistent", "/tmp") is None

    def test_reads_claude_jsonl_from_claude_bot_home(self, tmp_path, monkeypatch):
        from app.services.session_transfer import reader_claude

        session_id = "claude-bot-session"
        workspace = "/tmp/work"
        claude_home = tmp_path / "claude-bot-home"
        project_dir = claude_home / ".claude" / "projects" / "-tmp-work"
        project_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = project_dir / f"{session_id}.jsonl"
        jsonl_path.write_text(
            json.dumps({
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": [{"type": "text", "text": "Run from Claude user"}]},
            }) + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(reader_claude.Path, "home", staticmethod(lambda: tmp_path / "root-home"))
        monkeypatch.setattr(reader_claude, "home_for_user", lambda username: claude_home)

        result = reader_claude.read_session(session_id, workspace)

        assert result is not None
        assert result.source_cli == "claude"
        assert len(result.messages) == 1
        assert result.messages[0].content == "Run from Claude user"


class TestReaderQwen:
    def test_reads_qwen_jsonl(self, tmp_path):
        from app.services.session_transfer.reader_qwen import read_session

        session_id = "qwen-session-1"
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = chat_dir / f"{session_id}.jsonl"

        events = [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"parts": [{"text": "What is X?"}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
             "message": {"parts": [{"text": "X is..."}]}},
        ]
        with open(jsonl_path, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

        with patch(
            "app.services.session_transfer.reader_qwen._find_session_file",
            return_value=jsonl_path,
        ):
            result = read_session(session_id, "/tmp")

        assert result is not None
        assert result.source_cli == "qwen"
        assert len(result.messages) == 2
        assert result.messages[0].content == "What is X?"

    def test_returns_none_for_missing(self):
        from app.services.session_transfer.reader_qwen import read_session

        with patch(
            "app.services.session_transfer.reader_qwen._find_session_file",
            return_value=None,
        ):
            assert read_session("missing", "/tmp") is None


class TestReaderGemini:
    def test_reads_gemini_json(self, tmp_path):
        from app.services.session_transfer.reader_gemini import read_session

        session_id = "gemini-session-1"
        session_file = tmp_path / f"session-{session_id}.json"

        payload = {
            "projectHash": "abc123",
            "sessionId": session_id,
            "messages": [
                {"type": "user", "content": "Explain Y", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "gemini", "content": "Y is...", "timestamp": "2026-01-01T00:00:01Z"},
            ],
        }
        session_file.write_text(json.dumps(payload))

        with patch(
            "app.services.session_transfer.reader_gemini._find_session_file",
            return_value=session_file,
        ):
            result = read_session(session_id, "/tmp")

        assert result is not None
        assert result.source_cli == "gemini"
        assert len(result.messages) == 2
        assert result.messages[0].role == "user"
        assert result.messages[1].role == "assistant"

    def test_returns_none_for_missing(self):
        from app.services.session_transfer.reader_gemini import read_session

        with patch(
            "app.services.session_transfer.reader_gemini._find_session_file",
            return_value=None,
        ):
            assert read_session("missing", "/tmp") is None


class TestReaderCodex:
    def test_reads_codex_rollout(self, tmp_path):
        from app.services.session_transfer import reader_codex

        session_id = "019d9fbe-e199-7c90-b614-eaffe8f1f63c"
        rollout_dir = tmp_path / "sessions" / "2026" / "04" / "18"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        rollout = rollout_dir / f"rollout-2026-04-18T11-39-40-{session_id}.jsonl"

        events = [
            {"timestamp": "2026-04-18T08:40:06.074Z", "type": "session_meta",
             "payload": {"id": session_id, "cwd": "/tmp", "originator": "codex_cli_rs"}},
            {"timestamp": "2026-04-18T08:40:06.085Z", "type": "response_item",
             "payload": {"type": "message", "role": "developer",
                         "content": [{"type": "input_text", "text": "<permissions instructions>"}]}},
            {"timestamp": "2026-04-18T08:40:07.000Z", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "What is Codex?"}]}},
            {"timestamp": "2026-04-18T08:40:09.173Z", "type": "response_item",
             "payload": {"type": "reasoning", "summary": [], "encrypted_content": "..."}},
            {"timestamp": "2026-04-18T08:40:14.507Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "Codex is a coding agent."}]}},
            {"timestamp": "2026-04-18T08:40:15.000Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "exec", "arguments": "{}"}},
        ]
        with open(rollout, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        with patch.object(reader_codex, "CODEX_SESSIONS_BASE", tmp_path / "sessions"):
            result = reader_codex.read_session(session_id, "/tmp")

        assert result is not None
        assert result.source_cli == "codex"
        assert len(result.messages) == 2
        assert result.messages[0].role == "user"
        assert result.messages[0].content == "What is Codex?"
        assert result.messages[1].role == "assistant"
        assert result.messages[1].content == "Codex is a coding agent."

    def test_returns_none_for_missing(self, tmp_path):
        from app.services.session_transfer import reader_codex

        with patch.object(reader_codex, "CODEX_SESSIONS_BASE", tmp_path / "empty"):
            assert reader_codex.read_session("019d0000-0000-0000-0000-000000000000", "/tmp") is None

    def test_find_session_file_continues_after_scan_warning_threshold(self, tmp_path, monkeypatch):
        from app.services.session_transfer import reader_codex

        session_id = "019d9fbe-e199-7c90-b614-eaffe8f1f63c"
        base = tmp_path / "sessions"
        base.mkdir()
        filler = base / "rollout-2026-04-18T11-39-39-019d0000-0000-0000-0000-000000000000.jsonl"
        target = base / f"rollout-2026-04-18T11-39-40-{session_id}.jsonl"
        filler.write_text("", encoding="utf-8")
        target.write_text("", encoding="utf-8")
        monkeypatch.setattr(reader_codex, "_RGLOB_ROLLOUT_WARNING_THRESHOLD", 1)

        with (
            patch.object(reader_codex, "CODEX_SESSIONS_BASE", base),
            patch.object(reader_codex.Path, "rglob", return_value=iter([filler, target])),
        ):
            assert reader_codex._find_session_file(session_id, "/tmp") == target


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


class TestWriterCodex:
    def test_writes_rollout_and_history(self, tmp_path, monkeypatch):
        from app.services.session_transfer import writer_codex, reader_codex

        monkeypatch.setattr(writer_codex, "CODEX_BASE", tmp_path)
        monkeypatch.setattr(writer_codex, "CODEX_SESSIONS_DIR", tmp_path / "sessions")
        monkeypatch.setattr(writer_codex, "CODEX_HISTORY_FILE", tmp_path / "history.jsonl")

        canonical = _make_canonical(4, "claude")
        new_id = writer_codex.write_session(canonical, "/tmp/work")

        assert new_id is not None
        rollouts = list((tmp_path / "sessions").rglob(f"rollout-*-{new_id}.jsonl"))
        assert len(rollouts) == 1

        with open(rollouts[0]) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        assert lines[0]["type"] == "session_meta"
        assert lines[0]["payload"]["id"] == new_id
        assert lines[0]["payload"]["cwd"] == "/tmp/work"
        msgs = [ln for ln in lines if ln["type"] == "response_item"]
        assert len(msgs) == 4
        assert msgs[0]["payload"]["role"] == "user"
        assert msgs[0]["payload"]["content"][0]["type"] == "input_text"
        assert msgs[1]["payload"]["role"] == "assistant"
        assert msgs[1]["payload"]["content"][0]["type"] == "output_text"

        history_lines = (tmp_path / "history.jsonl").read_text().strip().splitlines()
        assert len(history_lines) == 2  # only user messages
        for line in history_lines:
            entry = json.loads(line)
            assert entry["session_id"] == new_id
            assert isinstance(entry["ts"], int)

        # round-trip: reader should be able to read the rollout we just wrote.
        monkeypatch.setattr(reader_codex, "CODEX_SESSIONS_BASE", tmp_path / "sessions")
        round_trip = reader_codex.read_session(new_id, "/tmp/work")
        assert round_trip is not None
        assert len(round_trip.messages) == 4

    def test_skips_empty(self, tmp_path, monkeypatch):
        from app.services.session_transfer import writer_codex

        monkeypatch.setattr(writer_codex, "CODEX_SESSIONS_DIR", tmp_path / "sessions")
        empty = CanonicalSession(source_cli="claude", session_id="x", workspace="/tmp")
        assert writer_codex.write_session(empty, "/tmp") is None


class TestWriterClaude:
    def test_writes_session_with_parent_chain(self, tmp_path, monkeypatch):
        from app.services.session_transfer import writer_claude, reader_claude

        monkeypatch.setattr(writer_claude, "CLAUDE_BASE", tmp_path)
        monkeypatch.setattr(writer_claude, "CLAUDE_PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(writer_claude, "CLAUDE_HISTORY_FILE", tmp_path / "history.jsonl")

        canonical = _make_canonical(4, "qwen")
        workspace = "/tmp/work"
        new_id = writer_claude.write_session(canonical, workspace)
        assert new_id is not None

        # Project key for /tmp/work matches reader's _project_key compatible candidates.
        target_files = list((tmp_path / "projects").rglob(f"{new_id}.jsonl"))
        assert len(target_files) == 1
        target = target_files[0]

        with open(target) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        assert lines[0]["type"] == "permission-mode"
        msg_lines = [ln for ln in lines if ln["type"] in ("user", "assistant")]
        assert len(msg_lines) == 4
        assert msg_lines[0]["parentUuid"] is None
        for prev, cur in zip(msg_lines, msg_lines[1:]):
            assert cur["parentUuid"] == prev["uuid"]

        # round-trip with reader.
        with patch(
            "app.services.session_transfer.reader_claude._candidate_project_dirs",
            return_value=[target.parent],
        ):
            rt = reader_claude.read_session(new_id, workspace)
        assert rt is not None
        assert len(rt.messages) == 4


class TestWriterQwen:
    def test_writes_chat_with_parts(self, tmp_path, monkeypatch):
        from app.services.session_transfer import writer_qwen, reader_qwen

        monkeypatch.setattr(writer_qwen, "QWEN_PROJECTS_BASE", tmp_path / "projects")
        canonical = _make_canonical(3, "claude")
        workspace = "/tmp/work"
        new_id = writer_qwen.write_session(canonical, workspace)
        assert new_id is not None
        targets = list((tmp_path / "projects").rglob(f"{new_id}.jsonl"))
        assert len(targets) == 1

        with open(targets[0]) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        assert len(lines) == 3
        user = lines[0]
        assert user["type"] == "user"
        assert user["message"]["role"] == "user"
        assert user["message"]["parts"][0]["text"]
        asst = lines[1]
        assert asst["type"] == "assistant"
        assert asst["message"]["role"] == "model"

        with patch(
            "app.services.session_transfer.reader_qwen._find_session_file",
            return_value=targets[0],
        ):
            rt = reader_qwen.read_session(new_id, workspace)
        assert rt is not None
        assert len(rt.messages) == 3


class TestWriterGemini:
    def test_writes_session_json(self, tmp_path, monkeypatch):
        from app.services.session_transfer import writer_gemini, reader_gemini

        monkeypatch.setattr(writer_gemini, "GEMINI_TMP_BASE", tmp_path / "tmp")
        canonical = _make_canonical(2, "qwen")
        workspace = "/tmp/work"
        new_id = writer_gemini.write_session(canonical, workspace)
        assert new_id is not None
        targets = list((tmp_path / "tmp").rglob(f"session-{new_id}.json"))
        assert len(targets) == 1

        payload = json.loads(targets[0].read_text())
        assert payload["sessionId"] == new_id
        assert len(payload["messages"]) == 2

        with patch(
            "app.services.session_transfer.reader_gemini._find_session_file",
            return_value=targets[0],
        ):
            rt = reader_gemini.read_session(new_id, workspace)
        assert rt is not None
        assert len(rt.messages) == 2
