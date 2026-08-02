import asyncio
import hashlib
import inspect
import os
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
import yaml

from app.services.config_service import ConfigService, FileConfigProvider
from app.services.report_history_service import ReportHistoryService
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig, app_config_to_dict
from modes.sdk.planning import ProjectPlan, save_plan
from sessions.scoped_key import session_scoped_key
from tg.command_registry import build_command_registry
from tg.callbacks import CallbackHandler
from tg.handlers import BotHandlers


def _write_setprompt_config(tmp_path: Path, *, prompt_regex: str | None = "old") -> AppConfig:
    cfg = AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[101], admlist_chat_ids=[101]),
        tools={
            "codex": ToolConfig(
                name="codex",
                mode="headless",
                cmd=["codex"],
                prompt_regex=prompt_regex,
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
    Path(cfg.path).write_text(
        yaml.safe_dump(app_config_to_dict(cfg), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return cfg


def test_validate_telegram_runtime_payload_requires_session_uid() -> None:
    handlers = BotHandlers(types.SimpleNamespace())

    with pytest.raises(
        ValidationError,
        match=r"telegram runtime payload is invalid: session_uid is required",
    ):
        handlers._validate_telegram_runtime_payload({"chat_id": 101})


def test_validate_telegram_runtime_payload_accepts_explicit_session_uid() -> None:
    handlers = BotHandlers(types.SimpleNamespace())
    payload = {
        "chat_id": 101,
        "session_uid": "thread:101:55",
        "mode_id": "admin",
    }

    validated = handlers._validate_telegram_runtime_payload(payload)

    assert validated == payload


def test_is_admin_fails_closed_without_policy_wiring() -> None:
    handlers = BotHandlers(types.SimpleNamespace())

    assert handlers._is_admin(101) is False


def test_is_session_visible_fails_closed_without_visibility_checker() -> None:
    handlers = BotHandlers(types.SimpleNamespace())

    assert handlers._is_session_visible_for_chat(101, types.SimpleNamespace(id="s1")) is False


def _make_full_app() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        cmd_start=object(),
        cmd_sessions=object(),
        cmd_interrupt=object(),
        cmd_git=object(),
        cmd_files=object(),
        cmd_miniapp=object(),
        cmd_selfupdate=object(),
        cmd_preset=object(),
        cmd_metrics=object(),
        cmd_tools=object(),
        cmd_newpath=object(),
        cmd_close=object(),
        cmd_status=object(),
        cmd_reports=object(),
        cmd_limits=object(),
        cmd_queue=object(),
        cmd_clearqueue=object(),
        cmd_rename=object(),
        cmd_cwd=object(),
        cmd_dirs=object(),
        cmd_resume=object(),
        cmd_state=object(),
        cmd_setprompt=object(),
        cmd_send=object(),
        cmd_lint_evolution_status=object(),
        cmd_lint_autopause_resume=object(),
        cmd_lint_schema_history=object(),
        cmd_lint_gate_dry_run=object(),
        cmd_sessions_search=object(),
        cmd_git_branch=object(),
        cmd_git_checkout=object(),
        cmd_git_stash_pop=object(),
        cmd_git_show=object(),
        cmd_remote_git_pull=object(),
        cmd_remote_git_push=object(),
        cmd_remote_git_fetch=object(),
        mode_registry_service=None,
    )


def test_command_registry_registers_limits_command() -> None:
    app = _make_full_app()

    registry = build_command_registry(app)
    names = {str(item.get("name") or "") for item in registry}

    assert "limits" in names
    limits_entry = next(item for item in registry if str(item.get("name") or "") == "limits")
    assert limits_entry["menu"] is True


def test_command_registry_registers_reports_command() -> None:
    app = _make_full_app()

    registry = build_command_registry(app)
    names = {str(item.get("name") or "") for item in registry}

    assert "reports" in names
    reports_entry = next(item for item in registry if str(item.get("name") or "") == "reports")
    assert reports_entry["menu"] is True


def test_command_registry_registers_git_subcommands() -> None:
    app = _make_full_app()

    registry = build_command_registry(app)
    names = {str(item.get("name") or "") for item in registry}

    for cmd in ("git_branch", "git_checkout", "git_stash_pop", "git_show"):
        assert cmd in names, f"Command '{cmd}' not registered"

    for cmd in ("git_branch", "git_checkout", "git_stash_pop", "git_show"):
        entry = next(item for item in registry if str(item.get("name") or "") == cmd)
        assert entry["menu"] is False, f"Command '{cmd}' should not appear in menu"


@pytest.mark.asyncio
async def test_cmd_reports_lists_current_session_reports(tmp_path: Path) -> None:
    service = ReportHistoryService()
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
    service.save_markdown_report(session, "# First", now=1000)
    sent: list[str] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        sent.append(str(text))

    route = types.SimpleNamespace(
        owner_chat_id=101,
        reply_kwargs=lambda: {"chat_id": 101},
    )
    bot_app = types.SimpleNamespace(
        report_history_service=service,
        ensure_telegram_inbound_session=AsyncMock(return_value=(route, session)),
        _send_message=_send_message,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=[])

    await handlers.cmd_reports(update, context)

    assert len(sent) == 1
    assert "report_19700101_001640.md" in sent[0]
    assert "/reports latest" in sent[0]


@pytest.mark.asyncio
async def test_cmd_reports_latest_sends_document(tmp_path: Path) -> None:
    service = ReportHistoryService()
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
    service.save_markdown_report(session, "# First", now=1000)
    documents: list[tuple[str, bytes]] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        raise AssertionError(f"unexpected message: {text}")

    async def _send_document(_ctx, *, document, filename: str, **_kw):
        documents.append((filename, document.read()))
        return True

    route = types.SimpleNamespace(
        owner_chat_id=101,
        reply_kwargs=lambda: {"chat_id": 101},
    )
    bot_app = types.SimpleNamespace(
        report_history_service=service,
        ensure_telegram_inbound_session=AsyncMock(return_value=(route, session)),
        _send_message=_send_message,
        _send_document=_send_document,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["latest"])

    await handlers.cmd_reports(update, context)

    assert documents == [("report_19700101_001640.md", b"# First")]


@pytest.mark.asyncio
async def test_cmd_reports_latest_sends_pdf_document(tmp_path: Path) -> None:
    service = ReportHistoryService()
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
    reports_dir = Path(service.ensure_reports_dir(session))
    pdf_path = reports_dir / "latest.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    os.utime(pdf_path, (2000, 2000))
    documents: list[tuple[str, bytes]] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        raise AssertionError(f"unexpected message: {text}")

    async def _send_document(_ctx, *, document, filename: str, **_kw):
        documents.append((filename, document.read()))
        return True

    route = types.SimpleNamespace(
        owner_chat_id=101,
        reply_kwargs=lambda: {"chat_id": 101},
    )
    bot_app = types.SimpleNamespace(
        report_history_service=service,
        ensure_telegram_inbound_session=AsyncMock(return_value=(route, session)),
        _send_message=_send_message,
        _send_document=_send_document,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["latest"])

    await handlers.cmd_reports(update, context)

    assert documents == [("latest.pdf", b"%PDF-1.4\n")]


@pytest.mark.asyncio
async def test_cmd_reports_generate_saves_plan_report_and_sends_document(tmp_path: Path) -> None:
    service = ReportHistoryService()
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), chat_id=101)
    save_plan(
        session.workdir,
        ProjectPlan(project_goal="Generate reports", tasks=[]),
        scoped_key=session_scoped_key(session),
    )
    messages: list[str] = []
    documents: list[tuple[str, bytes]] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        messages.append(str(text))

    async def _send_document(_ctx, *, document, filename: str, **_kw):
        documents.append((filename, document.read()))
        return True

    route = types.SimpleNamespace(
        owner_chat_id=101,
        reply_kwargs=lambda: {"chat_id": 101},
    )
    bot_app = types.SimpleNamespace(
        report_history_service=service,
        ensure_telegram_inbound_session=AsyncMock(return_value=(route, session)),
        _send_message=_send_message,
        _send_document=_send_document,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["generate"])

    await handlers.cmd_reports(update, context)

    reports = service.list_reports(session)
    assert len(messages) == 1
    assert len(reports) == 1
    assert reports[0].report_id.startswith("manager_plan_")
    assert documents[0][0] == reports[0].report_id
    assert b"# Project Report: Generate reports" in documents[0][1]


@pytest.mark.asyncio
async def test_cmd_reports_snapshot_saves_html_and_sends_document(tmp_path: Path) -> None:
    service = ReportHistoryService()
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), chat_id=101)
    messages: list[str] = []
    documents: list[tuple[str, bytes]] = []

    class _SnapshotService:
        def save_html_report(self, target_session, *, lang="en"):
            assert lang == "ru"
            return service.save_html_report(
                target_session,
                "<!doctype html><h1>Snapshot</h1>",
                prefix="session_snapshot",
                now=1000,
            )

    async def _send_message(_ctx, *, text: str, **_kw):
        messages.append(str(text))

    async def _send_document(_ctx, *, document, filename: str, **_kw):
        documents.append((filename, document.read()))
        return True

    route = types.SimpleNamespace(
        owner_chat_id=101,
        reply_kwargs=lambda: {"chat_id": 101},
    )
    bot_app = types.SimpleNamespace(
        report_history_service=service,
        session_snapshot_report_service=_SnapshotService(),
        ensure_telegram_inbound_session=AsyncMock(return_value=(route, session)),
        _send_message=_send_message,
        _send_document=_send_document,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["snapshot"])

    await handlers.cmd_reports(update, context)

    assert messages == ["HTML-отчёт по сессии создан: session_snapshot_19700101_001640.html"]
    assert documents == [("session_snapshot_19700101_001640.html", b"<!doctype html><h1>Snapshot</h1>")]


