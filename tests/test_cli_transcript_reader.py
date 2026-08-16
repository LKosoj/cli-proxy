import json
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from app.services.cli_backends.transcript_reader import CliTranscriptReader, TranscriptLocator


def _iso_utc(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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
            "pendingBackgroundAgentCount": 0,
            "sessionId": session_id,
            "timestamp": "2026-07-10T09:00:03Z",
        },
    )

    turn_end = reader.poll()

    assert turn_end.assistant_text == "Финальный ответ"
    assert turn_end.complete is False

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-07-10T09:00:04Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"<<<DONE:{request_id}>>>"}],
            },
        },
    )

    final = reader.poll()

    assert final.assistant_text == "Финальный ответ"
    assert final.complete is True
    assert final.locator is not None
    assert final.locator.path == str(path.resolve())


def test_claude_transcript_reader_completes_on_done_marker(tmp_path):
    request_id = "claude-done-marker"
    session_id = "66666666-6666-4666-8666-666666666666"
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
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"Финальный ответ\n<<<DONE:{request_id}>>>"}],
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

    result = reader.poll()

    assert result.assistant_text == "Финальный ответ"
    assert result.complete is True


def test_claude_transcript_reader_waits_for_done_after_background_turn(tmp_path):
    request_id = "claude-background"
    session_id = "77777777-7777-4777-8777-777777777777"
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
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Жду агентов"}]},
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "pendingBackgroundAgentCount": 10,
            "sessionId": session_id,
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

    intermediate = reader.poll()

    assert intermediate.assistant_text == "Жду агентов"
    assert intermediate.complete is False

    _append_jsonl(
        path,
        {
            "type": "system",
            "subtype": "turn_duration",
            "pendingBackgroundAgentCount": 0,
            "sessionId": session_id,
        },
    )

    all_agents_finished = reader.poll()

    assert all_agents_finished.complete is False

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Итоговый ответ"}],
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"<<<DONE:{request_id}>>>"}],
            },
        },
    )

    final = reader.poll()

    assert final.assistant_text == "Итоговый ответ"
    assert final.complete is True


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
                "last_agent_message": "Промежуточный итог",
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

    intermediate = reader.poll()

    assert intermediate.available is True
    assert intermediate.assistant_text == "Промежуточный итог"
    assert intermediate.complete is False
    assert intermediate.session_id == session_id

    _append_jsonl(
        path,
        {
            "timestamp": "2026-07-10T09:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": f"Codex готов\n<<<DONE:{request_id}>>>",
            },
        },
    )

    result = reader.poll()

    assert result.assistant_text == "Codex готов"
    assert result.complete is True


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

    intermediate = reader.poll()

    assert intermediate.available is True
    assert intermediate.progress_text == "Run tests"
    assert intermediate.assistant_text == "Grok готов"
    assert intermediate.complete is False

    _append_jsonl(
        path,
        {
            "timestamp": 5,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"\n<<<DONE:{request_id}>>>"},
                    "_meta": {"streamStartMs": 100},
                },
            },
        },
        {
            "timestamp": 6,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "turn_completed", "stop_reason": "end_turn"},
            },
        },
    )

    result = reader.poll()

    assert result.assistant_text == "Grok готов"
    assert result.complete is True


def test_kimi_transcript_reader_merges_step_parts(tmp_path):
    request_id = "kimi-request"
    session_id = "session_55555555-5555-4555-8555-555555555555"
    path = (
        tmp_path
        / ".kimi-code"
        / "sessions"
        / "wd_project_4f3f91f6b0b4"
        / session_id
        / "agents"
        / "main"
        / "wire.jsonl"
    )
    _append_jsonl(
        path,
        {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": f"<<<CLI_PROXY_REQUEST:{request_id}>>> собери проект"}],
            "origin": {"kind": "user"},
            "time": 1_700_000_001_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "turnId": "0", "step": 1},
            "time": 1_700_000_002_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "tool.call", "name": "Bash", "toolCallId": "call-1"},
            "time": 1_700_000_003_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Собираю."}},
            "time": 1_700_000_004_000,
        },
    )
    reader = CliTranscriptReader(
        cli_name="kimi",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id="",
        home_dir=tmp_path,
    )

    intermediate = reader.poll()

    assert intermediate.available is True
    assert intermediate.recognized is True
    assert intermediate.session_id == session_id
    assert intermediate.progress_text == "Bash"
    assert intermediate.assistant_text == "Собираю."
    assert intermediate.complete is False
    # Kimi stamps epoch milliseconds; the backend compares activity in seconds.
    assert intermediate.activity_at == 1_700_000_004.0

    _append_jsonl(
        path,
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Готово."}},
            "time": 1_700_000_005_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": f"<<<DONE:{request_id}>>>"}},
            "time": 1_700_000_006_000,
        },
        {"type": "turn.ended", "turnId": 0, "reason": "completed", "time": 1_700_000_007_000},
    )

    result = reader.poll()

    assert result.assistant_text == "Собираю.\nГотово."
    assert result.complete is True


