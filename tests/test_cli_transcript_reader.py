import json
import time
import urllib.parse
from pathlib import Path

from app.services.cli_backends.transcript_reader import CliTranscriptReader, TranscriptLocator


def _append_jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_claude_transcript_reader_streams_clean_text_and_completion(tmp_path):
    request_id = "claude-request"
    session_id = "11111111-1111-4111-8111-111111111111"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-07-10T09:00:00Z",
            "message": {"role": "user", "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> do work"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-07-10T09:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private thought"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
                    {"type": "text", "text": "Промежуточный статус"},
                ],
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        username="claude-bot",
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    assert first.assistant_text == "Промежуточный статус"
    assert first.progress_text == "Bash"
    assert first.complete is False
    assert "private thought" not in first.assistant_text

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-07-10T09:00:02Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Финальный ответ"}],
            },
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "sessionId": session_id,
            "timestamp": "2026-07-10T09:00:03Z",
        },
    )

    final = reader.poll()

    assert final.assistant_text == "Финальный ответ"
    assert final.complete is True
    assert final.locator is not None
    assert final.locator.path == str(path.resolve())


def test_codex_transcript_reader_uses_task_complete(tmp_path):
    request_id = "codex-request"
    session_id = "22222222-2222-4222-8222-222222222222"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "10"
        / f"rollout-2026-07-10T09-00-00-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {"timestamp": "2026-07-10T09:00:00Z", "type": "session_meta", "payload": {"id": session_id}},
        {
            "timestamp": "2026-07-10T09:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"<<<CLI_PROXY_REQUEST:{request_id}>>> do work"},
        },
        {
            "timestamp": "2026-07-10T09:00:02Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Проверяю тесты"},
        },
        {
            "timestamp": "2026-07-10T09:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "Codex готов",
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is True
    assert result.assistant_text == "Codex готов"
    assert result.complete is True
    assert result.session_id == session_id


def test_grok_transcript_reader_uses_online_updates(tmp_path):
    request_id = "grok-request"
    session_id = "33333333-3333-4333-8333-333333333333"
    workspace_key = urllib.parse.quote("/srv/project", safe="")
    path = tmp_path / ".grok" / "sessions" / workspace_key / session_id / "updates.jsonl"
    _append_jsonl(
        path,
        {
            "timestamp": 1,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": f"<<<CLI_PROXY_REQUEST:{request_id}>>> do work"},
                },
            },
        },
        {
            "timestamp": 2,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "tool_call", "title": "Run tests"},
            },
        },
        {
            "timestamp": 3,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Grok готов"},
                    "_meta": {"streamStartMs": 100},
                },
            },
        },
        {
            "timestamp": 4,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "turn_completed", "stop_reason": "end_turn"},
            },
        },
    )
    reader = CliTranscriptReader(
        cli_name="grok",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is True
    assert result.progress_text == "Run tests"
    assert result.assistant_text == "Grok готов"
    assert result.complete is True


def test_transcript_reader_recovers_from_persisted_locator(tmp_path):
    request_id = "recover-request"
    session_id = "44444444-4444-4444-8444-444444444444"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "user",
            "sessionId": session_id,
            "message": {"role": "user", "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> do work"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Восстановленный ответ"}]},
        },
        {"type": "system", "subtype": "turn_duration", "sessionId": session_id},
    )
    locator = TranscriptLocator(
        provider="claude",
        path=str(path.resolve()),
        start_offset=0,
        session_id=session_id,
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
        locator=locator,
    )

    result = reader.poll()

    assert result.complete is True
    assert result.assistant_text == "Восстановленный ответ"


def test_transcript_reader_rejects_locator_outside_provider_root(tmp_path):
    request_id = "rejected-locator"
    path = tmp_path / "outside.jsonl"
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Чужой ответ"}]},
        },
        {"type": "system", "subtype": "turn_duration"},
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        home_dir=tmp_path,
        locator=TranscriptLocator(
            provider="claude",
            path=str(path),
            start_offset=0,
        ),
    )

    result = reader.poll()

    assert result.available is False
    assert result.complete is False
    assert result.assistant_text == ""
    assert result.session_id == ""


def test_transcript_reader_does_not_consume_partial_jsonl_record(tmp_path):
    request_id = "partial-request"
    session_id = "55555555-5555-4555-8555-555555555555"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "user",
            "sessionId": session_id,
            "message": {"role": "user", "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> do work"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )
    assert reader.poll().assistant_text == ""

    record = json.dumps(
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "После дописи"}]},
        },
        ensure_ascii=False,
    )
    split_at = len(record) // 2
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record[:split_at])
    assert reader.poll().assistant_text == ""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(record[split_at:] + "\n")

    assert reader.poll().assistant_text == "После дописи"