@pytest.mark.asyncio
async def test_session_snapshot_callback_sends_html_document(tmp_path: Path) -> None:
    service = ReportHistoryService()
    session_uid = "chat:101:s1"
    session = types.SimpleNamespace(
        id="s1",
        chat_id=101,
        workdir=str(tmp_path),
        conversation_scope=types.SimpleNamespace(session_uid=session_uid),
        tool=types.SimpleNamespace(name="codex"),
        cli=types.SimpleNamespace(active_cli="codex", resume_tokens={}),
        modes=types.SimpleNamespace(active_mode="agent"),
        queue=[],
    )
    summary = service.save_html_report(
        session,
        "<!doctype html><h1>Callback snapshot</h1>",
        prefix="session_snapshot",
        now=1000,
    )
    documents: list[tuple[str, bytes, dict]] = []
    edits: list[str] = []

    class _Manager:
        def get_by_uid(self, token):
            return session if str(token) == session_uid else None

    class _SnapshotService:
        def save_html_report(self, target_session, *, lang="en"):
            assert target_session is session
            assert lang == "ru"
            return summary

    async def _send_document(_ctx, *, document, filename: str, **kwargs):
        documents.append((filename, document.read(), dict(kwargs)))
        return True

    async def _edit_message(_ctx, *, text: str, **_kwargs):
        edits.append(str(text))
        return True

    bot_app = types.SimpleNamespace(
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
        manager=_Manager(),
        handlers=types.SimpleNamespace(_is_session_visible_for_chat=lambda _chat_id, _session: True),
        session_snapshot_report_service=_SnapshotService(),
        _send_document=_send_document,
        _edit_message=_edit_message,
        build_telegram_reply_dest=lambda _session, chat_id: {"chat_id": int(chat_id)},
        resolve_telegram_callback_scope=lambda _query: (101, None, 101, session),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(
        data=f"sess_snapshot:{session_uid}",
        message=types.SimpleNamespace(chat_id=101, message_id=7),
    )

    ok = await handler._cb_sess_snapshot(
        data=f"sess_snapshot:{session_uid}",
        chat_id=101,
        query=query,
        context=object(),
    )

    assert ok is True
    assert documents == [
        (
            "session_snapshot_19700101_001640.html",
            b"<!doctype html><h1>Callback snapshot</h1>",
            {"chat_id": 101},
        )
    ]
    assert edits[-1] == "HTML-отчёт по сессии создан: session_snapshot_19700101_001640.html"


def _tmux_reread_bot_app(session, session_uid: str, *, is_admin: bool, outcome: str, edits):
    class _Manager:
        def get_by_uid(self, token):
            return session if str(token) == session_uid else None

    async def _reread(target_session, _context):
        assert target_session is session
        return outcome

    async def _edit_message(_ctx, *, text: str, **_kwargs):
        edits.append(str(text))
        return True

    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
        manager=_Manager(),
        handlers=types.SimpleNamespace(
            _is_admin=lambda _chat_id: is_admin,
            build_sessions_active_overview=lambda _chat_id, session=None: ("overview", None),
        ),
        session_management=types.SimpleNamespace(reread_tmux_output=_reread),
        _edit_message=_edit_message,
        resolve_telegram_callback_scope=lambda _query: (101, None, 101, session),
    )


