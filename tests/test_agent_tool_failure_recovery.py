import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from agent.agent_core import ReActAgent


class _FakeToolRegistry:
    def __init__(self):
        self._calls = 0

    def list_tool_names(self):
        return ["flaky_tool"]

    async def execute_many(self, calls, ctx):
        self._calls += 1
        # First iteration: tool fails. Second iteration: tool succeeds.
        if self._calls == 1:
            return [{"success": False, "error": "boom"} for _ in calls]
        return [{"success": True, "output": "ok"} for _ in calls]


def test_agent_does_not_stop_on_single_tool_failure(tmp_path, monkeypatch):
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

    seq = {"i": 0}

    async def _fake_call_openai(self, messages, allowed_tools):
        seq["i"] += 1
        if seq["i"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "flaky_tool", "arguments": "{}"},
                    }
                ],
            }
        if seq["i"] == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "flaky_tool", "arguments": "{}"},
                    }
                ],
            }
        return {"role": "assistant", "content": "done", "tool_calls": []}

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

    assert res.status in ("ok", "partial")
    assert res.output.strip() == "done"

