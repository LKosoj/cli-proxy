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


# --- content screening integration --------------------------------------------------------


def _load_cfg(*, screening_enabled=False, screening_mode="warn"):
    from config import load_config

    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.security.content_screening.enabled = screening_enabled
    cfg.security.content_screening.mode = screening_mode
    return cfg


def _make_probe_plugin(name: str, output: str, *, returns_external_content: bool, success: bool = True):
    from agent.plugins.base import ToolPlugin
    from modes.sdk.runtime.tooling.spec import ToolSpec

    class _ProbeExternalTool(ToolPlugin):
        def get_spec(self) -> ToolSpec:
            return ToolSpec(
                name=name,
                description="probe",
                parameters={"type": "object", "properties": {}},
                returns_external_content=returns_external_content,
            )

        async def execute(self, args, ctx):
            return {"success": success, "output": output, "error": None if success else "boom"}

    return _ProbeExternalTool()


def test_registry_skips_screening_when_disabled(monkeypatch):
    import app.services.content_screening_service as css_mod

    async def _fail(*_args, **_kwargs):
        raise AssertionError("classifier must not be called when content screening is disabled")

    monkeypatch.setattr(css_mod, "chat_completion", _fail)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=False)
    reg = ToolRegistry(cfg)
    assert reg._content_screening is None
    reg.register(_make_probe_plugin("probe_external_disabled", "some external content", returns_external_content=True))

    out = asyncio.run(reg.execute("probe_external_disabled", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert out["output"] == "some external content"


def test_registry_skips_screening_when_spec_does_not_return_external_content(monkeypatch):
    import app.services.content_screening_service as css_mod

    async def _fail(*_args, **_kwargs):
        raise AssertionError("classifier must not be called for tools not flagged as external content")

    monkeypatch.setattr(css_mod, "chat_completion", _fail)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True)
    reg = ToolRegistry(cfg)
    assert reg._content_screening is not None
    reg.register(_make_probe_plugin("probe_internal_tool", "some content", returns_external_content=False))

    out = asyncio.run(reg.execute("probe_internal_tool", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert out["output"] == "some content"


def test_registry_applies_auto_verdict_unchanged(monkeypatch):
    import json

    import app.services.content_screening_service as css_mod

    async def _fake(*_args, **_kwargs):
        return json.dumps({"decision": "auto"})

    monkeypatch.setattr(css_mod, "chat_completion", _fake)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True)
    reg = ToolRegistry(cfg)
    reg.register(_make_probe_plugin("probe_auto", "clean page text", returns_external_content=True))

    out = asyncio.run(reg.execute("probe_auto", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert out["output"] == "clean page text"


def test_registry_applies_strict_warn_verdict(monkeypatch):
    import json

    import app.services.content_screening_service as css_mod

    async def _fake(*_args, **_kwargs):
        return json.dumps({"decision": "strict", "reason": "prompt injection"})

    monkeypatch.setattr(css_mod, "chat_completion", _fake)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True, screening_mode="warn")
    reg = ToolRegistry(cfg)
    reg.register(_make_probe_plugin("probe_strict_warn", "malicious page text", returns_external_content=True))

    out = asyncio.run(reg.execute("probe_strict_warn", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert "malicious page text" in out["output"]
    assert "prompt injection" in out["output"]


def test_registry_applies_strict_block_verdict(monkeypatch):
    import json

    import app.services.content_screening_service as css_mod

    async def _fake(*_args, **_kwargs):
        return json.dumps({"decision": "strict", "reason": "prompt injection"})

    monkeypatch.setattr(css_mod, "chat_completion", _fake)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True, screening_mode="block")
    reg = ToolRegistry(cfg)
    reg.register(_make_probe_plugin("probe_strict_block", "malicious page text - must not leak", returns_external_content=True))

    out = asyncio.run(reg.execute("probe_strict_block", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert "malicious page text - must not leak" not in out["output"]


def test_registry_skips_screening_when_tool_reports_failure(monkeypatch):
    import app.services.content_screening_service as css_mod

    async def _fail(*_args, **_kwargs):
        raise AssertionError("classifier must not be called for a failed tool result")

    monkeypatch.setattr(css_mod, "chat_completion", _fail)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True)
    reg = ToolRegistry(cfg)
    reg.register(_make_probe_plugin("probe_failed", "irrelevant", returns_external_content=True, success=False))

    out = asyncio.run(reg.execute("probe_failed", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is False


def test_registry_skips_screening_when_output_not_string(monkeypatch):
    import app.services.content_screening_service as css_mod
    from agent.plugins.base import ToolPlugin
    from modes.sdk.runtime.tooling.spec import ToolSpec

    async def _fail(*_args, **_kwargs):
        raise AssertionError("classifier must not be called for non-string output")

    monkeypatch.setattr(css_mod, "chat_completion", _fail)

    class _NonStringOutputTool(ToolPlugin):
        def get_spec(self) -> ToolSpec:
            return ToolSpec(
                name="probe_non_string_output",
                description="probe",
                parameters={"type": "object", "properties": {}},
                returns_external_content=True,
            )

        async def execute(self, args, ctx):
            return {"success": True, "output": {"nested": "dict"}, "error": None}

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True)
    reg = ToolRegistry(cfg)
    reg.register(_NonStringOutputTool())

    out = asyncio.run(reg.execute("probe_non_string_output", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert out["output"] == {"nested": "dict"}


def test_registry_skips_screening_when_output_empty_or_whitespace(monkeypatch):
    import app.services.content_screening_service as css_mod

    async def _fail(*_args, **_kwargs):
        raise AssertionError("classifier must not be called for empty/whitespace output")

    monkeypatch.setattr(css_mod, "chat_completion", _fail)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True)
    reg = ToolRegistry(cfg)
    reg.register(_make_probe_plugin("probe_blank", "   \n  ", returns_external_content=True))

    out = asyncio.run(reg.execute("probe_blank", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert out["output"] == "   \n  "


def test_registry_execute_survives_content_screening_exception():
    from unittest.mock import AsyncMock

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True)
    reg = ToolRegistry(cfg)
    reg._content_screening.screen = AsyncMock(side_effect=RuntimeError("classifier exploded"))
    reg.register(_make_probe_plugin("probe_broken_screen", "some content", returns_external_content=True))

    out = asyncio.run(reg.execute("probe_broken_screen", {}, {"allowed_tools": ["All"]}))
    assert out["success"] is True
    assert out["output"] == "some content"


def test_registry_execute_many_screens_each_external_tool_independently(monkeypatch):
    import json

    import app.services.content_screening_service as css_mod

    async def _fake(_config, _system, user, *_args, **_kwargs):
        payload = json.loads(user)
        if "flagged" in payload["content"]:
            return json.dumps({"decision": "strict", "reason": "flagged content"})
        return json.dumps({"decision": "auto"})

    monkeypatch.setattr(css_mod, "chat_completion", _fake)

    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = _load_cfg(screening_enabled=True, screening_mode="warn")
    reg = ToolRegistry(cfg)
    reg.register(_make_probe_plugin("probe_many_clean", "clean content", returns_external_content=True))
    reg.register(_make_probe_plugin("probe_many_flagged", "flagged content", returns_external_content=True))

    results = asyncio.run(
        reg.execute_many(
            [
                {"name": "probe_many_clean", "args": {}},
                {"name": "probe_many_flagged", "args": {}},
            ],
            {"allowed_tools": ["All"]},
        )
    )
    assert results[0]["output"] == "clean content"
    assert "flagged content" in results[1]["output"]
    assert results[1]["output"] != "flagged content"  # warning prefix was applied