def _tmux_reread_query(session_uid: str, answers: list[tuple[str, bool]]):
    async def _answer(text=None, show_alert=False, **_kwargs):
        answers.append((str(text or ""), bool(show_alert)))

    return types.SimpleNamespace(
        data=f"sess_tmux_reread:{session_uid}",
        message=types.SimpleNamespace(chat_id=101, message_id=7),
        answer=_answer,
    )


@pytest.mark.asyncio
async def test_tmux_reread_callback_reports_outcome() -> None:
    session_uid = "chat:101:s1"
    session = types.SimpleNamespace(id="s1", chat_id=101, queue=[])
    answers: list[tuple[str, bool]] = []
    edits: list[str] = []
    bot_app = _tmux_reread_bot_app(
        session,
        session_uid,
        is_admin=True,
        outcome="no_request",
        edits=edits,
    )
    handler = CallbackHandler(bot_app)

    ok = await handler._cb_sess_tmux_reread(
        data=f"sess_tmux_reread:{session_uid}",
        chat_id=101,
        query=_tmux_reread_query(session_uid, answers),
        context=object(),
    )

    assert ok is True
    assert answers == [("Нет активного запроса tmux для перечитывания", True)]
    assert edits[-1] == "overview"


@pytest.mark.asyncio
async def test_tmux_reread_callback_rejects_non_admin() -> None:
    session_uid = "chat:101:s1"
    session = types.SimpleNamespace(id="s1", chat_id=101, queue=[])
    answers: list[tuple[str, bool]] = []
    edits: list[str] = []
    called: list[str] = []

    bot_app = _tmux_reread_bot_app(
        session,
        session_uid,
        is_admin=False,
        outcome="started",
        edits=edits,
    )

    async def _forbidden(_session, _context):
        called.append("reread")
        return "started"

    bot_app.session_management.reread_tmux_output = _forbidden
    handler = CallbackHandler(bot_app)

    ok = await handler._cb_sess_tmux_reread(
        data=f"sess_tmux_reread:{session_uid}",
        chat_id=101,
        query=_tmux_reread_query(session_uid, answers),
        context=object(),
    )

    assert ok is True
    assert called == []
    assert edits[-1] == "Сессия недоступна."


@pytest.mark.asyncio
async def test_cmd_git_branch_sends_usage_when_no_args() -> None:
    sent: list[str] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        sent.append(str(text))

    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(lang="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _upd, _s=None: {}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=[])

    await handlers.cmd_git_branch(update, context)

    assert len(sent) == 1
    assert "/git_branch" in sent[0]


@pytest.mark.asyncio
async def test_cmd_git_show_uses_head_by_default() -> None:
    sent: list[str] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        sent.append(str(text))

    git_mock = types.SimpleNamespace(
        ensure_git_session=AsyncMock(return_value=types.SimpleNamespace(workdir="/tmp")),
        ensure_git_repo=AsyncMock(return_value=True),
        git_show=AsyncMock(return_value=(0, "commit abc\n stat: 1 file")),
    )
    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        git=git_mock,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(lang="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _upd, _s=None: {}  # type: ignore[method-assign]

    route = types.SimpleNamespace(message_thread_id=None)
    bot_app.resolve_telegram_inbound_route = lambda _upd: route

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=[])

    await handlers.cmd_git_show(update, context)

    git_mock.git_show.assert_awaited_once()
    call_args = git_mock.git_show.call_args
    assert call_args[0][1] == "HEAD"
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ref", ["-rf", "HEAD:path", "..", "../etc/passwd", "HEAD..main"])
async def test_cmd_git_show_rejects_invalid_ref(bad_ref: str) -> None:
    sent: list[str] = []

    async def _send_message(_ctx, *, text: str, **_kw):
        sent.append(str(text))

    git_mock = types.SimpleNamespace(
        ensure_git_session=AsyncMock(return_value=types.SimpleNamespace(workdir="/tmp")),
        ensure_git_repo=AsyncMock(return_value=True),
        git_show=AsyncMock(return_value=(0, "")),
    )
    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        git=git_mock,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(lang="ru"),
        ),
        resolve_telegram_inbound_route=lambda _upd: types.SimpleNamespace(message_thread_id=None),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _upd, _s=None: {}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=[bad_ref])

    await handlers.cmd_git_show(update, context)

    git_mock.git_show.assert_not_awaited()
    assert len(sent) == 1


def test_cmd_setprompt_does_not_use_legacy_save_config() -> None:
    source = inspect.getsource(BotHandlers.cmd_setprompt)

    assert "from config import save_config" not in source
    assert "save_config(self.bot_app.config)" not in source


