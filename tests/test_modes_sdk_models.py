import asyncio

import pytest

from app.mode_dependencies import ModeDependencies, build_mode_foundation_services
from config import DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig, AppConfig
from modes.sdk import BaseMode, CallbackModel, MenuItemModel, MenuModel, MessageModel, ToolResult
from modes.sdk import ModePipelineService
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager


def test_menu_model_to_dict_roundtrip():
    menu = MenuModel(
        title="T",
        text="Hello",
        columns=2,
        items=[
            MenuItemModel(label="A", action="act", payload={"x": 1}),
            MenuItemModel(label="B", action="act2"),
        ],
    )
    d = menu.to_dict()
    assert d["title"] == "T"
    assert d["columns"] == 2
    assert d["items"][0]["label"] == "A"
    assert d["items"][0]["payload"]["x"] == 1


def test_message_and_callback_models_validate_required_fields():
    msg = MessageModel(text="hi", chat_id=1, user_id=2, message_id=3)
    assert msg.to_dict()["chat_id"] == 1

    cb = CallbackModel(action="x", chat_id=1, payload={"k": "v"})
    assert cb.to_dict()["payload"]["k"] == "v"

    with pytest.raises(ValueError):
        CallbackModel(action="", chat_id=1)

    with pytest.raises(ValueError):
        MenuModel(title="t", columns=0)


def test_tool_result_ok_fail_conventions():
    okr = ToolResult.ok("out", data={"a": 1})
    assert okr.success is True
    assert okr.error is None
    assert okr.output == "out"
    assert okr.data["a"] == 1

    err = ToolResult.fail("boom", output="partial")
    assert err.success is False
    assert err.error == "boom"
    assert err.output == "partial"

    with pytest.raises(ValueError):
        ToolResult(success=True, error="should-not")

    with pytest.raises(ValueError):
        ToolResult(success=False, error=None)


def test_base_mode_helpers_and_lifecycle_contract():
    class _M(BaseMode):
        async def handle_input(self, message: MessageModel, ctx):
            return ToolResult.ok(message.text)

        async def handle_callback(self, callback: CallbackModel, ctx):
            return ToolResult.ok(callback.action)

    async def _run():
        m = _M()
        m.initialize(config={"x": 1}, services={"svc": 123})
        assert m.get_service("svc") == 123
        assert m.require_service("svc") == 123
        with pytest.raises(KeyError):
            m.require_service("missing")

        # No-op lifecycle defaults.
        assert await m.on_enable({}) is None
        assert await m.on_disable({}) is None

        r1 = await m.handle_input(MessageModel(text="hi", chat_id=1), {})
        assert r1.success is True
        assert r1.output == "hi"

        r2 = await m.handle_callback(CallbackModel(action="a", chat_id=1), {})
        assert r2.success is True
        assert r2.output == "a"

    asyncio.run(_run())


def test_base_mode_exposes_type_safe_foundation_accessors(tmp_path):
    class _M(BaseMode):
        async def handle_input(self, message: MessageModel, ctx):
            return ToolResult.ok(message.text)

        async def handle_callback(self, callback: CallbackModel, ctx):
            return ToolResult.ok(callback.action)

    cfg = AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
            run_artifacts_enabled=False,
            run_artifacts_retention_days=14,
            run_doctor_enabled=False,
            run_boundary_validation_enabled=False,
            run_metrics_enabled=False,
            skill_discovery_mode="auto",
            skill_install_policy="admin_approve",
            skill_registry_paths=[".cli-proxy/skills", ".cli-proxy/project-skills"],
            skill_allowlisted_sources=["local:global-registry", "registry:npx-skills"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )
    foundation = build_mode_foundation_services(cfg)
    deps = ModeDependencies(
        session_manager=SessionManager(cfg),
        registry=ModeRegistryService(),
        pipeline=ModePipelineService(),
        run_artifacts=foundation.run_artifacts,
        run_observability=foundation.run_observability,
        run_doctor=foundation.run_doctor,
        run_boundary_validation=foundation.run_boundary_validation,
        skill_runtime=foundation.skill_runtime,
    )

    mode = _M()
    mode.initialize(config=cfg, services={"mode_dependencies": deps})

    assert mode._run_artifacts().is_enabled() is False
    assert mode._run_artifacts().retention_window_days() == 14
    assert mode._run_observability().is_enabled() is False
    assert mode._run_doctor().is_enabled() is False
    assert callable(getattr(mode._run_doctor(), "diagnose", None))
    assert mode._run_boundary_validation().is_enabled() is False
    assert callable(getattr(mode._run_boundary_validation(), "validate", None))
    assert mode._skill_runtime().allows_auto_discovery() is True
    assert mode._skill_runtime().allows_source("registry:npx-skills") is True
    assert mode._skill_runtime().registry_path_list() == [".cli-proxy/skills", ".cli-proxy/project-skills"]
