from __future__ import annotations

import json

from app.services.cli_json_stream import (
    build_cli_json_stream_adapter,
    ClaudeJsonStreamAdapter,
    CliJsonStreamEvent,
    CliJsonStreamRecorder,
    CodexJsonStreamAdapter,
    extract_cli_evidence_from_normalized_stream,
    GeminiJsonStreamAdapter,
    GrokJsonStreamAdapter,
    QwenJsonStreamAdapter,
    recover_cli_text_from_raw_stream,
)


def test_codex_json_stream_adapter_normalizes_semantic_events() -> None:
    adapter = CodexJsonStreamAdapter()
    events = []

    for line in (
        '{"type":"thread.started","thread_id":"019d17d2-cf09-7922-8f0e-a1ac4806593f"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
    ):
        events.extend(adapter.feed_line(line))

    assert [event.kind for event in events] == [
        "session_started",
        "progress",
        "assistant_text",
        "completed",
    ]
    assert adapter.session_id == "019d17d2-cf09-7922-8f0e-a1ac4806593f"
    assert adapter.completed is True
    assert adapter.final_output_text() == "OK"


def test_cli_json_stream_recorder_writes_raw_and_normalized_files(tmp_path) -> None:
    recorder = CliJsonStreamRecorder(
        enabled=True,
        workdir=str(tmp_path),
        cli_name="codex",
        session_uid="thread:1:s1",
    )

    recorder.record_raw_line('{"type":"thread.started","thread_id":"tid"}')
    recorder.record_event(
        CliJsonStreamEvent(
            kind="session_started",
            cli_name="codex",
            session_id="tid",
            payload={"type": "thread.started", "thread_id": "tid"},
        )
    )
    recorder.close()

    root = tmp_path / ".cli-proxy" / "cli-json-stream" / "codex"
    raw_files = list(root.rglob("*.raw.jsonl"))
    normalized_files = list(root.rglob("*.normalized.jsonl"))

    assert len(raw_files) == 1
    assert len(normalized_files) == 1
    assert raw_files[0].read_text(encoding="utf-8").strip() == '{"type":"thread.started","thread_id":"tid"}'
    normalized_lines = [json.loads(line) for line in normalized_files[0].read_text(encoding="utf-8").splitlines()]
    assert normalized_lines[0]["kind"] == "session_started"
    assert normalized_lines[0]["session_id"] == "tid"
    assert normalized_lines[0]["payload"] == {"type": "thread.started", "thread_id": "tid"}


def test_cli_json_stream_recorder_is_noop_when_disabled(tmp_path) -> None:
    recorder = CliJsonStreamRecorder(
        enabled=False,
        workdir=str(tmp_path),
        cli_name="codex",
        session_uid="thread:1:s1",
    )
    recorder.record_raw_line('{"type":"thread.started"}')
    recorder.record_event(
        CliJsonStreamEvent(
            kind="raw_event",
            cli_name="codex",
            payload={"type": "thread.started"},
        )
    )
    recorder.close()

    assert not (tmp_path / ".cli-proxy" / "cli-json-stream").exists()


def test_extract_cli_evidence_from_normalized_stream_reads_repo_paths(tmp_path) -> None:
    normalized = tmp_path / "stream.normalized.jsonl"
    normalized.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "tool_event",
                        "cli_name": "qwen",
                        "text": "read_file: header.blade.php",
                        "payload": {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "call-1",
                                        "name": "read_file",
                                        "input": {"absolute_path": "/srv/cli-proxy/views/header.blade.php"},
                                    }
                                ]
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "kind": "tool_event",
                        "cli_name": "qwen",
                        "text": "search_text: registration",
                        "payload": {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "call-2",
                                        "name": "search_text",
                                        "input": {"path": "/srv/cli-proxy/views/registration.blade.php"},
                                    }
                                ]
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    evidence = extract_cli_evidence_from_normalized_stream(normalized)

    paths = [str(item.get("path") or "") for item in evidence]
    assert "/srv/cli-proxy/views/header.blade.php" in paths
    assert "/srv/cli-proxy/views/registration.blade.php" in paths


def test_gemini_json_stream_adapter_normalizes_semantic_events() -> None:
    adapter = GeminiJsonStreamAdapter()
    events = []

    for line in (
        '{"type":"init","session_id":"41d92a9a-c235-4aef-afb2-5bc8f5e8bf03","model":"auto-gemini-3"}',
        '{"type":"tool_use","tool_name":"read_file","tool_id":"tool-1","parameters":{"file_path":"README.md"}}',
        '{"type":"tool_result","tool_id":"tool-1","status":"success","output":"Read lines 1-10"}',
        '{"type":"message","role":"assistant","content":"Hel","delta":true}',
        '{"type":"message","role":"assistant","content":"lo","delta":true}',
        '{"type":"result","status":"success"}',
    ):
        events.extend(adapter.feed_line(line))

    assert [event.kind for event in events] == [
        "session_started",
        "tool_event",
        "tool_event",
        "assistant_text",
        "assistant_text",
        "completed",
    ]
    assert adapter.session_id == "41d92a9a-c235-4aef-afb2-5bc8f5e8bf03"
    assert adapter.completed is True
    assert adapter.final_output_text() == "Hello"