@pytest.mark.asyncio
async def test_cmd_setprompt_saves_through_config_service_and_reports_summary(tmp_path: Path) -> None:
    cfg = _write_setprompt_config(tmp_path)
    original = yaml.safe_load(Path(cfg.path).read_text(encoding="utf-8"))
    service = ConfigService(FileConfigProvider(cfg.path))
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    reload_runtime_config = AsyncMock(
        return_value={
            "status": "success",
            "applied": ["tools.*"],
            "restart_required": [],
            "warnings": [],
        }
    )
    bot_app = types.SimpleNamespace(
        container=types.SimpleNamespace(config_service=service),
        _send_message=_send_message,
        reload_runtime_config=reload_runtime_config,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["codex", "^new-prompt$"])

    await handlers.cmd_setprompt(update, context)

    saved = yaml.safe_load(Path(cfg.path).read_text(encoding="utf-8"))
    backup = yaml.safe_load(Path(f"{cfg.path}.bak").read_text(encoding="utf-8"))
    assert saved["tools"]["codex"]["prompt_regex"] == "^new-prompt$"
    assert backup == original
    reload_runtime_config.assert_awaited_once()
    assert len(sent) == 1
    assert "prompt_regex сохранен." in sent[0]
    assert "changed: yes" in sent[0]
    assert "restart_required: none" in sent[0]
    assert "reloadable: tools.codex.prompt_regex" in sent[0]
    assert "not_applied: none" in sent[0]
    assert "errors: none" in sent[0]
    assert f"backup_path: {cfg.path}.bak" in sent[0]
    assert "runtime_reload: success" in sent[0]


@pytest.mark.asyncio
async def test_cmd_setprompt_validation_error_does_not_write_config(tmp_path: Path) -> None:
    cfg = _write_setprompt_config(tmp_path)
    original_text = Path(cfg.path).read_text(encoding="utf-8")
    service = ConfigService(FileConfigProvider(cfg.path))
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    reload_runtime_config = AsyncMock()
    bot_app = types.SimpleNamespace(
        container=types.SimpleNamespace(config_service=service),
        _send_message=_send_message,
        reload_runtime_config=reload_runtime_config,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["codex", ""])

    await handlers.cmd_setprompt(update, context)

    assert Path(cfg.path).read_text(encoding="utf-8") == original_text
    assert not Path(f"{cfg.path}.bak").exists()
    reload_runtime_config.assert_not_awaited()
    assert len(sent) == 1
    assert "prompt_regex не сохранен." in sent[0]
    assert "changed: no" in sent[0]
    assert "errors: " in sent[0]
    assert "tools.codex.prompt_regex" in sent[0]


@pytest.mark.asyncio
async def test_cmd_setprompt_unknown_tool_does_not_write_config(tmp_path: Path) -> None:
    cfg = _write_setprompt_config(tmp_path)
    original_text = Path(cfg.path).read_text(encoding="utf-8")
    service = ConfigService(FileConfigProvider(cfg.path))
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    reload_runtime_config = AsyncMock()
    bot_app = types.SimpleNamespace(
        container=types.SimpleNamespace(config_service=service),
        _send_message=_send_message,
        reload_runtime_config=reload_runtime_config,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["missing", "^new-prompt$"])

    await handlers.cmd_setprompt(update, context)

    assert Path(cfg.path).read_text(encoding="utf-8") == original_text
    assert not Path(f"{cfg.path}.bak").exists()
    reload_runtime_config.assert_not_awaited()
    assert sent == ["Инструмент не найден."]


@pytest.mark.asyncio
async def test_cmd_setprompt_reads_revision_before_save(tmp_path: Path) -> None:
    class _TrackingConfigService(ConfigService):
        def __init__(self, provider):
            super().__init__(provider)
            self.calls: list[object] = []

        async def current_revision(self, config=None) -> str:
            self.calls.append("current_revision")
            return await super().current_revision(config)

        async def save_config_draft_with_revision(self, config, *, expected_revision: str | None):
            self.calls.append(("save", expected_revision))
            return await super().save_config_draft_with_revision(
                config,
                expected_revision=expected_revision,
            )

    cfg = _write_setprompt_config(tmp_path)
    expected_revision = hashlib.sha256(Path(cfg.path).read_bytes()).hexdigest()
    service = _TrackingConfigService(FileConfigProvider(cfg.path))
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    bot_app = types.SimpleNamespace(
        container=types.SimpleNamespace(config_service=service),
        _send_message=_send_message,
        reload_runtime_config=AsyncMock(
            return_value={
                "status": "success",
                "applied": ["tools.*"],
                "restart_required": [],
                "warnings": [],
            }
        ),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["codex", "^new-prompt$"])

    await handlers.cmd_setprompt(update, context)

    assert service.calls == ["current_revision", ("save", expected_revision)]
    assert len(sent) == 1
    assert "prompt_regex сохранен." in sent[0]


@pytest.mark.asyncio
async def test_cmd_setprompt_revision_error_does_not_write_config(tmp_path: Path) -> None:
    class _StaleRevisionConfigService(ConfigService):
        async def current_revision(self, config=None) -> str:
            _ = config
            return "stale-revision"

    cfg = _write_setprompt_config(tmp_path)
    original_text = Path(cfg.path).read_text(encoding="utf-8")
    service = _StaleRevisionConfigService(FileConfigProvider(cfg.path))
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    reload_runtime_config = AsyncMock()
    bot_app = types.SimpleNamespace(
        container=types.SimpleNamespace(config_service=service),
        _send_message=_send_message,
        reload_runtime_config=reload_runtime_config,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["codex", "^new-prompt$"])

    await handlers.cmd_setprompt(update, context)

    assert Path(cfg.path).read_text(encoding="utf-8") == original_text
    assert not Path(f"{cfg.path}.bak").exists()
    reload_runtime_config.assert_not_awaited()
    assert len(sent) == 1
    assert "prompt_regex не сохранен." in sent[0]
    assert "revision mismatch" in sent[0]


