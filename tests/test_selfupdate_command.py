from __future__ import annotations

import json
import os
import asyncio
from types import SimpleNamespace

import pytest
from telegram import BotCommandScopeChat, BotCommandScopeDefault

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from tg.handlers import BotHandlers


def _build_app(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1, 2, 3], admlist_chat_ids=[1, 2]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


@pytest.mark.asyncio
async def test_set_bot_commands_scopes_show_selfupdate_only_for_admins(tmp_path) -> None:
    app = _build_app(tmp_path)

    class _FakeBot:
        def __init__(self):
            self.calls = []

        async def set_my_commands(self, commands, scope=None):
            self.calls.append((list(commands or []), scope))

    fake_bot = _FakeBot()
    fake_app = SimpleNamespace(bot=fake_bot)

    await app.set_bot_commands(fake_app)

    default_calls = [c for c in fake_bot.calls if isinstance(c[1], BotCommandScopeDefault)]
    admin_calls = [c for c in fake_bot.calls if isinstance(c[1], BotCommandScopeChat)]

    assert len(default_calls) == 1
    assert len(admin_calls) == 2

    default_names = [cmd.command for cmd in default_calls[0][0]]
    assert "selfupdate" not in default_names

    for commands, scope in admin_calls:
        assert int(getattr(scope, "chat_id")) in {1, 2}
        names = [cmd.command for cmd in commands]
        assert "selfupdate" in names


class _AllowAdminPolicy:
    async def ensure_allowed(self, _chat_id, _context) -> bool:
        return True

    async def require_admin(self, _chat_id, _context, *, scope: str = "generic") -> bool:
        _ = scope
        return True


def _build_handlers_bot_app():
    sent = []
    sent_kwargs = []

    async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
        sent.append((int(chat_id), str(text or "")))
        sent_kwargs.append(dict(_kwargs or {}))
        return True

    bot_app = SimpleNamespace(
        access_policy_service=_AllowAdminPolicy(),
        _send_message=_send_message,
    )
    bot_app._sent_message_kwargs = sent_kwargs
    return bot_app, sent


def _attach_state_path(bot_app, tmp_path) -> str:
    state_path = str(tmp_path / "state.json")
    bot_app.config = SimpleNamespace(
        defaults=SimpleNamespace(state_path=state_path),
        telegram=SimpleNamespace(token="123:test-token"),
    )
    return f"{state_path}.selfupdate_pending.json"


@pytest.mark.asyncio
async def test_cmd_selfupdate_reports_pull_error_and_skips_restart(monkeypatch, tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)

    calls = []

    async def _fake_run_subprocess(*argv: str, cwd=None):
        calls.append((argv, cwd))
        return 1, "fatal: local changes would be overwritten"

    monkeypatch.setattr(handlers, "_project_root", lambda: str(tmp_path))
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(handlers, "_run_subprocess", _fake_run_subprocess)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1001))
    context = SimpleNamespace(args=[])

    await handlers.cmd_selfupdate(update, context)

    assert len(calls) == 1
    assert calls[0][0] == ("git", "pull", "--ff-only")
    assert any("Ошибка git pull." in text for _chat_id, text in sent)


@pytest.mark.asyncio
async def test_cmd_selfupdate_success_runs_restart(monkeypatch, tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)
    marker_path = _attach_state_path(bot_app, tmp_path)

    calls = []

    async def _fake_run_subprocess(*argv: str, cwd=None):
        calls.append((argv, cwd))
        if argv[:3] == ("git", "pull", "--ff-only"):
            return 0, "Already up to date."
        return 0, ""

    spawned = []

    def _fake_spawn_selfupdate_watchdog(*, marker_path: str, timeout_sec: int):
        spawned.append((marker_path, timeout_sec))

    monkeypatch.setattr(handlers, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(handlers, "_bot_service_name", lambda: "cli-proxy-bot")
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(handlers, "_run_subprocess", _fake_run_subprocess)
    monkeypatch.setattr(handlers, "_spawn_selfupdate_watchdog", _fake_spawn_selfupdate_watchdog)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1002))
    context = SimpleNamespace(args=[])

    await handlers.cmd_selfupdate(update, context)

    assert len(calls) == 2
    assert calls[0][0] == ("git", "pull", "--ff-only")
    assert calls[1][0] == ("systemctl", "restart", "--no-block", "cli-proxy-bot")
    assert spawned == [(marker_path, 30)]
    assert any(text == "git pull выполнен успешно. Перезапуск сервиса запущен." for _chat_id, text in sent)
    assert not any("Already up to date." in text for _chat_id, text in sent)
    marker = json.loads(open(marker_path, "r", encoding="utf-8").read())
    assert int(marker["chat_id"]) == 1002
    assert marker["service_name"] == "cli-proxy-bot"


