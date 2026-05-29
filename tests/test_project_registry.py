from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from app.services.actor_identity import telegram_actor_id
from app.services.project_registry import ProjectOwnershipError, ProjectRegistry, ProjectRegistryError
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ThreadModeConfig, ToolConfig


def _build_config(tmp_path, *, intent: str, admin_chat_id: int = 501) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(
            token="token",
            whitelist_chat_ids=[admin_chat_id],
            admlist_chat_ids=[admin_chat_id],
        ),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
        thread_mode=ThreadModeConfig(enabled=False),
    )


def test_project_registry_restores_projects_from_sqlite_and_checks_owner(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="registry_a", admin_chat_id=111)
    cfg_b = _build_config(tmp_path, intent="registry_b", admin_chat_id=222)

    alpha = tmp_path / "alpha"
    beta_parent = tmp_path / "nested"
    beta = beta_parent / "alpha"
    alpha.mkdir()
    beta_parent.mkdir()
    beta.mkdir()

    registry_a = ProjectRegistry(cfg_a.defaults.state_path)
    first = registry_a.register_project(path=str(alpha), owner_id=111)
    second = registry_a.register_project(path=str(beta), owner_id=111)
    repeated = registry_a.register_project(path=str(alpha), owner_id=111, name="Alpha Project")

    owner_id = telegram_actor_id(111)
    assert first.owner_id == owner_id
    assert repeated.slug == first.slug
    assert repeated.name == "Alpha Project"
    assert second.slug != first.slug

    with pytest.raises(ProjectOwnershipError):
        registry_a.require_owner(path=str(alpha), owner_id=999)

    restored = ProjectRegistry(cfg_a.defaults.state_path)
    restored_records = restored.list_projects(owner_id=111)
    assert [(item.slug, item.path, item.owner_id) for item in restored_records] == [
        (first.slug, first.path, owner_id),
        (second.slug, second.path, owner_id),
    ]

    isolated = ProjectRegistry(cfg_b.defaults.state_path)
    assert isolated.list_projects() == []

    with sqlite3.connect(restored.db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
    assert int(row[0]) == 2


def test_project_registry_rejects_legacy_schema_without_owner_id(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="registry_legacy", admin_chat_id=111)
    db_path = ProjectRegistry(cfg.defaults.state_path).db_path

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS projects")
        conn.execute(
            """
            CREATE TABLE projects (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
            """
        )

    with pytest.raises(ProjectRegistryError, match=r"projects table is missing required column owner_id"):
        ProjectRegistry(cfg.defaults.state_path)


def test_newpath_command_registers_project_with_owner_id(tmp_path) -> None:
    async def _run() -> None:
        chat_id = 501
        cfg = _build_config(tmp_path, intent="newpath", admin_chat_id=chat_id)
        target = tmp_path / "dynamic-newpath"
        target.mkdir()

        app = BotApp(cfg)
        sent: list[dict[str, object]] = []

        async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
            sent.append({"chat_id": int(chat_id), "text": str(text)})

        app._send_message = _send_message
        app.ui_state.pending_new_tool[app.telegram_ui_key(chat_id)] = "dummy"
        app.ui_state.dirs_root[app.telegram_ui_key(chat_id)] = str(tmp_path)
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))
        context = SimpleNamespace(args=[str(target)])
        try:
            await app.handlers.cmd_newpath(update, context)

            record = app.project_registry.require_owner(path=str(target), owner_id=chat_id)
            assert record.owner_id == telegram_actor_id(chat_id)
            assert record.path == str(target.resolve())
            assert sent[-1]["text"] == "Сессия s1 создана и выбрана."

            restarted = ProjectRegistry(cfg.defaults.state_path)
            restored = restarted.require_owner(path=str(target), owner_id=chat_id)
            assert restored.slug == record.slug
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_new_command_registers_project_with_owner_id(tmp_path) -> None:
    async def _run() -> None:
        chat_id = 551
        cfg = _build_config(tmp_path, intent="new", admin_chat_id=chat_id)
        target = tmp_path / "dynamic-new"
        target.mkdir()

        app = BotApp(cfg)
        sent: list[dict[str, object]] = []

        async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
            sent.append({"chat_id": int(chat_id), "text": str(text)})

        app._send_message = _send_message
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))
        context = SimpleNamespace(args=["dummy", str(target)])
        try:
            await app.handlers.cmd_new(update, context)

            record = app.project_registry.require_owner(path=str(target), owner_id=chat_id)
            assert record.owner_id == telegram_actor_id(chat_id)
            assert record.path == str(target.resolve())
            assert sent[-1]["text"] == "Сессия s1 создана и выбрана."

            restarted = ProjectRegistry(cfg.defaults.state_path)
            restored = restarted.require_owner(path=str(target), owner_id=chat_id)
            assert restored.slug == record.slug
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_cwd_command_registers_project_with_owner_id(tmp_path) -> None:
    async def _run() -> None:
        chat_id = 601
        cfg = _build_config(tmp_path, intent="cwd", admin_chat_id=chat_id)
        base = tmp_path / "base-project"
        target = tmp_path / "cwd-project"
        base.mkdir()
        target.mkdir()

        app = BotApp(cfg)
        sent: list[dict[str, object]] = []

        async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
            sent.append({"chat_id": int(chat_id), "text": str(text)})

        app._send_message = _send_message
        try:
            app.manager.create(chat_id, "dummy", str(base))
            update = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))
            context = SimpleNamespace(args=[str(target)])

            await app.handlers.cmd_cwd(update, context)

            record = app.project_registry.require_owner(path=str(target), owner_id=chat_id)
            assert record.owner_id == telegram_actor_id(chat_id)
            assert record.path == str(target.resolve())
            assert sent[-1]["text"] == "Новая сессия s2 создана и выбрана."

            restarted = ProjectRegistry(cfg.defaults.state_path)
            restored = restarted.require_owner(path=str(target), owner_id=chat_id)
            assert restored.slug == record.slug
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())
