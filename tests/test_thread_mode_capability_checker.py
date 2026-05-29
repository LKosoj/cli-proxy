from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import bot
from app.services.lifecycle_service import build_post_init
from app.services.thread_mode_capability_checker import ThreadModeCapabilityChecker
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    TelegramConfig,
    ThreadModeConfig,
    ToolConfig,
)


class _FakeBot:
    def __init__(self, me, *, chat_is_forum: bool = True) -> None:
        self._me = me
        self._chat = SimpleNamespace(is_forum=bool(chat_is_forum))
        self.get_me_calls = 0
        self.get_chat_calls: list[int] = []

    async def get_me(self):
        self.get_me_calls += 1
        return self._me

    async def get_chat(self, chat_id: int):
        self.get_chat_calls.append(int(chat_id))
        return self._chat

    async def create_forum_topic(self, **_kwargs):
        return None

    async def edit_forum_topic(self, **_kwargs):
        return None


class _FakeApp:
    def __init__(self, fake_bot: _FakeBot) -> None:
        self.bot = fake_bot
        self.run_polling_calls: list[dict[str, float | int]] = []

    def run_polling(self, **kwargs) -> None:
        self.run_polling_calls.append(dict(kwargs))


class _AsyncRecorder:
    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    async def __call__(self, *_args, **_kwargs) -> None:
        self._log.append(self._name)


class _FakeSessionThreadManager:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def reconcile(self) -> None:
        self._log.append("session_thread_manager.reconcile")

    async def start_repair_job(self, **_kwargs) -> None:
        self._log.append("session_thread_manager.start_repair_job")


def _build_config(tmp_path, *, mode: str = "group", enabled: bool = True) -> AppConfig:
    workdir = tmp_path / "workdir"
    runtime = tmp_path / "runtime"
    logs = tmp_path / "logs"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
        thread_mode=ThreadModeConfig(
            enabled=enabled,
            mode=mode,
            topics_chat_id=-1001234567890 if mode == "group" else None,
        ),
    )


@pytest.mark.asyncio
async def test_thread_mode_capability_checker_allows_group_topics_enabled(tmp_path) -> None:
    checker = ThreadModeCapabilityChecker(_build_config(tmp_path, mode="group", enabled=True))
    fake_bot = _FakeBot(SimpleNamespace(has_topics_enabled=True, username="topics_bot", id=1))

    await checker.ensure_supported(fake_bot)

    assert fake_bot.get_me_calls == 1
    assert fake_bot.get_chat_calls == [-1001234567890]


@pytest.mark.asyncio
async def test_thread_mode_capability_checker_exits_with_critical_log_when_topics_disabled(tmp_path, caplog) -> None:
    checker = ThreadModeCapabilityChecker(_build_config(tmp_path, mode="group", enabled=True))
    fake_bot = _FakeBot(SimpleNamespace(has_topics_enabled=False, username="topics_bot", id=1))

    caplog.set_level(logging.CRITICAL, logger="app.services.thread_mode_capability_checker")

    with pytest.raises(SystemExit) as exc_info:
        await checker.ensure_supported(fake_bot)

    assert exc_info.value.code == 1
    assert "thread mode capability check failed" in caplog.text
    assert "has_topics_enabled must be True" in caplog.text
    assert "BotFather" in caplog.text
    assert "Thread Mode" in caplog.text
    assert "topics_chat_id=-1001234567890" in caplog.text


@pytest.mark.asyncio
async def test_thread_mode_capability_checker_private_mode_validates_get_me_and_does_not_leak_state(tmp_path) -> None:
    checker = ThreadModeCapabilityChecker(_build_config(tmp_path, mode="group", enabled=True))

    with pytest.raises(SystemExit):
        await checker.ensure_supported(_FakeBot(SimpleNamespace(has_topics_enabled=False, username="broken", id=1)))

    private_bot = _FakeBot(SimpleNamespace(has_topics_enabled=True, username="private_bot", id=2))
    checker_private = ThreadModeCapabilityChecker(_build_config(tmp_path, mode="private", enabled=True))

    await checker_private.ensure_supported(private_bot)

    assert private_bot.get_me_calls == 1
    assert private_bot.get_chat_calls == []


