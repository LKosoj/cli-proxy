import asyncio

import pytest

from app.services.session_service import SessionService
from app.services.session_thread_repository import SessionThreadRepository
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager, session_runtime_uid


def _build_config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


@pytest.mark.asyncio
async def test_close_session_cancels_session_tasks(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    manager = SessionManager(cfg)
    tasks = TaskService()
    sessions = SessionService(manager, tasks)
    s = sessions.create_session(1, "dummy", str(tmp_path))

    started = asyncio.Event()
    finished = {"done": False}

    async def runner(token):
        started.set()
        try:
            while not token.is_cancelled:
                await asyncio.sleep(0.05)
        finally:
            finished["done"] = True

    runtime_uid = session_runtime_uid(s)
    sessions.start_background_task(runtime_uid, "bg", runner)
    await asyncio.wait_for(started.wait(), timeout=0.5)
    assert tasks.list_active(session_uid=runtime_uid)

    assert await sessions.close_session(1, s.id, cancel_timeout_s=0.5) is True
    await asyncio.sleep(0.05)
    assert finished["done"] is True
    assert not tasks.list_active(session_uid=runtime_uid)


@pytest.mark.asyncio
async def test_close_session_deletes_orphan_thread_mapping(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    manager = SessionManager(cfg)
    tasks = TaskService()
    sessions = SessionService(manager, tasks)
    repository = SessionThreadRepository(cfg.defaults.state_path)
    s = sessions.create_session(1, "dummy", str(tmp_path))

    repository.upsert_mapping(
        owner_chat_id=1,
        session_id=s.id,
        session_uid="thread:1:123",
        topics_chat_id=1,
        message_thread_id=123,
        topic_name="orphan-topic",
    )
    assert repository.get_by_session(owner_chat_id=1, session_id=s.id) is not None

    assert await sessions.close_session(1, s.id, cancel_timeout_s=0.1) is True
    assert repository.get_by_session(owner_chat_id=1, session_id=s.id) is None