def test_reply_kwargs_prefers_inbound_route_thread_context() -> None:
    handlers = BotHandlers(
        types.SimpleNamespace(
            resolve_telegram_inbound_route=lambda _update: types.SimpleNamespace(
                reply_chat_id=101,
                message_thread_id=55,
            ),
            build_telegram_reply_dest=lambda *_args, **_kwargs: {"chat_id": 999},
        )
    )

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=999))

    assert handlers._reply_kwargs(update) == {"chat_id": 101, "message_thread_id": 55}


def test_admin_show_mode_menu_validates_and_passes_session_uid() -> None:
    async def _run() -> None:
        plugin_calls = []

        class _Plugin:
            def build_menu(self, *_args, **_kwargs):
                return "unused", None

            async def handle_input(self, message, ctx):
                plugin_calls.append((message, ctx))
                return None

        session = types.SimpleNamespace(
            id="sess-1",
            conversation_scope=types.SimpleNamespace(session_uid="thread:101:55"),
        )
        bot_app = types.SimpleNamespace(
            mode_registry_service=types.SimpleNamespace(get=lambda _mode_id: _Plugin()),
            access_policy_service=types.SimpleNamespace(is_mode_allowed_for_chat=lambda _chat_id, _mode_id: True),
            resolve_telegram_inbound_route=lambda _update: types.SimpleNamespace(
                owner_chat_id=101,
                reply_chat_id=101,
                message_thread_id=55,
                session_uid="thread:101:55",
            ),
            build_telegram_transport_context=lambda _context, **kwargs: {"dest": kwargs.get("dest")},
            build_telegram_reply_dest=lambda *_args, **_kwargs: {"kind": "telegram"},
            _send_message=AsyncMock(),
        )
        handlers = BotHandlers(bot_app)
        handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        handlers._require_scope_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        handlers._reply_kwargs = lambda _update, _session=None: {}  # type: ignore[method-assign]

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=101),
            effective_user=types.SimpleNamespace(id=42),
        )
        context = types.SimpleNamespace(args=["status"])

        await handlers._show_mode_menu(
            update,
            context,
            "admin",
            subcommand="status",
            command_args=["status"],
        )

        assert len(plugin_calls) == 1
        _message, runtime_ctx = plugin_calls[0]
        assert runtime_ctx["chat_id"] == 101
        assert runtime_ctx["session_uid"] == "thread:101:55"

    asyncio.run(_run())


def test_show_mode_menu_applies_simple_user_visibility_policy() -> None:
    async def _run() -> None:
        captured = {}

        class _Plugin:
            def build_menu(self, *_args, **kwargs):
                captured["menu_visibility"] = kwargs.get("menu_visibility")
                return "menu", None

            async def handle_input(self, message, ctx):
                _ = message
                _ = ctx
                return None

        session = types.SimpleNamespace(
            id="sess-1",
            chat_id=101,
            modes=types.SimpleNamespace(active_mode="agent"),
            conversation_scope=types.SimpleNamespace(session_uid="thread:101:55"),
        )
        bot_app = types.SimpleNamespace(
            mode_registry_service=types.SimpleNamespace(get=lambda _mode_id: _Plugin()),
            access_policy_service=types.SimpleNamespace(
                is_mode_allowed_for_chat=lambda _chat_id, _mode_id: True,
                is_admin=lambda _chat_id, scope="generic": False,
            ),
            resolve_telegram_inbound_route=lambda _update: types.SimpleNamespace(
                owner_chat_id=101,
                reply_chat_id=101,
                message_thread_id=55,
                session_uid="thread:101:55",
            ),
            build_telegram_transport_context=lambda _context, **kwargs: {"dest": kwargs.get("dest")},
            build_telegram_reply_dest=lambda *_args, **_kwargs: {"kind": "telegram"},
            _send_message=AsyncMock(),
        )
        handlers = BotHandlers(bot_app)
        handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        handlers._require_scope_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
        handlers._reply_kwargs = lambda _update, _session=None: {}  # type: ignore[method-assign]

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=101),
            effective_user=types.SimpleNamespace(id=42),
        )
        context = types.SimpleNamespace(args=[])

        await handlers._show_mode_menu(update, context, "agent")

        visibility = captured["menu_visibility"]
        assert visibility is not None
        assert visibility.allows("status") is True
        assert visibility.allows("doctor") is False
        assert visibility.allows("promote_skills") is False

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_cmd_newpath_forum_uses_owner_scope_and_replies_in_ui_topic() -> None:
    create_calls: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []
    ui_key = TelegramUiKey.from_parts(-100777000111, 101)
    route = types.SimpleNamespace(
        owner_chat_id=42,
        reply_chat_id=ui_key.chat_id,
        message_thread_id=ui_key.message_thread_id,
    )

    async def _ensure_telegram_inbound_authorized(_update, _context, **_kwargs):
        return route

    async def _create_from_pending_tool(
        *,
        owner_chat_id: int,
        path: str,
        root=None,
        clear_dirs_mode: bool = False,
        bot=None,
        message_thread_id=None,
        ui_chat_id=None,
    ):
        create_calls.append(
            {
                "owner_chat_id": int(owner_chat_id),
                "path": str(path),
                "root": root,
                "clear_dirs_mode": bool(clear_dirs_mode),
                "bot": bot,
                "message_thread_id": message_thread_id,
                "ui_chat_id": ui_chat_id,
            }
        )
        return types.SimpleNamespace(id="s-forum", chat_id=int(owner_chat_id)), None

    async def _send_message(_context, *, chat_id: int, text: str, message_thread_id=None, **_kwargs):
        sent.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "message_thread_id": message_thread_id,
            }
        )

    bot_app = types.SimpleNamespace(
        ensure_telegram_inbound_authorized=_ensure_telegram_inbound_authorized,
        resolve_telegram_inbound_route=lambda _update: route,
        telegram_ui_key_from_route=(
            lambda _route, fallback_chat_id: TelegramUiKey.from_route(_route, fallback_chat_id=fallback_chat_id)
        ),
        ui_state=ChatUiState(),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(workdir="/fallback-root")),
        session_creation_service=types.SimpleNamespace(create_from_pending_tool=_create_from_pending_tool),
        _send_message=_send_message,
    )
    bot_app.ui_state.dirs_root[ui_key] = "/forum-root"
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=ui_key.chat_id))
    context = types.SimpleNamespace(args=["/forum-root/project"], bot=object())

    await handlers.cmd_newpath(update, context)

    assert create_calls == [
        {
            "owner_chat_id": 42,
            "path": "/forum-root/project",
            "root": "/forum-root",
            "clear_dirs_mode": False,
            "bot": context.bot,
            "message_thread_id": 101,
            "ui_chat_id": -100777000111,
        }
    ]
    assert sent == [
        {
            "chat_id": -100777000111,
            "text": "Сессия s-forum создана и выбрана.",
            "message_thread_id": 101,
        }
    ]


