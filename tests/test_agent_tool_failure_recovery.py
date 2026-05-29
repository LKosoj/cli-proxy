import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.sdk.runtime.agent_core import ReActAgent


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


class _SearchLikeToolRegistry:
    def list_tool_names(self):
        return ["search_text"]

    async def execute_many(self, calls, ctx):
        # Simulate search output that includes "BLOCKED:" as plain file content.
        return [{"success": True, "output": "reason: BLOCKED: sample policy string"} for _ in calls]


def test_agent_does_not_mark_successful_search_output_as_blocked(tmp_path, monkeypatch):
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

    agent = ReActAgent(cfg, _SearchLikeToolRegistry())
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
                        "function": {"name": "search_text", "arguments": "{\"pattern\":\"BLOCKED\"}"},
                    }
                ],
            }
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))

    session = types.SimpleNamespace(workdir=str(tmp_path), id="s1")
    res = asyncio.run(
        agent.run(
            session_id="s1",
            user_message="analyze repo",
            session_obj=session,
            bot=None,
            context=None,
            chat_id=None,
            chat_type=None,
            task_id="step3",
            allowed_tools=["All"],
        )
    )

    assert res.status in ("ok", "partial")
    assert res.output.strip() == "done"


class _PolicyBlockedToolRegistry:
    def list_tool_names(self):
        return ["run_command"]

    async def execute_many(self, calls, ctx):
        return [
            {
                "success": False,
                "error": "🚫 BLOCKED: forbidden command",
                "blocked": True,
                "block_reason": "forbidden command",
            }
            for _ in calls
        ]


def test_agent_stops_on_explicit_policy_block_flag(tmp_path, monkeypatch):
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

    agent = ReActAgent(cfg, _PolicyBlockedToolRegistry())

    async def _fake_call_openai(self, messages, allowed_tools):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{\"command\":\"env\"}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{\"command\":\"printenv\"}"},
                },
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{\"command\":\"set\"}"},
                },
            ],
        }

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))

    session = types.SimpleNamespace(workdir=str(tmp_path), id="s1")
    res = asyncio.run(
        agent.run(
            session_id="s1",
            user_message="run blocked commands",
            session_obj=session,
            bot=None,
            context=None,
            chat_id=None,
            chat_type=None,
            task_id="step3",
            allowed_tools=["All"],
        )
    )

    assert res.status == "blocked"
    assert "Multiple blocked commands detected" in res.output


class _WebResearchRegistry:
    def __init__(self):
        self.calls = 0

    def list_tool_names(self):
        return ["web_research"]

    async def execute_many(self, calls, ctx):
        self.calls += 1
        return [{"success": True, "output": "research ok"} for _ in calls]


def test_agent_blocks_repeated_web_research_query_within_single_run(tmp_path, monkeypatch):
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

    reg = _WebResearchRegistry()
    agent = ReActAgent(cfg, reg)

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
                        "function": {"name": "web_research", "arguments": "{\"query\":\"Python asyncio\"}"},
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
                        "function": {"name": "web_research", "arguments": "{\"query\":\" python   asyncio  \"}"},
                    }
                ],
            }
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(agent, "_call_openai", types.MethodType(_fake_call_openai, agent))

    session = types.SimpleNamespace(workdir=str(tmp_path), id="s1")
    res = asyncio.run(
        agent.run(
            session_id="s1",
            user_message="analyze topic",
            session_obj=session,
            bot=None,
            context=None,
            chat_id=None,
            chat_type=None,
            task_id="step_web_research",
            allowed_tools=["All"],
        )
    )

    # The first query is executed, the repeated query is blocked before ToolRegistry execution.
    assert reg.calls == 1
    assert res.status in ("ok", "partial")
    assert res.output.strip() == "done"
