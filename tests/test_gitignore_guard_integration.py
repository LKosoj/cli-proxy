"""Integration: verify .gitignore contains '.cli-proxy/' after session init."""

import os

from config import (
    AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig,
)
from session import SessionManager, _gitignore_checked


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
    ), workdir


def test_session_create_adds_cli_proxy_to_gitignore(tmp_path):
    _gitignore_checked.clear()
    cfg, workdir = _build_config(tmp_path)
    gitignore = os.path.join(workdir, ".gitignore")

    assert not os.path.exists(gitignore)

    mgr = SessionManager(cfg)
    mgr.create(1, "qwen", workdir)

    assert os.path.isfile(gitignore)
    content = open(gitignore, "r").read()
    assert ".cli-proxy/" in content


def test_session_create_appends_to_existing_gitignore(tmp_path):
    _gitignore_checked.clear()
    cfg, workdir = _build_config(tmp_path)
    gitignore = os.path.join(workdir, ".gitignore")

    with open(gitignore, "w") as f:
        f.write("node_modules/\n")

    mgr = SessionManager(cfg)
    mgr.create(1, "qwen", workdir)

    content = open(gitignore, "r").read()
    assert "node_modules/" in content
    assert ".cli-proxy/" in content


def test_session_create_does_not_duplicate(tmp_path):
    _gitignore_checked.clear()
    cfg, workdir = _build_config(tmp_path)
    gitignore = os.path.join(workdir, ".gitignore")

    with open(gitignore, "w") as f:
        f.write(".cli-proxy/\n")

    mgr = SessionManager(cfg)
    mgr.create(1, "qwen", workdir)

    content = open(gitignore, "r").read()
    assert content.count(".cli-proxy") == 1