def test_command_registry_registers_sessions_search() -> None:
    app = _make_full_app()

    registry = build_command_registry(app)
    names = {str(item.get("name") or "") for item in registry}

    assert "sessions_search" in names
    entry = next(item for item in registry if str(item.get("name") or "") == "sessions_search")
    assert entry["menu"] is False
    assert not entry.get("admin_only")


@pytest.mark.asyncio
async def test_cmd_sessions_search_no_args_sends_usage() -> None:
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}, language="ru"),
            defaults=types.SimpleNamespace(language="ru"),
        ),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._owner_chat_id = lambda _update: 101  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=[])

    await handlers.cmd_sessions_search(update, context)

    assert len(sent) == 1
    assert "/sessions_search" in sent[0]


@pytest.mark.asyncio
async def test_cmd_sessions_search_returns_matching_sessions() -> None:
    sent: list[dict] = []

    async def _send_message(_context, *, text: str, **kwargs):
        sent.append({"text": str(text), **kwargs})

    s1 = types.SimpleNamespace(id="abc-123", name="my project", workdir="/srv/proj-a", tool=types.SimpleNamespace(name="claude"))
    s2 = types.SimpleNamespace(id="xyz-456", name=None, workdir="/home/user/work", tool=types.SimpleNamespace(name="codex"))
    s3 = types.SimpleNamespace(id="def-789", name="other", workdir="/srv/proj-b", tool=types.SimpleNamespace(name="gemini"))

    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}, language="ru"),
            defaults=types.SimpleNamespace(language="ru"),
        ),
        manager=types.SimpleNamespace(
            sessions_for_chat=lambda owner_chat_id: {"s1": s1, "s2": s2, "s3": s3},
        ),
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: True),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._owner_chat_id = lambda _update: 101  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["proj"])

    await handlers.cmd_sessions_search(update, context)

    assert len(sent) == 1
    text = sent[0]["text"]
    # s1 (workdir /srv/proj-a) and s3 (workdir /srv/proj-b) match "proj"; s2 does not
    # IDs are MarkdownV2-escaped: hyphens become \-
    assert "abc\\-123" in text
    assert "def\\-789" in text
    assert "xyz" not in text
    assert sent[0].get("parse_mode") == "MarkdownV2"


@pytest.mark.asyncio
async def test_cmd_sessions_search_not_found_message() -> None:
    sent: list[str] = []

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}, language="ru"),
            defaults=types.SimpleNamespace(language="ru"),
        ),
        manager=types.SimpleNamespace(
            sessions_for_chat=lambda owner_chat_id: {},
        ),
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: True),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._owner_chat_id = lambda _update: 101  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["nonexistent"])

    await handlers.cmd_sessions_search(update, context)

    assert len(sent) == 1
    assert "nonexistent" in sent[0]


@pytest.mark.asyncio
async def test_cmd_sessions_search_filters_by_name_and_id() -> None:
    sent: list[dict] = []

    async def _send_message(_context, *, text: str, **kwargs):
        sent.append({"text": str(text), **kwargs})

    s_by_id = types.SimpleNamespace(id="target-id", name="something", workdir="/home/x", tool=types.SimpleNamespace(name="codex"))
    s_by_name = types.SimpleNamespace(id="other-id", name="target name", workdir="/home/y", tool=types.SimpleNamespace(name="codex"))
    s_no_match = types.SimpleNamespace(id="unrelated", name="unrelated", workdir="/home/z", tool=types.SimpleNamespace(name="codex"))

    bot_app = types.SimpleNamespace(
        _send_message=_send_message,
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}, language="ru"),
            defaults=types.SimpleNamespace(language="ru"),
        ),
        manager=types.SimpleNamespace(
            sessions_for_chat=lambda _: {"a": s_by_id, "b": s_by_name, "c": s_no_match},
        ),
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: True),
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._owner_chat_id = lambda _update: 101  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 101}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=["target"])

    await handlers.cmd_sessions_search(update, context)

    assert len(sent) == 1
    text = sent[0]["text"]
    # IDs are MarkdownV2-escaped: hyphens become \-
    assert "target\\-id" in text
    assert "other\\-id" in text
    assert "unrelated" not in text


