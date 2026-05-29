import os
import asyncio


def test_tool_registry_registers_plain_names():
    # Ensure tool names match what the LLM calls (no "PluginPrefix.tool" dotted names).
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    names = reg.list_tool_names()
    assert "search_web" in names
    assert "run_command" in names
    assert all("." not in n for n in names)


def test_search_web_executes_via_helpers(monkeypatch):
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry
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


def test_registry_execute_uses_ctx_tool_timeout_override(monkeypatch):
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry
    from modes.sdk.runtime.tooling.spec import ToolSpec
    from agent.plugins.base import ToolPlugin
    import modes.sdk.runtime.tooling.registry as registry_mod

    class _ProbeTool(ToolPlugin):
        def get_spec(self) -> ToolSpec:
            return ToolSpec(
                name="probe_timeout_tool",
                description="probe",
                parameters={"type": "object", "properties": {}},
                timeout_ms=120_000,
            )

        async def execute(self, args, ctx):
            return {"success": True, "output": "ok"}

    captured = {}
    original_wait_for = registry_mod.asyncio.wait_for

    async def _fake_wait_for(awaitable, timeout=None):
        captured["timeout"] = timeout
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(registry_mod.asyncio, "wait_for", _fake_wait_for)

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)
    reg.register(_ProbeTool())

    out = asyncio.run(
        reg.execute(
            "probe_timeout_tool",
            {},
            {"allowed_tools": ["All"], "tool_timeouts_ms": {"probe_timeout_tool": 7000}},
        )
    )
    assert out["success"] is True
    assert captured["timeout"] == 7.0


def test_registry_execute_rejects_non_object_args_without_crash():
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    out = asyncio.run(reg.execute("run_command", "not-a-json-object", {"allowed_tools": ["All"]}))
    assert out["success"] is False
    assert "Invalid args for run_command" in str(out.get("error") or "")


def test_registry_execute_parses_object_args_from_string_json(tmp_path):
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    out = asyncio.run(
        reg.execute(
            "run_command",
            "{\"command\":\"echo ok\"}",
            {"allowed_tools": ["All"], "cwd": str(tmp_path)},
        )
    )
    assert out["success"] is True


def test_registry_execute_coerces_numeric_string_args_for_read_file(tmp_path):
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    sample = tmp_path / "sample.txt"
    sample.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    out = asyncio.run(
        reg.execute(
            "read_file",
            "{\"path\":\"sample.txt\",\"offset\":\"2\",\"limit\":\"2\"}",
            {"allowed_tools": ["All"], "cwd": str(tmp_path)},
        )
    )

    assert out["success"] is True
    assert out["output"] == "two\nthree"


def test_registry_execute_keeps_invalid_numeric_string_args_rejected_for_read_file(tmp_path):
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    sample = tmp_path / "sample.txt"
    sample.write_text("one\ntwo\nthree\n", encoding="utf-8")

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    out = asyncio.run(
        reg.execute(
            "read_file",
            {"path": "sample.txt", "offset": "second", "limit": "2"},
            {"allowed_tools": ["All"], "cwd": str(tmp_path)},
        )
    )

    assert out["success"] is False
    assert "invalid type for offset: expected number" in str(out.get("error") or "")


def test_tool_registry_build_bot_ui_defaults_to_all_tools():
    from config import load_config
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    reg = ToolRegistry(cfg)

    ui_default = reg.build_bot_ui()
    ui_empty = reg.build_bot_ui([])
    ui_all = reg.build_bot_ui(["All"])

    assert ui_default["plugin_menu"] == ui_all["plugin_menu"] == ui_empty["plugin_menu"]
    assert len(ui_default["message_handlers"]) == len(ui_all["message_handlers"]) == len(ui_empty["message_handlers"])
    assert len(ui_default["inline_handlers"]) == len(ui_all["inline_handlers"]) == len(ui_empty["inline_handlers"])
    assert sorted(h.get("plugin_name") for h in ui_default["message_handlers"]) == sorted(
        h.get("plugin_name") for h in ui_all["message_handlers"]
    ) == sorted(h.get("plugin_name") for h in ui_empty["message_handlers"])
    assert sorted(h.get("plugin_name") for h in ui_default["inline_handlers"]) == sorted(
        h.get("plugin_name") for h in ui_all["inline_handlers"]
    ) == sorted(h.get("plugin_name") for h in ui_empty["inline_handlers"])