@pytest.mark.asyncio
async def test_thread_mode_capability_checker_group_mode_rejects_non_forum_chat(tmp_path, caplog) -> None:
    checker = ThreadModeCapabilityChecker(_build_config(tmp_path, mode="group", enabled=True))
    fake_bot = _FakeBot(SimpleNamespace(has_topics_enabled=True, username="topics_bot", id=1), chat_is_forum=False)

    caplog.set_level(logging.CRITICAL, logger="app.services.thread_mode_capability_checker")

    with pytest.raises(SystemExit) as exc_info:
        await checker.ensure_supported(fake_bot)

    assert exc_info.value.code == 1
    assert fake_bot.get_me_calls == 1
    assert fake_bot.get_chat_calls == [-1001234567890]
    assert "Chat.is_forum must be True" in caplog.text


def test_post_init_exits_before_startup_services_when_topics_disabled(tmp_path, monkeypatch, caplog) -> None:
    config = _build_config(tmp_path, mode="group", enabled=True)
    fake_bot = _FakeBot(SimpleNamespace(has_topics_enabled=False, username="topics_bot", id=1))
    call_log: list[str] = []
    fake_bot_app = SimpleNamespace(
        config=config,
        set_bot_commands=_AsyncRecorder(call_log, "set_bot_commands"),
        mcp=SimpleNamespace(start=_AsyncRecorder(call_log, "mcp.start")),
        session_thread_manager=_FakeSessionThreadManager(call_log),
        notification_queue_service=SimpleNamespace(start=_AsyncRecorder(call_log, "notification_queue_service.start")),
        scheduler_service=SimpleNamespace(start=_AsyncRecorder(call_log, "scheduler_service.start")),
        mode_launch_adapter=SimpleNamespace(start=_AsyncRecorder(call_log, "mode_launch_adapter.start")),
        webhook_ingress_service=SimpleNamespace(start=_AsyncRecorder(call_log, "webhook_ingress_service.start")),
        miniapp_server=SimpleNamespace(start=_AsyncRecorder(call_log, "miniapp_server.start")),
        shared_http_ingress=SimpleNamespace(start=_AsyncRecorder(call_log, "shared_http_ingress.start")),
        handlers=SimpleNamespace(notify_pending_selfupdate=_AsyncRecorder(call_log, "notify_pending_selfupdate")),
        _task_deadline_checker_task=object(),
        is_allowed=lambda *_args, **_kwargs: True,
    )

    async def _deadline_checker(*_args, **_kwargs) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr("app.services.lifecycle_service.run_task_deadline_checker", _deadline_checker)
    caplog.set_level(logging.CRITICAL, logger="app.services.thread_mode_capability_checker")

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(build_post_init(fake_bot_app)(SimpleNamespace(bot=fake_bot)))

    assert exc_info.value.code == 1
    assert fake_bot.get_me_calls == 1
    assert call_log == []
    assert "Thread Mode" in caplog.text


def test_bot_main_runs_polling_without_touching_bot_before_event_loop(tmp_path, monkeypatch) -> None:
    fake_app = _FakeApp(_FakeBot(SimpleNamespace(has_topics_enabled=False, username="topics_bot", id=1)))
    config = _build_config(tmp_path, mode="group", enabled=True)
    config.telegram.poll_interval_sec = 0.5
    config.telegram.polling_timeout_sec = 9

    monkeypatch.setattr(bot, "load_validated_settings", lambda _path: object())
    monkeypatch.setattr(bot, "load_config", lambda _path: config)
    monkeypatch.setattr(bot, "build_app", lambda _config: fake_app)
    monkeypatch.setattr(bot, "load_dotenv_near", lambda *args, **kwargs: {})

    bot.main()

    assert fake_app.bot.get_me_calls == 0
    assert fake_app.bot.get_chat_calls == []
    assert fake_app.run_polling_calls == [{"poll_interval": 0.5, "timeout": 9}]
