"""Picking a resume target from the last CLI sessions of a workdir.

Covers the on-disk discovery service plus the Telegram and Desktop entry points.
"""

import asyncio
import hashlib
import json
import os
import urllib.parse
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import cli_session_history
from app.services.cli_session_history import CliSessionCandidate, list_recent_cli_sessions
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from sessions import session_ui as session_ui_module


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolate discovery from the real user's CLI history."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(cli_session_history, "_home_dirs", lambda: [home_dir])
    return home_dir


def _write_lines(path: Path, payloads: list[dict], mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(payload, ensure_ascii=False) for payload in payloads) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def _write_claude_session(home: Path, project_key: str, session_id: str, text: str, mtime: float) -> Path:
    return _write_lines(
        home / ".claude" / "projects" / project_key / f"{session_id}.jsonl",
        [
            {"type": "system", "content": "boot"},
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "subagent noise"},
            },
            {"type": "user", "message": {"role": "user", "content": text}},
        ],
        mtime,
    )


def _write_codex_rollout(
    home: Path,
    day: str,
    file_name: str,
    session_id: str,
    cwd: str,
    text: str,
    mtime: float,
) -> Path:
    return _write_lines(
        home / ".codex" / "sessions" / Path(day) / file_name,
        [
            {"type": "session_meta", "payload": {"session_id": session_id, "cwd": cwd}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            },
        ],
        mtime,
    )


def test_claude_lists_four_newest_sessions(fake_home):
    for index in range(6):
        _write_claude_session(
            fake_home, "-srv-demo-app", f"sid-{index}", f"запрос {index}", 1_700_000_000 + index
        )

    found = list_recent_cli_sessions("claude", "/srv/demo/app")

    assert [candidate.session_id for candidate in found] == ["sid-5", "sid-4", "sid-3", "sid-2"]
    assert found[0].cli == "claude"
    assert found[0].preview == "запрос 5"


def test_claude_preview_skips_sidechain_and_bridge_markers(fake_home):
    _write_claude_session(
        fake_home,
        "-srv-demo-app",
        "sid-marked",
        "<<<CLI_PROXY_REQUEST:4e303819-75da-43f0-a43b-8c077e66269f>>>\rСобери отчёт",
        1_700_000_000,
    )

    found = list_recent_cli_sessions("claude", "/srv/demo/app")

    assert [candidate.preview for candidate in found] == ["Собери отчёт"]


def test_claude_finds_project_dir_for_dotted_workdir(fake_home):
    _write_claude_session(fake_home, "-home-user--paperclip", "sid-dot", "привет", 1_700_000_000)

    found = list_recent_cli_sessions("claude", "/home/user/.paperclip")

    assert [candidate.session_id for candidate in found] == ["sid-dot"]


def test_claude_respects_explicit_limit(fake_home):
    for index in range(3):
        _write_claude_session(
            fake_home, "-srv-demo-app", f"sid-{index}", f"запрос {index}", 1_700_000_000 + index
        )

    found = list_recent_cli_sessions("claude", "/srv/demo/app", limit=2)

    assert [candidate.session_id for candidate in found] == ["sid-2", "sid-1"]


def test_codex_lists_only_sessions_of_this_workdir(fake_home):
    _write_codex_rollout(
        fake_home,
        "2026/07/18",
        "rollout-2026-07-18T08-52-39-019f746c.jsonl",
        "019f746c",
        "/srv/demo/app",
        "почему упал деплой?",
        1_700_000_100,
    )
    _write_codex_rollout(
        fake_home,
        "2026/07/17",
        "rollout-2026-07-17T08-52-39-019f0000.jsonl",
        "019f0000",
        "/srv/other/project",
        "чужая сессия",
        1_700_000_200,
    )

    found = list_recent_cli_sessions("codex", "/srv/demo/app")

    assert [candidate.session_id for candidate in found] == ["019f746c"]
    assert found[0].preview == "почему упал деплой?"


def test_qwen_lists_sessions_for_dotted_workdir(fake_home):
    _write_lines(
        fake_home / ".qwen" / "projects" / "-home-user--paperclip" / "chats" / "sid-q.jsonl",
        [
            {"type": "system", "systemPayload": {"phase": "invocation"}},
            {"type": "user", "message": {"parts": [{"text": "обнови версию"}]}},
        ],
        1_700_000_000,
    )

    found = list_recent_cli_sessions("qwen", "/home/user/.paperclip")

    assert [(candidate.session_id, candidate.preview) for candidate in found] == [
        ("sid-q", "обнови версию")
    ]


def test_gemini_lists_sessions_matching_project_hash(fake_home):
    workdir = "/srv/demo/app"
    project_hash = hashlib.sha256(workdir.encode("utf-8")).hexdigest()
    chats = fake_home / ".gemini" / "tmp" / project_hash / "chats"
    chats.mkdir(parents=True)
    for session_id, phash, mtime in (
        ("11111111-1111-1111-1111-111111111111", project_hash, 1_700_000_000),
        ("22222222-2222-2222-2222-222222222222", "deadbeef", 1_700_000_500),
    ):
        path = chats / f"session-{session_id}.json"
        path.write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "projectHash": phash,
                    "messages": [{"type": "user", "content": "What is 2 + 2?"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))

    found = list_recent_cli_sessions("gemini", workdir)

    assert [candidate.session_id for candidate in found] == [
        "11111111-1111-1111-1111-111111111111"
    ]
    assert found[0].preview == "What is 2 + 2?"


