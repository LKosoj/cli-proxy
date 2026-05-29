import asyncio
from unittest.mock import MagicMock

import pytest

from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager, session_runtime_uid


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig):
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):
        current = self.config
        for part in str(key or "").split("."):
            token = part.strip()
            if not token:
                continue
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
                continue
            if not hasattr(current, token):
                return default
            current = getattr(current, token)
        return current


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
async def test_run_session_input_cancellation_interrupts_session(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    task_service = TaskService()
    sessions = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=None,
    )

    session = sessions.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.interrupt = MagicMock()

    started = asyncio.Event()

    async def _slow_run_prompt(prompt: str, *args, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return "NEVER"

    session.run_prompt = _slow_run_prompt  # type: ignore[assignment]

    events: list[str] = []
    facade.subscribe(lambda note: events.append(note.event))

    task = asyncio.create_task(facade.run_session_input(session_uid, "hi"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert task_service.list_active(session_id=session_uid)

    cancelled = await task_service.cancel_session(session_uid, reason="manual", timeout_s=1.0)
    assert cancelled >= 1

    out = await asyncio.wait_for(task, timeout=2.0)
    assert out == ""
    assert "task:cancelled" in events
    assert session.interrupt.called is True
