import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime.agent_core import ReActAgent


class _AlwaysFailToolRegistry:
    def __init__(self):
        self.calls = 0

    def list_tool_names(self):
        return ["run_command"]

    async def execute_many(self, calls, ctx):
        self.calls += 1
        return [{"success": False, "error": "boom"} for _ in calls]


def test_agent_stops_on_repeated_identical_tool_calls(tmp_path, monkeypatch):
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            openai_api_key="test-key",
            openai_model="test-model",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )

    reg = _AlwaysFailToolRegistry()
    agent = ReActAgent(cfg, reg)

    async def _fake_call_openai(self, messages, allowed_tools):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{\"command\":\"pytest -q\"}"},
                }
            ],
        }

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))

    session = types.SimpleNamespace(workdir=str(tmp_path), id="s1")
    res = asyncio.run(
        agent.run(
            session_id="s1",
            user_message="run tests",
            session_obj=session,
            bot=None,
            context=None,
            chat_id=None,
            chat_type=None,
            task_id="step1",
            allowed_tools=["All"],
        )
    )

    assert res.status == "error"
    assert "Прогресс остановился" in res.output
    # Should stop early (not spin until max iterations).
    assert reg.calls == 3
