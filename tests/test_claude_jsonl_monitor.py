import json

from app.services.claude_jsonl_monitor import (
    ClaudeJsonlMonitor,
    ClaudeSessionMonitor,
    ClaudeTranscriptEvent,
)
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


def _build_session(tmp_path) -> Session:
    tool = ToolConfig(
        name="claude",
        mode="headless",
        cmd=["claude", "-p", "{prompt}"],
        headless_cmd=["claude", "-p", "{prompt}"],
    )
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"claude": tool},
        defaults=DefaultsConfig(workdir=str(tmp_path), state_path=str(tmp_path / "state.json")),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return Session(
        id="s1",
        tool=tool,
        workdir=str(tmp_path),
        idle_timeout_sec=10,
        config=cfg,
        chat_id=777,
    )


def test_claude_event_from_jsonl_line_extracts_progress_items():
    line = json.dumps(
        {
            "uuid": "event-1",
            "sessionId": "sess-123",
            "timestamp": "2026-03-01T22:43:36.510Z",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/srv/git_projects/cli-proxy/docs/anl/MODES_ANALYSIS.md"},
                    },
                    {"type": "thinking", "thinking": "Need to inspect the file first."},
                    {"type": "text", "text": "Done"},
                ],
            },
        }
    )

    event = ClaudeTranscriptEvent.from_jsonl_line(line)

    assert event is not None
    assert event.session_id == "sess-123"
    assert event.progress_items == [
        "🔧 Read(docs/anl/MODES_ANALYSIS.md)",
        "🤔 Need to inspect the file first.",
        "Done",
    ]


def test_claude_event_from_jsonl_line_summarizes_tool_result():
    line = json.dumps(
        {
            "uuid": "event-2",
            "sessionId": "sess-456",
            "timestamp": "2026-03-01T22:43:36.755Z",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "call_1",
                        "type": "tool_result",
                        "content": "",
                    }
                ],
            },
            "toolUseResult": {"numFiles": 4},
        }
    )

    event = ClaudeTranscriptEvent.from_jsonl_line(line)

    assert event is not None
    assert event.progress_items == ["✅ Found 4 files"]


def test_claude_event_from_jsonl_line_extracts_agent_progress_items():
    line = json.dumps(
        {
            "uuid": "event-2b",
            "sessionId": "sess-789",
            "timestamp": "2026-03-01T22:43:36.755Z",
            "type": "progress",
            "data": {
                "type": "agent_progress",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Glob", "input": {"pattern": "src/**/*.py"}},
                        {"type": "text", "text": "Searching the project."},
                    ],
                },
            },
        }
    )

    event = ClaudeTranscriptEvent.from_jsonl_line(line)

    assert event is not None
    assert event.progress_items == [
        "🔧 Glob(src/**/*.py)",
        "Searching the project.",
    ]


