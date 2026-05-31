import asyncio
from types import SimpleNamespace

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime.agent_core import ReActAgent
from modes.sdk.runtime.lifecycle_hooks import AgentLifecycleEvent


class _ToolRegistryStub:
    async def execute_many(self, _calls, _ctx):
        return []

    def list_tool_names(self):
        return ["run_command"]

    def record_message(self, _chat_id, _message_id):
        return None

    def resolve_question(self, _question_id, _answer):
        return False

    def build_bot_ui(self, _allowed_tools):
        return {}


class _SuccessfulToolRegistry(_ToolRegistryStub):
    async def execute_many(self, _calls, _ctx):
        return [{"success": True, "output": "tool ok"}]


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_agent_lifecycle_event_redacted_copy_removes_secret_values():
    event = AgentLifecycleEvent(
        event_type="tool_execution",
        message="Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        metadata={"headers": {"Authorization": "abcdefghijklmnop"}, "token": "secret-value"},
    )

    redacted = event.redacted_copy()

    assert redacted.redacted is True
    assert "QWxhZGRpbj" not in redacted.message
    assert redacted.metadata["headers"]["Authorization"] == "[REDACTED]"
    assert redacted.metadata["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_agent_core_lifecycle_hook_exception_does_not_fail_run(tmp_path, monkeypatch):
    react = ReActAgent(_cfg(tmp_path), _ToolRegistryStub())

    async def _fake_call_openai(_messages, _allowed_tools):
        return {"role": "assistant", "content": "done", "tool_calls": []}

    async def _failing_hook(_event):
        raise RuntimeError("hook failed")

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr(
        "modes.sdk.runtime.agent_core.runtime_chat_completion",
        lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'),
    )

    result = await react.run(
        session_id="s1",
        user_message="run task",
        session_obj=SimpleNamespace(id="s1", workdir=str(tmp_path), state_root=str(tmp_path / "state")),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-lifecycle",
        allowed_tools=["run_command"],
        lifecycle_hook=_failing_hook,
    )

    assert result.status == "ok"
    assert result.output == "done"


@pytest.mark.asyncio
async def test_agent_core_lifecycle_hook_receives_runtime_progress_events(tmp_path, monkeypatch):
    react = ReActAgent(_cfg(tmp_path), _ToolRegistryStub())
    events = []

    async def _fake_call_openai(_messages, _allowed_tools):
        return {"role": "assistant", "content": "done", "tool_calls": []}

    async def _hook(event):
        events.append(event)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr(
        "modes.sdk.runtime.agent_core.runtime_chat_completion",
        lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'),
    )

    await react.run(
        session_id="s1",
        user_message="run task",
        session_obj=SimpleNamespace(
            id="s1",
            active_mode="agent",
            workdir=str(tmp_path),
            state_root=str(tmp_path / "state"),
        ),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-lifecycle",
        allowed_tools=["run_command"],
        lifecycle_hook=_hook,
    )

    progress_phases = [event.phase for event in events if event.event_type == "runtime_progress"]
    assert "start" in progress_phases
    assert "final" in progress_phases
    assert {event.mode_id for event in events if event.event_type == "runtime_progress"} == {"agent"}


@pytest.mark.asyncio
async def test_agent_core_lifecycle_hook_drains_non_progress_events_before_return(tmp_path, monkeypatch):
    react = ReActAgent(_cfg(tmp_path), _SuccessfulToolRegistry())
    events = []
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "run_command", "arguments": "{\"cmd\":\"echo ok\"}"},
                }
            ],
        },
        {"role": "assistant", "content": "done", "tool_calls": []},
    ]

    async def _fake_call_openai(_messages, _allowed_tools):
        return messages.pop(0)

    async def _hook(event):
        if event.event_type != "runtime_progress":
            await asyncio.sleep(0.02)
        events.append(event)

    monkeypatch.setattr(react, "_call_openai", _fake_call_openai)
    monkeypatch.setattr(
        "modes.sdk.runtime.agent_core.runtime_chat_completion",
        lambda *_a, **_k: asyncio.sleep(0, result='{"claims": []}'),
    )

    result = await react.run(
        session_id="s1",
        user_message="run task",
        session_obj=SimpleNamespace(
            id="s1",
            active_mode="agent",
            workdir=str(tmp_path),
            state_root=str(tmp_path / "state"),
        ),
        bot=None,
        context=None,
        chat_id=1,
        chat_type="private",
        task_id="t-lifecycle-drain",
        allowed_tools=["run_command"],
        lifecycle_hook=_hook,
    )

    assert result.status == "ok"
    event_types = [event.event_type for event in events]
    assert event_types.count("llm_request") == 2
    assert event_types.count("llm_response") == 2
    assert "tool_execution" in event_types
