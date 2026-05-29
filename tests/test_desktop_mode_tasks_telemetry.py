import asyncio
from collections import defaultdict
from contextlib import suppress

import pytest

from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from modes.registry import ModeRegistry
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager


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
async def test_desktop_mode_task_done_callback_emits_single_correct_terminal_event(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    task_service = TaskService()
    sessions = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=ModeRegistryService(ModeRegistry()),
    )
    facade.config = cfg

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    tasks = facade._desktop_mode_tasks_service()

    async def _ok() -> str:
        return "ok"

    async def _fail() -> None:
        raise RuntimeError("boom")

    block = asyncio.Event()

    async def _wait_cancel() -> None:
        await block.wait()

    ok_task = tasks.create(session_uid="s1", mode_id="tele", coro=_ok(), name="ok")
    fail_task = tasks.create(session_uid="s1", mode_id="tele", coro=_fail(), name="fail")
    cancel_task = tasks.create(session_uid="s1", mode_id="tele", coro=_wait_cancel(), name="cancel")

    assert await ok_task == "ok"
    with pytest.raises(RuntimeError, match="boom"):
        await fail_task

    cancel_task.cancel()
    with suppress(asyncio.CancelledError):
        await cancel_task

    await asyncio.sleep(0)

    terminal_by_name: dict[str, list[str]] = defaultdict(list)
    terminal_events = {"task:completed", "task:failed", "task:cancelled"}
    for event, payload in events:
        if event in terminal_events:
            terminal_by_name[str(payload.get("name") or "")].append(event)

    assert terminal_by_name["mode:tele:ok"] == ["task:completed"]
    assert terminal_by_name["mode:tele:fail"] == ["task:failed"]
    assert terminal_by_name["mode:tele:cancel"] == ["task:cancelled"]

    fail_payloads = [
        payload
        for event, payload in events
        if event == "task:failed" and str(payload.get("name") or "") == "mode:tele:fail"
    ]
    assert len(fail_payloads) == 1
    assert "boom" in str(fail_payloads[0].get("error") or "")
