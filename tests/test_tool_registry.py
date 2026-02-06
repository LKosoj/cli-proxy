import os
import asyncio


def test_tool_registry_registers_plain_names():
    # Ensure tool names match what the LLM calls (no "PluginPrefix.tool" dotted names).
    from config import load_config
    from agent.tooling.registry import ToolRegistry

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    names = reg.list_tool_names()
    assert "search_web" in names
    assert "run_command" in names
    assert all("." not in n for n in names)


def test_search_web_executes_via_helpers(monkeypatch):
    from config import load_config
    from agent.tooling.registry import ToolRegistry
    from agent.tooling import helpers

    async def _fake_search_web_impl(query: str, config):
        assert query == "silver price"
        return {"success": True, "output": "ok"}

    monkeypatch.setattr(helpers, "search_web_impl", _fake_search_web_impl)

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    tool = reg.plugins["search_web"]

    # ctx is mostly for common fields; search_web uses only config.
    out = asyncio.run(tool.execute({"query": "silver price"}, {"cwd": cfg.defaults.workdir}))
    assert out["success"] is True
    assert out["output"] == "ok"


def test_codeinterpreter_static_block_never_raises_regex_error():
    # Regression: internal regex patterns must never crash the tool.
    from agent.plugins.codeinterpreter import CodeInterpreterTool

    tool = CodeInterpreterTool()
    blocked, reason = tool._static_block("print('hello')")  # noqa: SLF001 (internal regression test)
    assert blocked is False
    assert reason == ""