def test_qwen_json_stream_adapter_exposes_thinking_and_uses_result() -> None:
    adapter = QwenJsonStreamAdapter()
    events = []

    for line in (
        '{"type":"system","subtype":"init","session_id":"3e327768-c278-4ae0-87e6-1af843f3e290"}',
        '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"plan next file read"}]}}',
        (
            '{"type":"assistant","message":{"content":['
            '{"type":"tool_use","id":"call-1","name":"read_file","input":{"absolute_path":"/srv/git_projects/cli-proxy/README.md"}},'
            '{"type":"text","text":"OK"}'
            ']}}'
        ),
        (
            '{"type":"user","message":{"content":['
            '{"type":"tool_result","tool_use_id":"call-1","is_error":false,"content":"Read lines 1-10"}'
            ']}}'
        ),
        '{"type":"result","subtype":"success","is_error":false,"result":"OK"}',
    ):
        events.extend(adapter.feed_line(line))

    assert [event.kind for event in events] == [
        "session_started",
        "thinking",
        "tool_event",
        "assistant_text",
        "tool_event",
        "completed",
    ]
    assert adapter.session_id == "3e327768-c278-4ae0-87e6-1af843f3e290"
    assert adapter.completed is True
    assert adapter.final_output_text() == "OK"


def test_claude_rate_limit_event_top_level() -> None:
    adapter = ClaudeJsonStreamAdapter()
    line = (
        '{"type":"rate_limit_event","session_id":"sid-1",'
        '"usage":{"input_tokens":123,"cache_read_input_tokens":45,"cache_creation_input_tokens":6}}'
    )

    events = adapter.feed_line(line)

    assert [event.kind for event in events] == ["rate_limit"]
    rl = events[0]
    assert rl.session_id == "sid-1"
    assert rl.payload["input_tokens"] == 123
    assert rl.payload["cache_read_input_tokens"] == 45
    assert rl.payload["cache_creation_input_tokens"] == 6
    assert rl.payload["raw"]["type"] == "rate_limit_event"


def test_claude_rate_limit_event_via_system_subtype() -> None:
    adapter = ClaudeJsonStreamAdapter()
    line = (
        '{"type":"system","subtype":"rate_limit_event","session_id":"sid-2",'
        '"usage":{"input_tokens":1}}'
    )

    events = adapter.feed_line(line)

    assert [event.kind for event in events] == ["rate_limit"]
    assert events[0].session_id == "sid-2"
    assert events[0].payload["input_tokens"] == 1


def test_claude_task_notification_becomes_short_progress_event() -> None:
    adapter = ClaudeJsonStreamAdapter()
    line = json.dumps(
        {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "a86e8b34cd19c1b9b",
            "status": "stopped",
            "output_file": "/tmp/claude/tasks/a86e8b34cd19c1b9b.output",
            "summary": (
                'No completion record was found for background agent '
                '"Pre-plan миссии E1 (транспорт)" from the previous session.'
            ),
            "session_id": "993f3c1a-9c37-45c7-9d1e-1797d9ec4177",
        },
        ensure_ascii=False,
    )

    events = adapter.feed_line(line)

    assert [event.kind for event in events] == ["progress"]
    event = events[0]
    assert event.session_id == "993f3c1a-9c37-45c7-9d1e-1797d9ec4177"
    assert event.turn_id == "a86e8b34cd19c1b9b"
    assert event.text == "Claude background task stopped: Pre-plan миссии E1 (транспорт)"
    assert "{" not in event.text
    assert "output_file" not in event.text
    assert "No completion record" not in event.text


def test_claude_json_stream_adapter_normalizes_semantic_events() -> None:
    adapter = ClaudeJsonStreamAdapter()
    events = []

    for line in (
        '{"type":"system","subtype":"init","session_id":"0193a9e6-92fd-4814-a900-709b98f20eea"}',
        (
            '{"type":"assistant","message":{"content":['
            '{"type":"tool_use","id":"tool-1","name":"Bash","input":{"command":"ls -la","description":"List files"}},'
            '{"type":"text","text":"OK"}'
            ']},"session_id":"0193a9e6-92fd-4814-a900-709b98f20eea"}'
        ),
        '{"type":"system","subtype":"task_progress","description":"Finding **/*.py","session_id":"0193a9e6-92fd-4814-a900-709b98f20eea"}',
        (
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tool-1","is_error":false,"content":"Done"}]},'
            '"session_id":"0193a9e6-92fd-4814-a900-709b98f20eea"}'
        ),
        '{"type":"rate_limit_event","session_id":"0193a9e6-92fd-4814-a900-709b98f20eea"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"OK","session_id":"0193a9e6-92fd-4814-a900-709b98f20eea"}',
    ):
        events.extend(adapter.feed_line(line))

    assert [event.kind for event in events] == [
        "session_started",
        "tool_event",
        "assistant_text",
        "progress",
        "tool_event",
        "rate_limit",
        "completed",
    ]
    assert adapter.session_id == "0193a9e6-92fd-4814-a900-709b98f20eea"
    assert adapter.completed is True
    assert adapter.final_output_text() == "OK"


def test_grok_json_stream_adapter_normalizes_semantic_events() -> None:
    adapter = GrokJsonStreamAdapter()
    events = []
    lines = (
        '{"type":"thought","data":"The"}',
        '{"type":"text","data":"O"}',
        '{"type":"text","data":"K"}',
        (
            '{"type":"end","stopReason":"EndTurn",'
            '"sessionId":"019e7c90-afa6-7071-985a-9817f69f8ca8",'
            '"requestId":"e8d933ca-b9d5-4766-bab3-87e6fb3fbbf2"}'
        ),
    )

    for line in lines:
        events.extend(adapter.feed_line(line))

    assert [event.kind for event in events] == [
        "assistant_text",
        "assistant_text",
        "session_started",
        "completed",
    ]
    assert adapter.session_id == "019e7c90-afa6-7071-985a-9817f69f8ca8"
    assert adapter.completed is True
    assert adapter.final_output_text() == "OK"

    assert build_cli_json_stream_adapter("grok").cli_name == "grok"
    raw_text = "\n".join(lines)
    assert recover_cli_text_from_raw_stream("grok", raw_text) == "OK"
