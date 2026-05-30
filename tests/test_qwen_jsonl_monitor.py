"""Tests for Qwen JSONL monitor service."""

import asyncio
import json

from app.services.qwen_jsonl_monitor import (
    QwenChatEvent,
    QwenJsonlMonitor,
    QwenSessionMonitor,
    extract_progress_text,
)


def test_event_from_jsonl_line():
    """Test parsing a single JSONL line."""
    line = json.dumps({
        "uuid": "test-123",
        "sessionId": "sess-456",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "assistant",
        "message": {
            "role": "model",
            "parts": [
                {"text": "Hello", "thought": True},
                {"text": "World"}
            ]
        }
    })

    event = QwenChatEvent.from_jsonl_line(line)

    assert event is not None
    assert event.uuid == "test-123"
    assert event.session_id == "sess-456"
    assert event.event_type == "assistant"
    assert event.thought == "Hello"
    assert event.text == "World"


def test_extract_progress_text_thought():
    """Test extracting progress text from thought event."""
    line = json.dumps({
        "uuid": "test-1",
        "sessionId": "sess-1",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "assistant",
        "message": {
            "role": "model",
            "parts": [{"text": "Thinking about the problem", "thought": True}]
        }
    })

    event = QwenChatEvent.from_jsonl_line(line)
    progress = extract_progress_text(event)

    assert progress is not None
    assert "🤔" in progress
    assert "Thinking" in progress


def test_extract_progress_text_tool_call():
    """Test extracting progress text from tool call event."""
    # Tool calls in Qwen JSONL are type: system with subtype: ui_telemetry
    line = json.dumps({
        "uuid": "test-2",
        "sessionId": "sess-1",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "system",
        "subtype": "ui_telemetry",
        "systemPayload": {
            "uiEvent": {
                "function_name": "run_shell_command",
                "function_args": {"command": "echo 'hello world'"},
            }
        }
    })

    event = QwenChatEvent.from_jsonl_line(line)
    progress = extract_progress_text(event)

    assert progress is not None
    assert "🔧" in progress
    assert "run_shell_command" in progress
    assert "echo 'hello world'" in progress


def test_session_monitor_read_events(tmp_path):
    """Test reading events from JSONL file."""
    jsonl_file = tmp_path / "test.jsonl"

    # Write some events
    events_data = [
        {
            "uuid": "e1", "sessionId": "s1",
            "timestamp": "2026-03-01T20:00:00.000Z", "type": "user",
            "message": {"role": "user", "parts": [{"text": "Hello"}]}
        },
        {
            "uuid": "e2", "sessionId": "s1",
            "timestamp": "2026-03-01T20:00:01.000Z", "type": "assistant",
            "message": {"role": "model", "parts": [{"text": "Hi", "thought": True}]}
        },
    ]

    with open(jsonl_file, "w") as f:
        for event in events_data:
            f.write(json.dumps(event) + "\n")

    monitor = QwenSessionMonitor(
        session_id="s1",
        jsonl_path=jsonl_file,
    )

    # Read events
    events = monitor.read_new_events()

    assert len(events) == 2
    assert events[0].uuid == "e1"
    assert events[1].uuid == "e2"

    # Reading again should return no new events
    events2 = monitor.read_new_events()
    assert len(events2) == 0


def test_session_monitor_deduplication(tmp_path):
    """Test that events are not duplicated."""
    jsonl_file = tmp_path / "test.jsonl"

    # Write initial events
    with open(jsonl_file, "w") as f:
        f.write(json.dumps({
            "uuid": "e1", "sessionId": "s1",
            "timestamp": "2026-03-01T20:00:00.000Z", "type": "user",
            "message": {"role": "user", "parts": []}
        }) + "\n")

    monitor = QwenSessionMonitor(
        session_id="s1",
        jsonl_path=jsonl_file,
    )

    # Read once
    events1 = monitor.read_new_events()
    assert len(events1) == 1

    # Append more events
    with open(jsonl_file, "a") as f:
        f.write(json.dumps({
            "uuid": "e2", "sessionId": "s1",
            "timestamp": "2026-03-01T20:00:01.000Z", "type": "assistant",
            "message": {"role": "model", "parts": []}
        }) + "\n")

    # Read again - should only get new event
    events2 = monitor.read_new_events()
    assert len(events2) == 1
    assert events2[0].uuid == "e2"


def test_qwen_monitor_discover_sessions(tmp_path):
    """Test session discovery."""
    chat_dir = tmp_path / "chats"
    chat_dir.mkdir()

    # Create some session files
    (chat_dir / "session1.jsonl").write_text(
        '{"uuid":"e1","sessionId":"session1","timestamp":"2026-03-01T20:00:00.000Z",'
        '"type":"user","message":{"role":"user","parts":[]}}\n'
    )
    (chat_dir / "session2.jsonl").write_text(
        '{"uuid":"e2","sessionId":"session2","timestamp":"2026-03-01T20:00:00.000Z",'
        '"type":"user","message":{"role":"user","parts":[]}}\n'
    )

    # Monkey-patch QWEN_CHAT_BASE_DIR
    import app.services.qwen_jsonl_monitor as mon_module
    original_base = mon_module.QWEN_CHAT_BASE_DIR
    mon_module.QWEN_CHAT_BASE_DIR = tmp_path

    try:
        monitor = QwenJsonlMonitor(workdir=str(tmp_path))
        monitor._find_chat_dir = lambda: chat_dir  # Override to use our test dir

        monitor._discover_sessions(chat_dir)

        assert len(monitor.monitors) == 2
        assert "session1" in monitor.monitors
        assert "session2" in monitor.monitors
    finally:
        mon_module.QWEN_CHAT_BASE_DIR = original_base