def test_grok_lists_sessions_with_summary_preview(fake_home):
    workdir = "/srv/demo/app"
    key = urllib.parse.quote(workdir, safe="")
    session_dir = fake_home / ".grok" / "sessions" / key / "sid-grok"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text(
        json.dumps({"generated_title": "Починка CI"}, ensure_ascii=False), encoding="utf-8"
    )
    os.utime(session_dir, (1_700_000_000, 1_700_000_000))

    found = list_recent_cli_sessions("grok", workdir)

    assert [(candidate.session_id, candidate.preview) for candidate in found] == [
        ("sid-grok", "Починка CI")
    ]


def test_kimi_lists_sessions_of_this_workspace_only(fake_home):
    workdir = "/srv/demo/app"
    bucket = fake_home / ".kimi-code" / "sessions" / "wd_app_fa69cc192fc6"
    titled = bucket / "session_titled"
    titled.mkdir(parents=True)
    (titled / "state.json").write_text(
        json.dumps({"title": "Разбор логов"}, ensure_ascii=False), encoding="utf-8"
    )
    _write_lines(
        titled / "agents" / "main" / "wire.jsonl",
        [{"type": "metadata", "protocol_version": "1.5", "created_at": 1}],
        1_700_000_500,
    )
    _write_lines(
        bucket / "session_plain" / "agents" / "main" / "wire.jsonl",
        [
            {
                "type": "context.append_message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "напоминание"}],
                    "origin": {"kind": "injection"},
                },
            },
            {
                "type": "context.append_message",
                "message": {"role": "user", "content": [{"type": "text", "text": "обнови зависимости"}]},
            },
        ],
        1_700_000_000,
    )
    _write_lines(
        fake_home / ".kimi-code" / "sessions" / "wd_other_000000000000" / "session_alien"
        / "agents" / "main" / "wire.jsonl",
        [{"type": "metadata", "protocol_version": "1.5", "created_at": 1}],
        1_700_000_900,
    )

    found = list_recent_cli_sessions("kimi", workdir)

    assert [(candidate.session_id, candidate.preview) for candidate in found] == [
        ("session_titled", "Разбор логов"),
        ("session_plain", "обнови зависимости"),
    ]


def test_unknown_cli_and_empty_workdir_return_nothing(fake_home):
    assert list_recent_cli_sessions("unknown-cli", "/srv/demo/app") == []
    assert list_recent_cli_sessions("claude", "") == []
    assert list_recent_cli_sessions("claude", "/srv/demo/app", limit=0) == []


def _build_app(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_a, **_kw: None
    return app


class _Query:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(chat_id=1, message_id=1)


def _candidates() -> list[CliSessionCandidate]:
    return [
        CliSessionCandidate(
            cli="dummy", session_id="sid-new", mtime=1_700_000_500, preview="свежая задача"
        ),
        CliSessionCandidate(cli="dummy", session_id="sid-old", mtime=1_700_000_000, preview=""),
    ]


def _patch_history(monkeypatch, candidates):
    monkeypatch.setattr(
        session_ui_module, "list_recent_cli_sessions", lambda *_a, **_kw: list(candidates)
    )


def _capture_edit(app):
    captured = {}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None):
        captured["text"] = text
        captured["markup"] = reply_markup
        return True

    app.session_ui._edit_msg = _fake_edit_msg
    return captured