@pytest.mark.asyncio
async def test_cmd_selfupdate_installs_requirements_before_restart(monkeypatch, tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)
    marker_path = _attach_state_path(bot_app, tmp_path)

    calls = []

    async def _fake_run_subprocess(*argv: str, cwd=None):
        calls.append((argv, cwd))
        if argv[:3] == ("git", "pull", "--ff-only"):
            return 0, "Updating a..b\nFast-forward\n requirements.txt | 2 +-\n"
        if len(argv) >= 6 and argv[1:5] == ("-m", "pip", "install", "-r"):
            return 0, "Successfully installed"
        return 0, ""

    monkeypatch.setattr(handlers, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(handlers, "_bot_service_name", lambda: "cli-proxy-bot")
    monkeypatch.setattr(handlers, "_spawn_selfupdate_watchdog", lambda **_kwargs: None)
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(handlers, "_run_subprocess", _fake_run_subprocess)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1006))
    context = SimpleNamespace(args=[])

    await handlers.cmd_selfupdate(update, context)

    assert len(calls) == 3
    assert calls[0][0] == ("git", "pull", "--ff-only")
    assert calls[1][0] == (str(venv_python), "-m", "pip", "install", "-r", "requirements.txt")
    assert calls[2][0] == ("systemctl", "restart", "--no-block", "cli-proxy-bot")
    assert any("Обновляю зависимости в .venv" in text for _chat_id, text in sent)
    assert any(text == "git pull выполнен успешно. Перезапуск сервиса запущен." for _chat_id, text in sent)
    assert os.path.exists(marker_path)


@pytest.mark.asyncio
async def test_cmd_selfupdate_stops_when_requirements_install_fails(monkeypatch, tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)
    marker_path = _attach_state_path(bot_app, tmp_path)

    calls = []

    async def _fake_run_subprocess(*argv: str, cwd=None):
        calls.append((argv, cwd))
        if argv[:3] == ("git", "pull", "--ff-only"):
            return 0, "Updating a..b\nFast-forward\n requirements.txt | 2 +-\n"
        if len(argv) >= 6 and argv[1:5] == ("-m", "pip", "install", "-r"):
            return 1, "ERROR: failed to build wheel"
        return 0, ""

    monkeypatch.setattr(handlers, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(handlers, "_bot_service_name", lambda: "cli-proxy-bot")
    monkeypatch.setattr(handlers, "_spawn_selfupdate_watchdog", lambda **_kwargs: None)
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(handlers, "_run_subprocess", _fake_run_subprocess)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1007))
    context = SimpleNamespace(args=[])

    await handlers.cmd_selfupdate(update, context)

    assert len(calls) == 2
    assert calls[0][0] == ("git", "pull", "--ff-only")
    assert calls[1][0] == (str(venv_python), "-m", "pip", "install", "-r", "requirements.txt")
    assert not any(call[0][:3] == ("systemctl", "restart", "--no-block") for call in calls)
    assert any("обновление зависимостей завершилось ошибкой" in text.lower() for _chat_id, text in sent)
    assert not os.path.exists(marker_path)