def test_kimi_transcript_reader_restarts_text_on_next_step(tmp_path):
    request_id = "kimi-steps"
    session_id = "session_66666666-6666-4666-8666-666666666666"
    path = (
        tmp_path
        / ".kimi-code"
        / "sessions"
        / "wd_project_4f3f91f6b0b4"
        / session_id
        / "agents"
        / "main"
        / "wire.jsonl"
    )
    _append_jsonl(
        path,
        {
            "type": "context.append_message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"<<<CLI_PROXY_REQUEST:{request_id}>>> проверь тесты"}],
                "origin": {"kind": "user"},
            },
            "time": 1_700_000_001_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "turnId": "0", "step": 1},
            "time": 1_700_000_002_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Запускаю тесты."}},
            "time": 1_700_000_003_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.end", "turnId": "0", "step": 1, "finishReason": "tool_use"},
            "time": 1_700_000_004_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "step.begin", "turnId": "0", "step": 2},
            "time": 1_700_000_005_000,
        },
        {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "text", "text": "Тесты зелёные."}},
            "time": 1_700_000_006_000,
        },
    )
    reader = CliTranscriptReader(
        cli_name="kimi",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id="",
        home_dir=tmp_path,
    )

    result = reader.poll()

    # Only the last step's text is the answer; the previous step is progress noise.
    assert result.assistant_text == "Тесты зелёные."
    assert result.complete is False


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
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"Восстановленный ответ\n<<<DONE:{request_id}>>>"}],
            },
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


def test_codex_tui_rollout_is_read_like_claude(tmp_path):
    """codex в tmux (source=cli) пишет тот же rollout, что и headless.

    Структура повторяет реальный файл интерактивной сессии: текст ответа в
    JSONL приходит чистым, без элементов TUI, которые засоряют экран.
    """
    request_id = "req-dbg1"
    session_id = "019fb77a-db03-7680-a5c3-cc98f766da48"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "31"
        / f"rollout-2026-07-31T12-21-49-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": "2026-07-31T12:21:49Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/tmp/tmuxdbg", "source": "cli"},
        },
        {
            "timestamp": "2026-07-31T12:21:50Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-07-31T12:21:51Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": f"<<<CLI_PROXY_REQUEST:{request_id}>>> что такое git",
            },
        },
        {
            "timestamp": "2026-07-31T12:21:55Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Смотрю историю репозитория", "phase": "commentary"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        request_id=request_id,
        workdir="/tmp/tmuxdbg",
        started_at=time.time() - 5,
        home_dir=tmp_path,
    )

    progress = reader.poll()

    assert progress.available is True
    assert progress.recognized is True
    assert progress.session_id == session_id
    assert progress.assistant_text == "Смотрю историю репозитория"
    assert progress.complete is False

    _append_jsonl(
        path,
        {
            "timestamp": "2026-07-31T12:22:05Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": f"Git — это система контроля версий.\n\n<<<DONE:{request_id}>>>",
            },
        },
        {
            "timestamp": "2026-07-31T12:22:06Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": f"Git — это система контроля версий.\n\n<<<DONE:{request_id}>>>",
            },
        },
    )

    final = reader.poll()

    assert final.complete is True
    assert final.assistant_text == "Git — это система контроля версий."
    assert "esc to interrupt" not in final.assistant_text


