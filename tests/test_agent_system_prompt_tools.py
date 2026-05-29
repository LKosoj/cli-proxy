import asyncio
import os

from config import load_config
from modes.sdk.runtime.agent_core import ReActAgent
from modes.sdk.runtime.profiles import build_analyst_profile, build_default_profile
from modes.sdk.runtime.tooling.registry import ToolRegistry


def _build_agent():
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    agent = ReActAgent(cfg, reg)
    return cfg, reg, agent


def test_analyst_system_prompt_lists_only_allowed_tools(tmp_path):
    cfg, reg, agent = _build_agent()
    profile = build_analyst_profile(cfg, reg)

    prompt = agent._load_system_prompt(str(tmp_path), None, profile.allowed_tools)

    assert "Available tools in this run:" in prompt
    assert "read_file" in prompt
    assert "search_text" in prompt
    assert "write_file" not in prompt
    assert "run_command" not in prompt
    assert "Create any files and scripts" not in prompt
    assert "Execute any commands" not in prompt
    assert "do not promise file creation, file editing, shell execution" in prompt.lower()
    assert "- read_file: Read file contents. Always read before editing a file." in prompt
    assert "- search_text: Search for text/code in files using grep/ripgrep. Find definitions, usages, patterns." in prompt
    assert "- search_web:" in prompt
    assert "- web_research:" in prompt
    assert "- use_cli:" in prompt
    assert "- run_command:" not in prompt
    assert "Опирайся на описания инструментов в <TOOLS> как на основной источник" in prompt


def test_default_system_prompt_keeps_write_tools_visible(tmp_path):
    cfg, reg, agent = _build_agent()
    profile = build_default_profile(cfg, reg)

    prompt = agent._load_system_prompt(str(tmp_path), None, profile.allowed_tools)

    assert "write_file" in prompt
    assert "run_command" in prompt


def test_progressive_analyst_profile_uses_full_tool_schemas_without_get_tool_details():
    cfg, reg, agent = _build_agent()
    cfg.defaults.tool_disclosure = "progressive"
    profile = build_analyst_profile(cfg, reg)

    assert "get_tool_details" not in profile.allowed_tools

    definitions = asyncio.run(agent._build_request_tool_definitions(profile.allowed_tools))
    by_name = {item["function"]["name"]: item["function"] for item in definitions}

    assert by_name["read_file"]["parameters"]["required"] == ["path"]
    assert by_name["read_file"]["parameters"]["properties"]["path"]["type"] == "string"
    assert by_name["search_text"]["parameters"]["required"] == ["pattern"]
    assert by_name["search_text"]["parameters"]["properties"]["pattern"]["type"] == "string"


def test_progressive_default_profile_keeps_core_repo_tools_on_full_schema():
    cfg, reg, agent = _build_agent()
    cfg.defaults.tool_disclosure = "progressive"
    profile = build_default_profile(cfg, reg)

    definitions = asyncio.run(agent._build_request_tool_definitions(profile.allowed_tools))
    by_name = {item["function"]["name"]: item["function"] for item in definitions}

    assert by_name["get_tool_details"]["parameters"]["required"] == ["tool_names"]
    assert by_name["read_file"]["parameters"]["required"] == ["path"]
    assert by_name["search_text"]["parameters"]["required"] == ["pattern"]
    assert by_name["run_command"]["parameters"]["properties"] == {}