def test_claude_session_monitor_enriches_hook_progress_with_tool_context(tmp_path):
    jsonl_file = tmp_path / "claude.jsonl"
    jsonl_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "uuid": "event-1",
                        "sessionId": "sess-123",
                        "timestamp": "2026-03-01T22:43:36.510Z",
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call_1",
                                    "name": "Read",
                                    "input": {"file_path": "/srv/git_projects/cli-proxy/modes/analyst/mode.py"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "uuid": "event-2",
                        "sessionId": "sess-123",
                        "timestamp": "2026-03-01T22:43:36.755Z",
                        "type": "progress",
                        "toolUseID": "call_1",
                        "data": {
                            "type": "hook_progress",
                            "hookName": "PostToolUse:Read",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monitor = ClaudeSessionMonitor(session_id="sess-123", jsonl_path=jsonl_file)

    events = monitor.read_new_events()

    assert len(events) == 2
    assert events[1].progress_items == ["⏳ PostToolUse:Read(modes/analyst/mode.py)"]


def test_claude_monitor_discovers_new_session_file(tmp_path):
    monitor = ClaudeJsonlMonitor(workdir="/srv/git_projects/cli-proxy", poll_interval=0.01)
    monitor._candidate_project_roots = lambda: [tmp_path]

    project_dir = tmp_path / monitor._project_key_candidates()[0]
    project_dir.mkdir()

    existing = project_dir / "old-session.jsonl"
    existing.write_text(
        '{"type":"queue-operation","operation":"enqueue","sessionId":"old-session"}\n',
        encoding="utf-8",
    )
    monitor._known_file_sizes = monitor._snapshot_known_files()

    seen: list[str] = []
    monitor.session_callback = seen.append

    new_file = project_dir / "new-session.jsonl"
    new_file.write_text(
        '{"type":"queue-operation","operation":"enqueue","sessionId":"new-session"}\n',
        encoding="utf-8",
    )

    monitor._discover_active_session(project_dir)

    assert monitor.get_latest_session_id() == "new-session"
    assert seen == ["new-session"]
    assert monitor.monitors["new-session"].last_position == 0


def test_claude_monitor_prefers_project_dir_with_active_session_transcript(tmp_path):
    monitor = ClaudeJsonlMonitor(
        workdir="/srv/git_projects/cli-proxy",
        poll_interval=0.01,
        session_id="target-session",
    )
    roots = [tmp_path / "root-projects", tmp_path / "claude-projects"]
    monitor._candidate_project_roots = lambda: roots

    project_key = monitor._project_key_candidates()[0]
    root_project_dir = roots[0] / project_key
    claude_project_dir = roots[1] / project_key
    root_project_dir.mkdir(parents=True)
    claude_project_dir.mkdir(parents=True)

    (root_project_dir / "other-session.jsonl").write_text(
        '{"type":"queue-operation","operation":"enqueue","sessionId":"other-session"}\n',
        encoding="utf-8",
    )
    (claude_project_dir / "target-session.jsonl").write_text(
        '{"type":"queue-operation","operation":"enqueue","sessionId":"target-session"}\n',
        encoding="utf-8",
    )

    assert monitor._find_project_dir() == claude_project_dir


def test_claude_monitor_uses_previous_size_for_resumed_session(tmp_path):
    monitor = ClaudeJsonlMonitor(workdir="/srv/git_projects/cli-proxy", poll_interval=0.01)
    monitor._candidate_project_roots = lambda: [tmp_path]

    project_dir = tmp_path / monitor._project_key_candidates()[0]
    project_dir.mkdir()

    resumed = project_dir / "resume-session.jsonl"
    resumed.write_text(
        '{"type":"queue-operation","operation":"enqueue","sessionId":"resume-session"}\n',
        encoding="utf-8",
    )
    previous_size = resumed.stat().st_size
    monitor._known_file_sizes = monitor._snapshot_known_files()

    resumed.write_text(
        resumed.read_text(encoding="utf-8")
        + '{"type":"assistant","sessionId":"resume-session","message":{"content":[{"type":"text","text":"done"}]}}\n',
        encoding="utf-8",
    )

    seen: list[str] = []
    monitor.session_callback = seen.append
    monitor._discover_active_session(project_dir)

    assert monitor.get_latest_session_id() == "resume-session"
    assert seen == ["resume-session"]
    assert monitor.monitors["resume-session"].last_position == previous_size


def test_claude_monitor_attaches_subagent_transcripts(tmp_path):
    monitor = ClaudeJsonlMonitor(
        workdir="/srv/git_projects/cli-proxy",
        poll_interval=0.01,
        session_id="root-session",
    )
    monitor._candidate_project_roots = lambda: [tmp_path]

    project_dir = tmp_path / monitor._project_key_candidates()[0]
    subagents_dir = project_dir / "root-session" / "subagents"
    subagents_dir.mkdir(parents=True)

    root_file = project_dir / "root-session.jsonl"
    root_file.write_text(
        '{"type":"assistant","sessionId":"root-session","message":{"content":[{"type":"text","text":"root"}]}}\n',
        encoding="utf-8",
    )
    sub_file = subagents_dir / "agent-1.jsonl"
    sub_file.write_text(
        '{"type":"assistant","sessionId":"root-session","message":{"content":[{"type":"text","text":"sub"}]}}\n',
        encoding="utf-8",
    )

    monitor._ensure_session_monitors(project_dir)

    assert "root-session" in monitor.monitors
    assert "root-session:subagent:agent-1" in monitor.monitors


def test_session_claude_event_updates_resume_and_ticks(tmp_path):
    session = _build_session(tmp_path)
    session._on_claude_session_detected("claude-session-1")

    line = json.dumps(
        {
            "uuid": "event-3",
            "sessionId": "claude-session-1",
            "timestamp": "2026-03-01T22:43:42.700Z",
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read"},
                    {"type": "text", "text": "Completed the check."},
                ],
            },
        }
    )
    event = ClaudeTranscriptEvent.from_jsonl_line(line)
    assert event is not None

    session._on_claude_event(event)

    assert session.resume_token == "claude-session-1"
    assert session.tick_seen == 2
    assert session.last_tick_value == "Completed the check."