def test_qwen_monitor_find_chat_dir_uses_project_key_candidates_without_cwd_fallback(tmp_path):
    import app.services.qwen_jsonl_monitor as mon_module

    original_base = mon_module.QWEN_CHAT_BASE_DIR
    mon_module.QWEN_CHAT_BASE_DIR = tmp_path / ".qwen" / "projects"

    try:
        chat_dir = mon_module.QWEN_CHAT_BASE_DIR / "-srv-git-projects-LLMApiGateway" / "chats"
        chat_dir.mkdir(parents=True)
        (chat_dir / "session.jsonl").write_text('{"uuid":"e1","sessionId":"s1","type":"user"}\n', encoding="utf-8")

        monitor = QwenJsonlMonitor(workdir="/srv/git_projects/LLMApiGateway")

        assert monitor._project_key_candidates() == [
            "-srv-git_projects-LLMApiGateway",
            "-srv-git-projects-LLMApiGateway",
        ]
        assert monitor._find_chat_dir() == chat_dir
    finally:
        mon_module.QWEN_CHAT_BASE_DIR = original_base


def test_extract_progress_text_tool_result():
    """Test extracting progress text from tool result."""
    line = json.dumps({
        "uuid": "test-3",
        "sessionId": "sess-1",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "tool_result",
        "toolCallResult": {
            "resultDisplay": "Successfully created file.txt"
        }
    })

    event = QwenChatEvent.from_jsonl_line(line)
    progress = extract_progress_text(event)

    assert progress is not None
    assert "✅" in progress
    assert "Successfully" in progress


def test_extract_progress_text_truncation():
    """Test that long tool results are truncated."""
    long_result = "x" * 200
    line = json.dumps({
        "uuid": "test-4",
        "sessionId": "sess-1",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "tool_result",
        "toolCallResult": {
            "resultDisplay": long_result
        }
    })

    event = QwenChatEvent.from_jsonl_line(line)
    progress = extract_progress_text(event)

    assert progress is not None
    assert len(progress) < 120  # "✅ " + 100 chars + "..."


def test_extract_progress_text_tool_call_with_args():
    line = json.dumps({
        "uuid": "test-args-1",
        "sessionId": "sess-1",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "system",
        "subtype": "ui_telemetry",
        "systemPayload": {
            "uiEvent": {
                "function_name": "read_file",
                "function_args": {
                    "absolute_path": "/srv/git_projects/cli-proxy/app/services/qwen_jsonl_monitor.py"
                },
            }
        }
    })

    event = QwenChatEvent.from_jsonl_line(line)
    progress = extract_progress_text(event)

    assert progress == "🔧 read_file(app/services/qwen_jsonl_monitor.py)"


def test_extract_progress_text_tool_result_prefers_detailed_error():
    line = json.dumps({
        "uuid": "test-err-1",
        "sessionId": "sess-1",
        "timestamp": "2026-03-01T20:00:00.000Z",
        "type": "tool_result",
        "message": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": "call_1",
                        "name": "read_file",
                        "response": {
                            "error": "File not found: /srv/git_projects/cli-proxy/missing.py"
                        },
                    }
                }
            ],
        },
        "toolCallResult": {
            "status": "error",
            "resultDisplay": "File not found",
        },
    })

    event = QwenChatEvent.from_jsonl_line(line)
    progress = extract_progress_text(event)

    assert progress is not None
    assert progress.startswith("❌ read_file: ")
    assert "missing.py" in progress


# --- тесты метода stop() ---

def test_stop_before_start_is_safe():
    """stop() до start() не бросает исключений и не меняет состояние."""
    monitor = QwenJsonlMonitor(workdir="/tmp")
    monitor.stop()  # не должен бросать
    assert monitor._task is None
    assert not monitor._running


def test_double_stop_is_safe():
    """Двойной stop() безопасен."""
    monitor = QwenJsonlMonitor(workdir="/tmp")
    monitor.stop()
    monitor.stop()
    assert monitor._task is None


async def _run_stop_from_loop(workdir: str) -> bool:
    """Запускаем monitor, вызываем stop() из работающего event loop — должно пройти без RuntimeError."""
    monitor = QwenJsonlMonitor(workdir=workdir)
    monitor.start()
    try:
        monitor.stop()
    except RuntimeError:
        return False
    return True


def test_stop_from_running_loop_no_runtime_error():
    """stop() из работающего event loop не бросает RuntimeError и обнуляет _task."""
    result = asyncio.run(_run_stop_from_loop("/tmp"))
    assert result, "stop() бросил RuntimeError в работающем loop"


async def _run_task_cancelled_after_stop() -> bool:
    """После stop() + await sleep(0) задача должна быть отменена."""
    monitor = QwenJsonlMonitor(workdir="/tmp")
    monitor.start()
    task = monitor._task
    monitor.stop()
    # monitor._task уже None, но сама корутина ещё pending — даём ей обработать cancel
    await asyncio.sleep(0)
    return task is not None and task.cancelled()


def test_task_cancelled_after_stop():
    """После stop() + await asyncio.sleep(0) задача .cancelled()."""
    result = asyncio.run(_run_task_cancelled_after_stop())
    assert result, "Задача не перешла в состояние cancelled после stop()"
