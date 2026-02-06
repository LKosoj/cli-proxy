import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from agent.agent_core import ReActAgent


class _FakeToolRegistry:
    def list_tool_names(self):
        return ["noop"]

    async def execute_many(self, calls, ctx):
        # Always succeed so the agent doesn't stop early with "all tools failed".
        out = "ok" * 200  # non-empty output
        return [{"success": True, "output": out} for _ in calls]


def test_max_iterations_is_partial_and_returns_progress(tmp_path, monkeypatch):
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

    agent = ReActAgent(cfg, _FakeToolRegistry())

    # Force the agent to always ask for a tool call so it hits the iteration cap.
    async def _fake_call_openai(self, messages, allowed_tools):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                }
            ],
        }

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))

    session = types.SimpleNamespace(workdir=str(tmp_path), id="s1")
    res = asyncio.run(
        agent.run(
            session_id="s1",
            user_message="do stuff",
            session_obj=session,
            bot=None,
            context=None,
            chat_id=None,
            chat_type=None,
            task_id="step1",
            allowed_tools=["All"],
        )
    )

    assert res.status == "partial"
    assert "Достигнут лимит итераций" in res.output
    assert "Последние вызовы инструментов" in res.output