@pytest.mark.asyncio
async def test_cmd_limits_uses_owner_scope_and_service_result() -> None:
    sent: list[dict[str, object]] = []
    route = types.SimpleNamespace(
        owner_chat_id=42,
        reply_chat_id=-100777000111,
        message_thread_id=101,
        session=types.SimpleNamespace(workdir="/current/project"),
        reply_kwargs=lambda: {"chat_id": -100777000111, "message_thread_id": 101},
    )
    sessions = {
        "s1": types.SimpleNamespace(id="s1"),
        "s2": types.SimpleNamespace(id="s2"),
    }

    async def _ensure_telegram_inbound_authorized(_update, _context, **_kwargs):
        return route

    async def _send_message(_context, *, text: str, chat_id: int, message_thread_id=None, **_kwargs):
        sent.append(
            {
                "text": str(text),
                "chat_id": int(chat_id),
                "message_thread_id": message_thread_id,
            }
        )

    service = types.SimpleNamespace(
        SUPPORTED_CLI_NAMES=("claude", "codex", "gemini", "grok", "qwen"),
        describe_for_sessions=AsyncMock(return_value="LIMITS REPORT"),
    )
    bot_app = types.SimpleNamespace(
        ensure_telegram_inbound_authorized=_ensure_telegram_inbound_authorized,
        manager=types.SimpleNamespace(sessions_for_chat=lambda owner_chat_id: sessions if int(owner_chat_id) == 42 else {}),
        config=types.SimpleNamespace(
            tools={
                "claude": types.SimpleNamespace(enabled=True),
                "codex": types.SimpleNamespace(enabled=True),
                "gemini": types.SimpleNamespace(enabled=True),
                "grok": types.SimpleNamespace(enabled=True),
                "qwen": types.SimpleNamespace(enabled=False),
                "backup": types.SimpleNamespace(enabled=True),
            }
        ),
        cli_limits_service=service,
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: True),
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=-100777000111))

    await handlers.cmd_limits(update, types.SimpleNamespace())

    service.describe_for_sessions.assert_awaited_once()
    described_sessions = service.describe_for_sessions.await_args.args[0]
    assert list(described_sessions) == [sessions["s1"], sessions["s2"]]
    assert service.describe_for_sessions.await_args.kwargs["available_clis"] == ["claude", "codex", "gemini", "grok"]
    assert service.describe_for_sessions.await_args.kwargs["preferred_workdir"] == "/current/project"
    assert sent == [
        {
            "text": "LIMITS REPORT",
            "chat_id": -100777000111,
            "message_thread_id": 101,
        }
    ]


@pytest.mark.asyncio
async def test_cmd_limits_filters_sessions_for_non_admin_user() -> None:
    sent: list[dict[str, object]] = []
    route = types.SimpleNamespace(
        owner_chat_id=42,
        reply_chat_id=4200,
        message_thread_id=None,
        session=types.SimpleNamespace(id="allowed", workdir="/allowed/project"),
        reply_kwargs=lambda: {"chat_id": 4200},
    )
    allowed = types.SimpleNamespace(id="allowed", workdir="/allowed/project")
    hidden = types.SimpleNamespace(id="hidden", workdir="/hidden/project")
    sessions = {"allowed": allowed, "hidden": hidden}

    async def _ensure_telegram_inbound_authorized(_update, _context, **_kwargs):
        return route

    async def _send_message(_context, *, text: str, chat_id: int, **_kwargs):
        sent.append({"text": str(text), "chat_id": int(chat_id)})

    service = types.SimpleNamespace(
        SUPPORTED_CLI_NAMES=("codex",),
        describe_for_sessions=AsyncMock(return_value="FILTERED LIMITS"),
    )
    bot_app = types.SimpleNamespace(
        ensure_telegram_inbound_authorized=_ensure_telegram_inbound_authorized,
        manager=types.SimpleNamespace(sessions_for_chat=lambda owner_chat_id: sessions if int(owner_chat_id) == 42 else {}),
        config=types.SimpleNamespace(tools={"codex": types.SimpleNamespace(enabled=True)}),
        cli_limits_service=service,
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: False),
        is_session_allowed_for_chat=lambda chat_id, session: getattr(session, "id", "") == "allowed",
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=4200))

    await handlers.cmd_limits(update, types.SimpleNamespace())

    service.describe_for_sessions.assert_awaited_once()
    assert list(service.describe_for_sessions.await_args.args[0]) == [allowed]
    assert service.describe_for_sessions.await_args.kwargs["preferred_workdir"] == "/allowed/project"
    assert sent == [{"text": "FILTERED LIMITS", "chat_id": 4200}]


@pytest.mark.asyncio
async def test_cmd_rename_rejects_inaccessible_session_id_for_non_admin_user() -> None:
    sent: list[dict[str, object]] = []
    hidden = types.SimpleNamespace(id="hidden", name="old")
    sessions = {"hidden": hidden}
    route = types.SimpleNamespace(owner_chat_id=42, reply_chat_id=42, message_thread_id=None, session=None)

    async def _send_message(_context, *, text: str, **kwargs):
        sent.append({"text": str(text), **kwargs})

    bot_app = types.SimpleNamespace(
        resolve_telegram_inbound_route=lambda _update: route,
        manager=types.SimpleNamespace(
            sessions_for_chat=lambda owner_chat_id: sessions if int(owner_chat_id) == 42 else {},
            get=lambda owner_chat_id, session_id: sessions.get(session_id),
        ),
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: False),
        is_session_allowed_for_chat=lambda chat_id, session: False,
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 42}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=42))
    context = types.SimpleNamespace(args=["hidden", "New", "Name"])

    await handlers.cmd_rename(update, context)

    assert hidden.name == "old"
    assert sent == [{"text": "Сессия недоступна.", "chat_id": 42}]


@pytest.mark.asyncio
async def test_cmd_state_rejects_inaccessible_session_id_for_non_admin_user() -> None:
    sent: list[dict[str, object]] = []
    hidden = types.SimpleNamespace(id="hidden", tool=types.SimpleNamespace(name="codex"), workdir="/hidden/project")
    sessions = {"hidden": hidden}
    route = types.SimpleNamespace(owner_chat_id=42, reply_chat_id=42, message_thread_id=None, session=None)
    repo = MagicMock()

    async def _send_message(_context, *, text: str, **kwargs):
        sent.append({"text": str(text), **kwargs})

    bot_app = types.SimpleNamespace(
        resolve_telegram_inbound_route=lambda _update: route,
        resolve_telegram_scope_session=lambda **_kwargs: None,
        manager=types.SimpleNamespace(
            sessions_for_chat=lambda owner_chat_id: sessions if int(owner_chat_id) == 42 else {},
            get=lambda owner_chat_id, session_id: sessions.get(session_id),
        ),
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: False),
        is_session_allowed_for_chat=lambda chat_id, session: False,
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 42}  # type: ignore[method-assign]
    handlers._state_repository = lambda: repo  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=42))
    context = types.SimpleNamespace(args=["hidden"])

    await handlers.cmd_state(update, context)

    repo.get_state.assert_not_called()
    assert sent == [{"text": "Сессия недоступна.", "chat_id": 42}]


