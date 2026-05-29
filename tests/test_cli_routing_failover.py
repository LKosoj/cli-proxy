import logging
import types
import asyncio

from agent.cli_routing import run_prompt_routed_meta
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.sdk.runtime.cli_contracts import CLIResponseFormat
from session import SessionManager
from agent.plugins.use_cli import UseCliTool


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _cfg(tmp_path):
    # Use a shared executable ("bash") so availability checks pass on any CI environment.
    tools = {
        "gemini": ToolConfig(name="gemini", mode="headless", cmd=["bash", "-lc", "cat"]),
        "claude": ToolConfig(name="claude", mode="headless", cmd=["bash", "-lc", "cat"]),
        "qwen": ToolConfig(name="qwen", mode="headless", cmd=["bash", "-lc", "cat"]),
        "codex": ToolConfig(name="codex", mode="headless", cmd=["bash", "-lc", "cat"]),
    }
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_cli="claude",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_run_prompt_routed_meta_fails_over_and_restores_active_cli(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "claude", str(tmp_path))

        calls = []

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            calls.append(self.tool.name)
            if self.tool.name == "gemini":
                raise RuntimeError("boom")
            return f"ok:{self.tool.name}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)

        before_cli = s.active_cli
        before_tool = s.tool.name

        cli_used, out = await run_prompt_routed_meta(s, cfg, "analytics", "x", timeout_sec=5)
        assert cli_used == "claude"
        assert out == "ok:claude"
        assert calls[:2] == ["gemini", "claude"]

        # Must not persistently change user's active CLI.
        assert s.active_cli == before_cli
        assert s.tool.name == before_tool

    asyncio.run(_run())


def test_use_cli_routes_to_analytics_when_cli_work_type_set(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "codex", str(tmp_path))
        s.cli_work_type = "analytics"

        calls = []

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            calls.append(self.tool.name)
            if self.tool.name == "gemini":
                raise RuntimeError("gemini down")
            return f"ok:{self.tool.name}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)

        tool = UseCliTool()
        resp = await tool.execute({"task_text": "do"}, {"session": s})
        assert resp["success"] is True
        assert resp["output"].startswith("ok:")
        # Analytics routing tries gemini first, then claude.
        assert calls[:2] == ["gemini", "claude"]
        # User-selected active CLI remains unchanged.
        assert s.active_cli == "codex"
        assert s.tool.name == "codex"

    asyncio.run(_run())


def test_run_prompt_routed_meta_logs_cli_dialog(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "codex", str(tmp_path))

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            return f"ok:{self.tool.name}:{prompt}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)

        logger = logging.getLogger("bot.cli_dialog")
        handler = _ListHandler()
        old_handlers = list(logger.handlers)
        old_propagate = logger.propagate
        old_level = logger.level
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            cli_used, out = await run_prompt_routed_meta(s, cfg, "default", "hello", chat_id=101)
        finally:
            logger.handlers = old_handlers
            logger.propagate = old_propagate
            logger.setLevel(old_level)

        assert out == f"ok:{cli_used}:hello"
        assert len(handler.messages) == 1
        parts = handler.messages[0].splitlines()
        assert len(parts) == 2
        assert "][user:101][hello]" in parts[0]
        assert f"][{cli_used}][ok:{cli_used}:hello]" in parts[1]

    asyncio.run(_run())


def test_use_cli_logs_dialog_in_agent_mode(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "codex", str(tmp_path))
        s.active_mode = "agent"

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            return f"ok:{self.tool.name}:{prompt}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)

        logger = logging.getLogger("bot.cli_dialog")
        handler = _ListHandler()
        old_handlers = list(logger.handlers)
        old_propagate = logger.propagate
        old_level = logger.level
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            tool = UseCliTool()
            resp = await tool.execute({"task_text": "do"}, {"session": s, "chat_id": 55})
        finally:
            logger.handlers = old_handlers
            logger.propagate = old_propagate
            logger.setLevel(old_level)

        assert resp["success"] is True
        assert resp["output"] == "ok:codex:do"
        assert len(handler.messages) == 1
        parts = handler.messages[0].splitlines()
        assert len(parts) == 2
        assert "][user:55][do]" in parts[0]
        assert "][codex][ok:codex:do]" in parts[1]

    asyncio.run(_run())


def test_use_cli_fresh_run_forces_force_fresh_true(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "codex", str(tmp_path))
        s.active_mode = "agent"
        captured = {"force_fresh": None}

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            captured["force_fresh"] = kwargs.get("force_fresh")
            return f"ok:{self.tool.name}:{prompt}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)
        tool = UseCliTool()
        resp = await tool.execute({"task_text": "do", "fresh_run": True}, {"session": s, "chat_id": 77})
        assert resp["success"] is True
        assert captured["force_fresh"] is True

    asyncio.run(_run())


def test_use_cli_tool_spec_timeout_is_3600s():
    tool = UseCliTool()
    spec = tool.get_spec()
    assert int(spec.timeout_ms) == 3_600_000


def test_run_prompt_routed_meta_parses_structured_work_type_when_flag_enabled(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "claude", str(tmp_path))

        calls = []

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            calls.append(self.tool.name)
            if self.tool.name == "gemini":
                raise RuntimeError("boom")
            return f"ok:{self.tool.name}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)
        structured = '{"work_type":"analytics","confidence":0.92,"reason":"dev task"}'
        cli_used, out = await run_prompt_routed_meta(s, cfg, structured, "x", timeout_sec=5)

        assert cli_used == "claude"
        assert out == "ok:claude"
        assert calls[:2] == ["gemini", "claude"]

    asyncio.run(_run())


def test_run_prompt_routed_meta_logs_and_fallbacks_on_malformed_structured_work_type(tmp_path, monkeypatch):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "claude", str(tmp_path))
        logged: list[str] = []

        def _fake_exception(msg, *args, **kwargs):  # noqa: ANN001, ARG001
            logged.append(str(msg))

        monkeypatch.setattr("agent.cli_routing._log.exception", _fake_exception)

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            return f"ok:{self.tool.name}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)
        malformed = "```json\n{\"work_type\":\n```"
        cli_used, out = await run_prompt_routed_meta(s, cfg, malformed, "x", timeout_sec=5)

        assert cli_used == "claude"
        assert out == "ok:claude"
        assert any("cli_routing work_type structured parse failed" in msg for msg in logged)

    asyncio.run(_run())


def test_run_prompt_routed_meta_wraps_prompt_when_json_response_format_requested(tmp_path):
    async def _run():
        cfg = _cfg(tmp_path)
        mgr = SessionManager(cfg)
        s = mgr.create(1, "claude", str(tmp_path))
        captured = {"prompt": None}

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            del args, kwargs
            captured["prompt"] = str(prompt)
            return "{}"

        s.run_prompt = types.MethodType(_fake_run_prompt, s)

        cli_used, out = await run_prompt_routed_meta(
            s,
            cfg,
            "default",
            "Верни JSON",
            response_format=CLIResponseFormat.JSON_OBJECT,
        )

        assert cli_used == "claude"
        assert out == "{}"
        assert f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.JSON_OBJECT}" in str(captured["prompt"] or "")

    asyncio.run(_run())