@pytest.mark.asyncio
async def test_cmd_selfupdate_treats_nonzero_restart_as_ok_when_service_is_active(monkeypatch, tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)
    _attach_state_path(bot_app, tmp_path)

    calls = []

    async def _fake_run_subprocess(*argv: str, cwd=None):
        calls.append((argv, cwd))
        if argv[:3] == ("git", "pull", "--ff-only"):
            return 0, "Already up to date."
        if argv[:3] == ("systemctl", "restart", "--no-block"):
            return 1, "Job queued."
        if argv[:2] == ("systemctl", "is-active"):
            return 0, "active\n"
        return 0, ""

    monkeypatch.setattr(handlers, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(handlers, "_bot_service_name", lambda: "bot.service")
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(handlers, "_run_subprocess", _fake_run_subprocess)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1003))
    context = SimpleNamespace(args=[])

    await handlers.cmd_selfupdate(update, context)

    assert len(calls) == 3
    assert calls[0][0] == ("git", "pull", "--ff-only")
    assert calls[1][0] == ("systemctl", "restart", "--no-block", "bot.service")
    assert calls[2][0] == ("systemctl", "is-active", "bot.service")
    assert any(text == "git pull выполнен успешно. Перезапуск сервиса запущен." for _chat_id, text in sent)


@pytest.mark.asyncio
async def test_cmd_selfupdate_reports_unconfirmed_restart_state_without_false_failure(monkeypatch, tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)
    _attach_state_path(bot_app, tmp_path)

    calls = []

    async def _fake_run_subprocess(*argv: str, cwd=None):
        calls.append((argv, cwd))
        if argv[:3] == ("git", "pull", "--ff-only"):
            return 0, "Already up to date."
        if argv[:3] == ("systemctl", "restart", "--no-block"):
            return 1, "Job queued."
        if argv[:2] == ("systemctl", "is-active"):
            return 3, "inactive\n"
        return 0, ""

    monkeypatch.setattr(handlers, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(handlers, "_bot_service_name", lambda: "bot.service")
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(handlers, "_run_subprocess", _fake_run_subprocess)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1004))
    context = SimpleNamespace(args=[])

    await handlers.cmd_selfupdate(update, context)

    assert len(calls) == 3
    assert calls[0][0] == ("git", "pull", "--ff-only")
    assert calls[1][0] == ("systemctl", "restart", "--no-block", "bot.service")
    assert calls[2][0] == ("systemctl", "is-active", "bot.service")
    assert any("Команда перезапуска сервиса `bot.service` отправлена" in text for _chat_id, text in sent)
    assert not any("но не удалось перезапустить сервис" in text for _chat_id, text in sent)


@pytest.mark.asyncio
async def test_notify_pending_selfupdate_sends_confirmation_and_clears_marker(tmp_path) -> None:
    bot_app, sent = _build_handlers_bot_app()
    handlers = BotHandlers(bot_app)
    marker_path = _attach_state_path(bot_app, tmp_path)
    handlers._save_selfupdate_marker(chat_id=1005, service_name="bot.service")

    fake_application = SimpleNamespace(bot=SimpleNamespace())
    await handlers.notify_pending_selfupdate(fake_application)

    assert sent
    assert sent[0][0] == 1005
    assert "Selfupdate подтверждён" in sent[0][1]
    assert not os.path.exists(marker_path)


def test_notify_pending_selfupdate_restores_thread_context_from_marker(tmp_path) -> None:
    async def _run() -> None:
        bot_app, sent = _build_handlers_bot_app()
        handlers = BotHandlers(bot_app)
        _attach_state_path(bot_app, tmp_path)
        handlers._save_selfupdate_marker(chat_id=1006, service_name="bot.service", message_thread_id=77)

        fake_application = SimpleNamespace(bot=SimpleNamespace())
        await handlers.notify_pending_selfupdate(fake_application)

        assert sent
        assert sent[0][0] == 1006
        assert "Selfupdate подтверждён" in str(sent[0][1])
        assert bot_app._sent_message_kwargs[0]["message_thread_id"] == 77

    asyncio.run(_run())