def test_codex_resume_keeps_appending_to_same_rollout(tmp_path):
    """codex exec resume дописывает тот же файл, поэтому поиск по session_id
    остаётся валидным между ходами."""
    session_id = "019fb756-d431-76d2-a4a6-0898b70a2882"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "31"
        / f"rollout-2026-07-31T11-42-28-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": "2026-07-31T11:42:28Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/tmp/tmuxdbg"},
        },
        {
            "timestamp": "2026-07-31T12:30:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "<<<CLI_PROXY_REQUEST:resume-req>>> ответь ok"},
        },
        {
            "timestamp": "2026-07-31T12:30:02Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "ok\n\n<<<DONE:resume-req>>>"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        request_id="resume-req",
        workdir="/tmp/tmuxdbg",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.complete is True
    assert result.assistant_text == "ok"
    assert result.locator is not None and result.locator.path.endswith(f"{session_id}.jsonl")


def _qwen_path(home: Path, session_id: str) -> Path:
    return home / ".qwen" / "projects" / "-srv-project" / "chats" / f"{session_id}.jsonl"


def test_qwen_transcript_reader_reads_parts_and_skips_thoughts(tmp_path):
    request_id = "qwen-request"
    session_id = "22222222-2222-4222-8222-222222222222"
    _append_jsonl(
        _qwen_path(tmp_path, session_id),
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-07-31T10:00:00Z",
            "message": {"role": "user", "parts": [{"text": f"<<<CLI_PROXY_REQUEST:{request_id}>>> сделай"}]},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-07-31T10:00:01Z",
            "message": {
                "role": "model",
                "parts": [
                    {"text": "надо сначала поискать", "thought": True},
                    {"functionCall": {"id": "call_1", "name": "glob", "args": {"pattern": "*.py"}}},
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-07-31T10:00:03Z",
            "message": {"role": "model", "parts": [{"text": "Готово"}]},
        },
    )
    reader = CliTranscriptReader(
        cli_name="qwen",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    assert first.recognized is True
    # Размышления модели в ответ не попадают, имя инструмента идёт в прогресс.
    assert first.assistant_text == "Готово"
    assert first.progress_text == "glob"
    assert first.complete is False

    _append_jsonl(
        _qwen_path(tmp_path, session_id),
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-07-31T10:00:05Z",
            "message": {"role": "model", "parts": [{"text": f"Итог работы\n<<<DONE:{request_id}>>>"}]},
        },
    )
    second = reader.poll()

    assert second.complete is True
    assert second.assistant_text == "Итог работы"
    assert second.session_id == session_id


def test_qwen_transcript_reader_ignores_foreign_session(tmp_path):
    """Маркер запроса обязателен: чужой чат не должен подхватываться."""
    _append_jsonl(
        _qwen_path(tmp_path, "33333333-3333-4333-8333-333333333333"),
        {
            "type": "assistant",
            "timestamp": "2026-07-31T10:00:00Z",
            "message": {"role": "model", "parts": [{"text": "чужой ответ"}]},
        },
    )
    reader = CliTranscriptReader(
        cli_name="qwen",
        request_id="qwen-missing",
        workdir="/srv/project",
        started_at=time.time() - 5,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.available is False
    assert result.assistant_text == ""


def _write_gemini_session(home: Path, session_id: str, messages: list) -> Path:
    path = home / ".gemini" / "tmp" / "project-hash" / "chats" / f"session-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sessionId": session_id,
        "projectHash": "project-hash",
        "cwd": "/srv/project",
        "startTime": "2026-07-31T10:00:00.000Z",
        "lastUpdated": "2026-07-31T10:00:10.000Z",
        "messages": messages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_gemini_transcript_reader_survives_file_rewrite(tmp_path):
    """gemini переписывает файл целиком, а не дописывает: читатель обязан
    переоткрывать его, иначе после перезаписи ответ будет потерян."""
    request_id = "gemini-request"
    session_id = "44444444-4444-4444-8444-444444444444"
    _write_gemini_session(
        tmp_path,
        session_id,
        [
            {"id": "m1", "type": "user", "content": "прошлый вопрос", "timestamp": "2026-07-31T09:00:00.000Z"},
            {"id": "m2", "type": "gemini", "content": "прошлый ответ", "timestamp": "2026-07-31T09:00:01.000Z"},
            {
                "id": "m3",
                "type": "user",
                "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> посчитай",
                "timestamp": "2026-07-31T10:00:00.000Z",
            },
        ],
    )
    reader = CliTranscriptReader(
        cli_name="gemini",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    # Ответ прошлого хода лежит выше маркера и не должен попасть в результат.
    assert first.assistant_text == ""
    assert first.complete is False

    _write_gemini_session(
        tmp_path,
        session_id,
        [
            {"id": "m1", "type": "user", "content": "прошлый вопрос", "timestamp": "2026-07-31T09:00:00.000Z"},
            {"id": "m2", "type": "gemini", "content": "прошлый ответ", "timestamp": "2026-07-31T09:00:01.000Z"},
            {
                "id": "m3",
                "type": "user",
                "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> посчитай",
                "timestamp": "2026-07-31T10:00:00.000Z",
            },
            {"id": "m4", "type": "gemini", "content": "промежуточно", "timestamp": "2026-07-31T10:00:02.000Z"},
        ],
    )
    second = reader.poll()

    assert second.assistant_text == "промежуточно"
    assert second.complete is False

    _write_gemini_session(
        tmp_path,
        session_id,
        [
            {
                "id": "m3",
                "type": "user",
                "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> посчитай",
                "timestamp": "2026-07-31T10:00:00.000Z",
            },
            {"id": "m4", "type": "gemini", "content": "промежуточно", "timestamp": "2026-07-31T10:00:02.000Z"},
            {
                "id": "m5",
                "type": "gemini",
                "content": f"Ответ: 4\n<<<DONE:{request_id}>>>",
                "timestamp": "2026-07-31T10:00:04.000Z",
            },
        ],
    )
    third = reader.poll()

    assert third.complete is True
    assert third.assistant_text == "Ответ: 4"
    assert third.session_id == session_id


def test_gemini_transcript_reader_tolerates_partially_written_file(tmp_path):
    """Файл переписывается целиком, поэтому опрос может попасть на момент записи."""
    request_id = "gemini-partial"
    session_id = "55555555-5555-4555-8555-555555555555"
    path = _write_gemini_session(
        tmp_path,
        session_id,
        [
            {
                "id": "m1",
                "type": "user",
                "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> посчитай",
                "timestamp": "2026-07-31T10:00:00.000Z",
            },
            {"id": "m2", "type": "gemini", "content": "первый ответ", "timestamp": "2026-07-31T10:00:02.000Z"},
        ],
    )
    reader = CliTranscriptReader(
        cli_name="gemini",
        request_id=request_id,
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )
    assert reader.poll().assistant_text == "первый ответ"

    path.write_text('{"sessionId": "55555555", "messages": [{"id": "m1"', encoding="utf-8")

    result = reader.poll()

    # Обрывок не должен ни падать, ни стирать уже прочитанный ответ.
    assert result.assistant_text == "первый ответ"
    assert result.complete is False


def test_gemini_transcript_reader_finds_lowercase_project_dir(tmp_path):
    """gemini заводит папку проекта в нижнем регистре, хотя рабочая директория
    может быть в смешанном — поиск не должен зависеть от её имени."""
    request_id = "gemini-case"
    session_id = "66666666-6666-4666-8666-666666666666"
    path = tmp_path / ".gemini" / "tmp" / "llmapigateway" / "chats" / f"session-{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "messages": [
                    {
                        "id": "m1",
                        "type": "user",
                        "content": f"<<<CLI_PROXY_REQUEST:{request_id}>>> посчитай",
                        "timestamp": "2026-07-31T10:00:00.000Z",
                    },
                    {
                        "id": "m2",
                        "type": "gemini",
                        "content": f"Готово\n<<<DONE:{request_id}>>>",
                        "timestamp": "2026-07-31T10:00:02.000Z",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reader = CliTranscriptReader(
        cli_name="gemini",
        request_id=request_id,
        workdir="/srv/git_projects/LLMApiGateway",
        started_at=time.time() - 5,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.complete is True
    assert result.assistant_text == "Готово"
    assert result.session_id == session_id


def test_codex_transcript_reader_attaches_to_known_thread_without_marker(tmp_path):
    """Маркер запроса может не дойти до журнала (вытеснен из хвоста огромного
    файла или записан CLI не дословно). Тред известен по session_id, поэтому
    читатель цепляется к его текущему концу и берёт всё, что появится дальше."""
    session_id = "019fade8-142e-7c91-ae5b-3dc3c03a2448"
    path = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "07"
        / "29"
        / f"rollout-2026-07-29T15-44-55-{session_id}.jsonl"
    )
    _append_jsonl(
        path,
        {
            "timestamp": "2026-07-29T15:44:55Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/project"},
        },
        {
            "timestamp": "2026-07-29T15:45:00Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "ответ прошлого хода"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        request_id="marker-lost",
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    # Всё, что записано до привязки, уже отдано пользователю и не повторяется.
    assert first.assistant_text == ""

    _append_jsonl(
        path,
        {
            "timestamp": "2026-07-29T15:46:00Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "свежий ответ"},
        },
    )
    second = reader.poll()

    assert second.assistant_text == "свежий ответ"
    assert second.recognized is True
    assert second.session_id == session_id


def test_codex_transcript_reader_follows_marker_into_new_rollout(tmp_path):
    """Если CLI продолжил тред в новом файле, поиск по session_id находит старый.
    Маркер текущего запроса приводит читателя к настоящему журналу хода."""
    session_id = "019fade8-142e-7c91-ae5b-3dc3c03a2448"
    base = tmp_path / ".codex" / "sessions" / "2026" / "07" / "29"
    _append_jsonl(
        base / f"rollout-2026-07-29T15-44-55-{session_id}.jsonl",
        {
            "timestamp": "2026-07-29T15:44:55Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/srv/project"},
        },
    )
    fresh_id = "01a00a64-0fe1-7fa3-a3d0-23124f88d3a4"
    _append_jsonl(
        base / f"rollout-2026-07-29T16-00-00-{fresh_id}.jsonl",
        {
            "timestamp": "2026-07-29T16:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "<<<CLI_PROXY_REQUEST:moved-req>>> посчитай"},
        },
        {
            "timestamp": "2026-07-29T16:00:05Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "ответ из нового файла"},
        },
    )
    reader = CliTranscriptReader(
        cli_name="codex",
        request_id="moved-req",
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    result = reader.poll()

    assert result.assistant_text == "ответ из нового файла"
    assert result.locator is not None and result.locator.path.endswith(f"{fresh_id}.jsonl")
    # Идентификатор треда переезжает вслед за журналом.
    assert result.session_id == fresh_id


def test_claude_transcript_reader_attaches_to_known_session_without_marker(tmp_path):
    """Привязка без маркера работает одинаково для всех CLI с журналом-дописью."""
    session_id = "77777777-7777-4777-8777-777777777777"
    path = tmp_path / ".claude" / "projects" / "-srv-project" / f"{session_id}.jsonl"
    _append_jsonl(
        path,
        {
            "type": "assistant",
            "timestamp": "2026-07-31T09:00:00Z",
            "message": {"content": [{"type": "text", "text": "ответ прошлого хода"}]},
        },
    )
    reader = CliTranscriptReader(
        cli_name="claude",
        request_id="marker-lost",
        workdir="/srv/project",
        started_at=time.time() - 5,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    assert first.assistant_text == ""

    _append_jsonl(
        path,
        {
            "type": "assistant",
            "timestamp": "2026-07-31T10:00:00Z",
            "message": {"content": [{"type": "text", "text": "свежий ответ"}]},
        },
    )
    second = reader.poll()

    assert second.assistant_text == "свежий ответ"


def test_gemini_transcript_reader_uses_start_time_without_marker(tmp_path):
    """gemini переписывает файл целиком, поэтому без маркера границей служит
    время старта запроса, а не байтовое смещение."""
    session_id = "88888888-8888-4888-8888-888888888888"
    started_at = time.time()
    _write_gemini_session(
        tmp_path,
        session_id,
        [
            {"id": "m1", "type": "user", "content": "прошлый вопрос", "timestamp": "2026-07-31T09:00:00.000Z"},
            {"id": "m2", "type": "gemini", "content": "прошлый ответ", "timestamp": "2026-07-31T09:00:01.000Z"},
        ],
    )
    reader = CliTranscriptReader(
        cli_name="gemini",
        request_id="gemini-marker-lost",
        workdir="/srv/project",
        started_at=started_at,
        session_id=session_id,
        home_dir=tmp_path,
    )

    first = reader.poll()

    assert first.available is True
    assert first.assistant_text == ""

    _write_gemini_session(
        tmp_path,
        session_id,
        [
            {"id": "m1", "type": "user", "content": "прошлый вопрос", "timestamp": "2026-07-31T09:00:00.000Z"},
            {"id": "m2", "type": "gemini", "content": "прошлый ответ", "timestamp": "2026-07-31T09:00:01.000Z"},
            {
                "id": "m3",
                "type": "gemini",
                "content": "свежий ответ",
                "timestamp": _iso_utc(started_at + 1),
            },
        ],
    )
    second = reader.poll()

    assert second.assistant_text == "свежий ответ"
