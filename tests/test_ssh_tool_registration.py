"""Verification: SSH tool plugins are registered in ToolRegistry."""

import os

from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)
from modes.sdk.runtime.tooling.registry import ToolRegistry


def _build_config(tmp_path):
    workdir = str(tmp_path / "project")
    os.makedirs(workdir, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1]),
        tools={"qwen": ToolConfig(name="qwen", mode="headless", cmd=["echo"])},
        defaults=DefaultsConfig(
            workdir=workdir,
            state_path=str(tmp_path / "state.db"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_ssh_exec_registered_in_tool_registry(tmp_path):
    cfg = _build_config(tmp_path)
    registry = ToolRegistry(cfg)
    assert "ssh_exec" in registry.specs, (
        f"ssh_exec not in specs: {sorted(registry.specs.keys())}"
    )


def test_ssh_long_registered_in_tool_registry(tmp_path):
    cfg = _build_config(tmp_path)
    registry = ToolRegistry(cfg)
    assert "ssh_long" in registry.specs


def test_ssh_cancel_registered_in_tool_registry(tmp_path):
    cfg = _build_config(tmp_path)
    registry = ToolRegistry(cfg)
    assert "ssh_cancel" in registry.specs


def test_ssh_tools_have_correct_spec_names(tmp_path):
    cfg = _build_config(tmp_path)
    registry = ToolRegistry(cfg)
    assert registry.specs["ssh_exec"].name == "ssh_exec"
    assert registry.specs["ssh_long"].name == "ssh_long"
    assert registry.specs["ssh_cancel"].name == "ssh_cancel"


def test_ssh_tools_in_definitions(tmp_path):
    cfg = _build_config(tmp_path)
    registry = ToolRegistry(cfg)
    defs = registry.get_definitions()
    names = {d["function"]["name"] for d in defs if "function" in d}
    assert "ssh_exec" in names
    assert "ssh_long" in names
    assert "ssh_cancel" in names