def test_resume_button_shows_recent_sessions_picker(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.resume_token = "sid-old"
    _patch_history(monkeypatch, _candidates())
    captured = _capture_edit(app)

    handled = asyncio.run(
        app.session_ui.handle_callback(_Query(f"sess_resume:{session.id}"), 1, None)
    )

    assert handled is True
    buttons = [btn for row in captured["markup"].inline_keyboard for btn in row]
    callbacks = [btn.callback_data for btn in buttons]
    assert f"sess_rpick:{session.id}:sid-new" in callbacks
    assert f"sess_rpick:{session.id}:sid-old" in callbacks
    assert f"sess_rmanual:{session.id}" in callbacks
    # Newest first, the current resume target is marked, empty preview falls back to the id.
    assert "свежая задача" in buttons[0].text
    assert buttons[1].text.startswith("✅")
    assert "sid-old" in buttons[1].text


def test_resume_picker_without_history_offers_manual_input(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    _patch_history(monkeypatch, [])
    captured = _capture_edit(app)

    asyncio.run(app.session_ui.handle_callback(_Query(f"sess_resume:{session.id}"), 1, None))

    callbacks = [btn.callback_data for row in captured["markup"].inline_keyboard for btn in row]
    assert callbacks[0] == f"sess_rmanual:{session.id}"
    assert "не найдено" in captured["text"]


def test_resume_pick_sets_resume_token(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    _patch_history(monkeypatch, _candidates())
    captured = _capture_edit(app)

    handled = asyncio.run(
        app.session_ui.handle_callback(_Query(f"sess_rpick:{session.id}:sid-new"), 1, None)
    )

    assert handled is True
    assert session.resume_token == "sid-new"
    assert "Resume обновлен." in captured["text"]


def test_resume_pick_on_stale_list_keeps_token(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.resume_token = "sid-old"
    _patch_history(monkeypatch, _candidates())
    captured = _capture_edit(app)

    asyncio.run(
        app.session_ui.handle_callback(_Query(f"sess_rpick:{session.id}:sid-gone"), 1, None)
    )

    assert session.resume_token == "sid-old"
    assert captured["text"] == "Сессия не найдена — список устарел. Откройте Resume заново."


def test_resume_manual_button_waits_for_typed_token(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    _patch_history(monkeypatch, _candidates())
    captured = _capture_edit(app)

    asyncio.run(app.session_ui.handle_callback(_Query(f"sess_rmanual:{session.id}"), 1, None))

    assert list(app.session_ui.pending_session_resume.values()) == [
        {"owner_chat_id": 1, "session_id": session.id}
    ]
    assert "Введите новый resume" in captured["text"]


def test_resume_pick_callback_data_fits_telegram_limit(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    long_id = "a" * 80
    _patch_history(
        monkeypatch,
        [CliSessionCandidate(cli="dummy", session_id=long_id, mtime=1_700_000_000, preview="x")],
    )
    captured = _capture_edit(app)

    asyncio.run(app.session_ui.handle_callback(_Query(f"sess_resume:{session.id}"), 1, None))
    data = captured["markup"].inline_keyboard[0][0].callback_data
    assert len(data) == session_ui_module.CALLBACK_DATA_MAX_LEN

    asyncio.run(app.session_ui.handle_callback(_Query(data), 1, None))
    assert session.resume_token == long_id


def _desktop_widget_stub(session, monkeypatch, candidates):
    from desktop.widgets import session_manager as desktop_session_manager

    monkeypatch.setattr(
        desktop_session_manager, "list_recent_cli_sessions", lambda *_a, **_kw: list(candidates)
    )
    widget = SimpleNamespace(
        facade=SimpleNamespace(ui_language="ru"),
        session_service=SimpleNamespace(
            get_session_by_uid=lambda _uid: session,
            _manager=SimpleNamespace(_persist_sessions=lambda: None),
        ),
        session_list=SimpleNamespace(
            selectedItems=lambda: [SimpleNamespace(data=lambda _role: "uid-1")]
        ),
        refresh_sessions=lambda: None,
    )
    cls = desktop_session_manager.SessionManagerWidget
    widget._resume_candidate_label = cls._resume_candidate_label
    widget._apply_resume_token = partial(cls._apply_resume_token, widget)
    widget._ask_resume_token_manually = partial(cls._ask_resume_token_manually, widget)
    return desktop_session_manager, widget


def test_desktop_resume_menu_picks_session_from_history(monkeypatch):
    session = SimpleNamespace(id="s1", resume_token=None, workdir="/srv/demo/app", active_cli="dummy")
    module, widget = _desktop_widget_stub(session, monkeypatch, _candidates())
    shown = {}

    def _fake_get_item(_parent, _title, _label, items, _current, _editable):
        shown["items"] = list(items)
        return items[0], True

    monkeypatch.setattr(module.QInputDialog, "getItem", _fake_get_item)

    module.SessionManagerWidget._on_resume_edit(widget)

    assert session.resume_token == "sid-new"
    assert "свежая задача" in shown["items"][0]
    assert shown["items"][-1] == "✍️ Ввести id вручную"


def test_desktop_resume_menu_falls_back_to_manual_input(monkeypatch):
    session = SimpleNamespace(id="s1", resume_token=None, workdir="/srv/demo/app", active_cli="dummy")
    module, widget = _desktop_widget_stub(session, monkeypatch, _candidates())

    monkeypatch.setattr(
        module.QInputDialog,
        "getItem",
        lambda _parent, _title, _label, items, _current, _editable: (items[-1], True),
    )
    monkeypatch.setattr(
        module.QInputDialog,
        "getText",
        lambda _parent, _title, _label, text="": ("typed-token", True),
    )

    module.SessionManagerWidget._on_resume_edit(widget)

    assert session.resume_token == "typed-token"


def test_desktop_resume_menu_without_history_asks_for_token(monkeypatch):
    session = SimpleNamespace(id="s1", resume_token=None, workdir="/srv/demo/app", active_cli="dummy")
    module, widget = _desktop_widget_stub(session, monkeypatch, [])

    def _fail_get_item(*_args, **_kwargs):
        raise AssertionError("picker must be skipped when there is no history")

    monkeypatch.setattr(module.QInputDialog, "getItem", _fail_get_item)
    monkeypatch.setattr(
        module.QInputDialog,
        "getText",
        lambda _parent, _title, _label, text="": ("typed-token", True),
    )

    module.SessionManagerWidget._on_resume_edit(widget)

    assert session.resume_token == "typed-token"
