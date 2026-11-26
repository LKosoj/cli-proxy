from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, _start_command
from app.services.cli_model_service import (
    CliModelCatalog,
    CliModelOption,
    apply_cli_model_args,
    clear_cli_model_cache,
)
from session import (
    CliState,
    get_session_cli_effort,
    get_session_cli_model,
    set_session_cli_model_settings,
)
from tg.callbacks import CallbackHandler
from tg.handlers import BotHandlers


def test_apply_cli_model_args_for_supported_clis() -> None:
    assert apply_cli_model_args(
        "agy",
        ["agy", "--output-format", "stream-json", "--print=hi"],
        model="gemini-4-neon-high",
        effort="high",
    ) == ["agy", "--model", "gemini-4-neon-high", "--effort", "high", "--output-format", "stream-json", "--print=hi"]

    assert apply_cli_model_args(
        "claude",
        ["claude", "--model", "old", "-p", "hi"],
        model="opus",
        effort="max",
    ) == ["claude", "--model", "opus", "--effort", "max", "-p", "hi"]

    assert apply_cli_model_args(
        "grok",
        ["grok", "--reasoning-effort", "low", "-p", "hi"],
        model="grok-4.7",
        effort="high",
    ) == ["grok", "--model", "grok-4.7", "--reasoning-effort", "high", "-p", "hi"]

    assert apply_cli_model_args(
        "codex",
        ["codex", "-c", "model_reasoning_effort=low", "exec", "hi"],
        model="gpt-5.4",
        effort="xhigh",
    ) == ["codex", "-m", "gpt-5.4", "-c", "model_reasoning_effort=xhigh", "exec", "hi"]

    # Неподдерживаемый CLI не изменяется
    assert apply_cli_model_args("gemini", ["gemini", "-p", "hi"], model="x", effort="high") == ["gemini", "-p", "hi"]


def test_session_cli_model_settings_and_tmux_start_command() -> None:
    session = SimpleNamespace(
        id="s1",
        workdir="/tmp",
        tool=SimpleNamespace(
            name="agy",
            interactive_cmd=["agy"],
            interactive_resume_cmd=["agy", "--conversation", "{resume}"],
            env={},
        ),
        cli=CliState(active_cli="agy", resume_tokens={"agy": "conv-123"}),
    )
    changed, model, effort = set_session_cli_model_settings(session, model="gemini-4-neon-medium", effort="high")
    assert changed is True
    assert model == "gemini-4-neon-medium"
    assert effort == "high"
    assert get_session_cli_model(session) == "gemini-4-neon-medium"
    assert get_session_cli_effort(session) == "high"

    cmd = _start_command(session, {})
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gemini-4-neon-medium"
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"
    assert "--conversation" in cmd and cmd[cmd.index("--conversation") + 1] == "conv-123"


@pytest.mark.asyncio
async def test_tmux_restart_for_settings_preserves_resume_token(tmp_path: Path) -> None:
    killed: list[str] = []

    class FakeDriver:
        user = None

        async def has_session(self, name: str) -> bool:
            return name not in killed

        async def kill_session(self, name: str) -> bool:
            killed.append(name)
            return True

    backend = TmuxExecutionBackend(driver=FakeDriver())
    session = SimpleNamespace(
        id="s46",
        chat_id=42,
        workdir=str(tmp_path),
        tool=SimpleNamespace(name="agy", tmux_user=None),
        cli=CliState(active_cli="agy", resume_tokens={}),
        resume_token=None,
    )
    paths = backend.paths(session)
    os.makedirs(paths["runtime_dir"], exist_ok=True)
    backend._write_state(paths, {"state": "idle", "resume_token": "tok-abc"})

    restarted = await backend.restart_for_settings(session)
    assert restarted is True
    assert killed == [paths["session_name"]]
    assert session.cli.resume_tokens["agy"] == "tok-abc"


@pytest.mark.asyncio
async def test_telegram_rich_edit_model_card_and_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_cli_model_cache()
    catalog = CliModelCatalog(
        cli_name="agy",
        supported=True,
        models=tuple(
            CliModelOption(id=f"model-{i}", label=f"Model {i}", efforts=("low", "medium", "high"))
            for i in range(10)
        ),
        efforts=("low", "medium", "high"),
        default_model="model-0",
    )
    monkeypatch.setattr("app.services.cli_model_service.get_cli_model_catalog", lambda _cli, **kw: catalog)
    restart_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(TmuxExecutionBackend, "restart_for_settings", restart_mock)

    saved = {"count": 0}
    session = SimpleNamespace(
        id="s46",
        chat_id=42,
        busy=False,
        workdir="/tmp",
        tool=SimpleNamespace(name="agy", execution_backends=["headless", "tmux"], default_execution_backend="tmux"),
        cli=CliState(active_cli="agy"),
        config=SimpleNamespace(
            tools={"agy": SimpleNamespace(name="agy", execution_backends=["headless", "tmux"], default_execution_backend="tmux")},
            defaults=SimpleNamespace(default_execution_backend="tmux"),
        ),
    )
    bot_app = SimpleNamespace(
        config=session.config,
        manager=SimpleNamespace(
            get=lambda _cid, tok: session if tok == "s46" else None,
            get_by_uid=lambda _tok: None,
            save=lambda: saved.__setitem__("count", saved["count"] + 1),
        ),
    )
    handlers = BotHandlers(bot_app)
    bot_app.handlers = handlers

    text, kb = handlers.build_session_model_card(session, page=0, lang="ru")
    assert "agy" in text
    all_callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
    assert all(len(cb.encode("utf-8")) <= 64 for cb in all_callbacks)
    assert any(cb == "sess_model:p:s46:1" for cb in all_callbacks)

    edits: list[tuple[str, object]] = []
    router = CallbackHandler(bot_app)

    async def fake_edit(_ctx, _query, edit_text, *, reply_markup=None, md2: bool = True) -> bool:
        edits.append((edit_text, reply_markup))
        return True

    router._edit_msg = fake_edit  # type: ignore[method-assign]
    router._callback_scope = lambda cid, _q: (cid, cid, session)  # type: ignore[method-assign]
    router._ensure_session_visible = AsyncMock(return_value=True)  # type: ignore[method-assign]
    query = SimpleNamespace(from_user=SimpleNamespace(id=42, language_code="ru"))

    handled = await router._cb_sess_model(data="sess_model:m:s46:2:0", chat_id=42, query=query, context=SimpleNamespace())
    assert handled is True
    assert get_session_cli_model(session) == "model-2"
    assert saved["count"] == 1
    restart_mock.assert_awaited_once_with(session)
    assert edits and "Model 2" in edits[-1][0]

    handled_eff = await router._cb_sess_model(
        data="sess_model:e:s46:high:0", chat_id=42, query=query, context=SimpleNamespace(),
    )
    assert handled_eff is True
    assert get_session_cli_effort(session) == "high"
    assert "high" in edits[-1][0]


def test_miniapp_app_js_syntax_and_model_hooks() -> None:
    app_js = Path("miniapp/static/app.js").read_text(encoding="utf-8")
    index_html = Path("miniapp/static/index.html").read_text(encoding="utf-8")
    assert "settingsCliModelGroup" in index_html
    assert "settingsCliModel" in index_html
    assert "settingsCliEffort" in index_html
    assert "settingsCliModel" in app_js
    assert "cli_model" in app_js and "cli_effort" in app_js
    assert json.dumps({"ok": True})  # keep json import used