@pytest.mark.asyncio
async def test_cmd_state_tool_workdir_pair_rejects_inaccessible_project_for_non_admin_user() -> None:
    sent: list[dict[str, object]] = []
    route = types.SimpleNamespace(owner_chat_id=42, reply_chat_id=42, message_thread_id=None, session=None)
    repo = MagicMock()

    async def _send_message(_context, *, text: str, **kwargs):
        sent.append({"text": str(text), **kwargs})

    bot_app = types.SimpleNamespace(
        resolve_telegram_inbound_route=lambda _update: route,
        resolve_telegram_scope_session=lambda **_kwargs: None,
        user_projects=lambda chat_id: ["/allowed/project"] if int(chat_id) == 42 else [],
        access_policy_service=types.SimpleNamespace(is_admin=lambda chat_id: False),
        manager=types.SimpleNamespace(sessions_for_chat=lambda owner_chat_id: {}),
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {"chat_id": 42}  # type: ignore[method-assign]
    handlers._state_repository = lambda: repo  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=42))
    context = types.SimpleNamespace(args=["codex", "/hidden/project"])

    await handlers.cmd_state(update, context)

    repo.get_state.assert_not_called()
    assert sent == [
        {
            "text": "Состояние не найдено (используйте /state <session_id> или /state <tool> <workdir>)",
            "chat_id": 42,
        }
    ]


@pytest.mark.asyncio
async def test_cmd_state_reports_unavailable_state_when_state_path_invalid(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    sent: list[str] = []
    route = types.SimpleNamespace(
        owner_chat_id=101,
        reply_chat_id=101,
        message_thread_id=None,
        session=None,
    )

    async def _send_message(_context, *, text: str, **_kwargs):
        sent.append(str(text))

    bot_app = types.SimpleNamespace(
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(state_path=MagicMock().config.defaults.state_path)),
        resolve_telegram_inbound_route=lambda _update: route,
        resolve_telegram_scope_session=lambda **_kwargs: None,
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._reply_kwargs = lambda _update, _session=None: {}  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=101))
    context = types.SimpleNamespace(args=[])

    await handlers.cmd_state(update, context)

    assert sent == ["Состояние недоступно: state_path не настроен."]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_state_pick_reports_unavailable_state_when_state_path_invalid(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    sent: list[str] = []
    ui_key = TelegramUiKey.from_parts(101, None)

    async def _edit_msg(_context, _query, text: str, **_kwargs):
        sent.append(str(text))

    bot_app = types.SimpleNamespace(
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(state_path=MagicMock().config.defaults.state_path)),
        telegram_ui_key_from_query=lambda _query: ui_key,
        telegram_ui_key=lambda _chat_id: ui_key,
        ui_state=types.SimpleNamespace(state_menu={ui_key: ["entry-1"]}),
        resolve_telegram_callback_scope=lambda _query: (101, None, 101, None),
    )
    handler = CallbackHandler(bot_app)
    handler._edit_msg = _edit_msg  # type: ignore[method-assign]

    query = types.SimpleNamespace(
        message=types.SimpleNamespace(chat_id=101, message_id=1),
        from_user=types.SimpleNamespace(id=7),
    )

    result = await handler._cb_state_pick(
        data="state_pick:0",
        chat_id=101,
        query=query,
        context=types.SimpleNamespace(),
    )

    assert result is True
    assert sent == ["Состояние недоступно: state_path не настроен."]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cmd_new_forum_uses_owner_scope_and_registers_project() -> None:
    create_calls: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []
    route = types.SimpleNamespace(
        owner_chat_id=42,
        reply_chat_id=-100777000111,
        message_thread_id=101,
    )

    async def _create_session(
        *,
        owner_chat_id: int,
        tool: str,
        path: str,
        root=None,
        bot=None,
        ui_chat_id=None,
        register_project: bool = False,
    ):
        create_calls.append(
            {
                "owner_chat_id": int(owner_chat_id),
                "tool": str(tool),
                "path": str(path),
                "root": root,
                "bot": bot,
                "ui_chat_id": ui_chat_id,
                "register_project": bool(register_project),
            }
        )
        return types.SimpleNamespace(id="s-forum", chat_id=int(owner_chat_id)), None

    async def _send_message(_context, *, chat_id: int, text: str, message_thread_id=None, **_kwargs):
        sent.append(
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "message_thread_id": message_thread_id,
            }
        )

    bot_app = types.SimpleNamespace(
        resolve_telegram_inbound_route=lambda _update: route,
        session_creation_service=types.SimpleNamespace(create_session=_create_session),
        _send_message=_send_message,
    )
    handlers = BotHandlers(bot_app)
    handlers._ensure_allowed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handlers._require_admin = AsyncMock(return_value=True)  # type: ignore[method-assign]

    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=route.reply_chat_id))
    context = types.SimpleNamespace(args=["dummy", "/forum-root/project"], bot=object())

    await handlers.cmd_new(update, context)

    assert create_calls == [
        {
            "owner_chat_id": 42,
            "tool": "dummy",
            "path": "/forum-root/project",
            "root": None,
            "bot": context.bot,
            "ui_chat_id": -100777000111,
            "register_project": True,
        }
    ]
    assert sent == [
        {
            "chat_id": -100777000111,
            "text": "Сессия s-forum создана и выбрана.",
            "message_thread_id": 101,
        }
    ]
