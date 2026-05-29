"""Verification: SSHService is accessible via ModeDependencies in all Agent modes."""

import os

from app.bootstrap import build_application
from app.services.ssh_service import SSHService
from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)


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


def test_mode_dependencies_has_initialized_ssh_service(tmp_path):
    cfg = _build_config(tmp_path)
    container = build_application(cfg)
    deps = container.mode_dependencies
    assert deps.ssh is not None
    assert isinstance(deps.ssh, SSHService)


def test_ssh_service_is_same_instance_in_deps_and_container(tmp_path):
    cfg = _build_config(tmp_path)
    container = build_application(cfg)
    assert container.ssh_service is container.mode_dependencies.ssh


def test_ssh_service_has_required_methods(tmp_path):
    cfg = _build_config(tmp_path)
    container = build_application(cfg)
    svc = container.mode_dependencies.ssh
    for method in ("exec", "stream", "cancel", "close_all",
                   "test_connection", "generate_key"):
        assert callable(getattr(svc, method, None)), f"missing method: {method}"
