import asyncio
import sqlite3
import threading
import types
from pathlib import Path
from typing import Any

import yaml

from bot import BotApp, TelegramInboundRoute
from app.services.run_artifact_store import RunArtifactStore
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.admin.chat_memory import ChatMemory, ChatPendingStore
from modes.admin.config_store import AdminConfigStore
from modes.admin.executor import AdminExecutionResult
from modes.admin.mode import AdminMode
from modes.admin.monitor import AdminMonitorSnapshot, AdminServerSnapshot
from modes.admin.runner_service import (
    AdminExecutorNotifierStepResult,
    AdminMonitorAnalyzerStepResult,
    AdminPipelineStepResult,
)
from modes.admin.state_store import AdminStateStore
from session import session_runtime_uid
from tg.callbacks import CallbackHandler
from tg.command_registry import build_command_registry


class _FakeMessage:
    def __init__(self, chat_id: int = 1, message_id: int = 10) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


class _RecordingModeMessaging:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_text(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.events.append(
            {
                "method": "send_text",
                "chat_id": int(chat_id),
                "text": str(text or ""),
                "kwargs": dict(kwargs or {}),
            }
        )

    async def send_plain_text(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.events.append(
            {
                "method": "send_plain_text",
                "chat_id": int(chat_id),
                "text": str(text or ""),
                "kwargs": dict(kwargs or {}),
            }
        )

    async def send_or_edit(self, **kwargs: Any) -> None:
        payload = dict(kwargs or {})
        payload["method"] = "send_or_edit"
        payload["text"] = str(payload.get("text") or "")
        self.events.append(payload)


class _FakeAdminRunnerRuntime:
    capabilities = frozenset({"run_admin_pipeline"})

    def __init__(self, *, ready: bool = True, block_run: bool = False) -> None:
        self.ready = bool(ready)
        self.block_run = bool(block_run)
        self.notifier_ensured = False
        self.run_calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def ensure_notifier(self, *, state_store):  # type: ignore[no-untyped-def]
        _ = state_store
        self.notifier_ensured = True
        return object()

    def is_pipeline_ready(self) -> bool:
        return bool(self.ready and self.notifier_ensured)

    async def run_pipeline_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.run_calls.append(dict(kwargs))
        self.started.set()
        if self.block_run:
            await self.release.wait()
        return None


_RUNNER_WAIT_TIMEOUT_S = 2.0


def _build_app(tmp_path) -> BotApp:
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
            openai_api_key="k",
            openai_model="m",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app._test_selected_session = None

    def _current(chat_id: int):
        selected = getattr(app, "_test_selected_session", None)
        if selected is not None:
            return selected
        sessions = list(app.manager.sessions_for_chat(int(chat_id)).values())
        return sessions[-1] if sessions else None

    def _set_active(chat_id: int, session_id: str) -> bool:
        session = app.manager.get(int(chat_id), str(session_id))
        if session is None:
            return False
        app._test_selected_session = session
        return True

    def _active(chat_id: int):
        return _current(int(chat_id))

    def _resolve_scope_session(*, reply_chat_id: int, message_thread_id=None, owner_chat_id=None):
        _ = message_thread_id
        target_chat_id = owner_chat_id if owner_chat_id is not None else reply_chat_id
        return _current(int(target_chat_id))

    def _resolve_inbound_route(update):
        chat_id = int(getattr(getattr(update, "effective_chat", None), "id", 0) or 0)
        session = _current(chat_id)
        session_uid = None
        if session is not None:
            session_uid = str(
                getattr(getattr(session, "conversation_scope", None), "session_uid", "")
                or f"{chat_id}:{getattr(session, 'id', '')}"
            )
        return TelegramInboundRoute(
            owner_chat_id=chat_id,
            reply_chat_id=chat_id,
            message_thread_id=None,
            session_uid=session_uid,
            session=session,
        )

    def _resolve_callback_scope(query):
        chat_id = int(getattr(getattr(query, "message", None), "chat_id", 0) or 0)
        return chat_id, None, chat_id, _current(chat_id)

    app.manager.active = _active  # type: ignore[attr-defined]
    app.manager.set_active = _set_active  # type: ignore[attr-defined]
    app.resolve_telegram_scope_session = _resolve_scope_session  # type: ignore[method-assign]
    app.resolve_telegram_inbound_route = _resolve_inbound_route  # type: ignore[method-assign]
    app.resolve_telegram_callback_scope = _resolve_callback_scope  # type: ignore[method-assign]
    mode = app.mode_registry.get("admin")
    if mode is not None:
        mode._resolve_runner_service = lambda: None
    return app


def _install_send_recorder(app: BotApp):
    sent = []

    async def _send_message(_ctx, *, chat_id: int, text: str, reply_markup=None, **kwargs):
        sent.append((chat_id, text, reply_markup, kwargs))
        return None

    app._send_message = _send_message
    return sent


def _write_admin_config(session, payload: dict) -> Path:
    config_path = Path(session.workdir) / ".cli-proxy" / ".admin" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return config_path


def _admin_exec_config() -> dict:
    return {
        "admin": {
            "allowlist": {
                "local": {
                    "safe_action": {"argv": ["echo", "SAFE_OK"], "timeout_sec": 5},
                    "check_action": {"argv": ["echo", "CHECK_OK"], "timeout_sec": 5},
                },
                "ssh": {},
            },
            "actions": {
                "local": {
                    "safe_action": {"argv": ["echo", "SAFE_OK"], "timeout_sec": 5},
                    "check_action": {"argv": ["echo", "CHECK_OK"], "timeout_sec": 5},
                },
                "ssh": {},
            },
            "servers": {
                "srv-1": {
                    "target": "local",
                    "check_action_id": "check_action",
                }
            },
            "monitor": {
                "enabled": True,
                "interval_sec": 30,
                "servers": [],
            },
        }
    }


def _register_pending_skill_install(app: BotApp, session: Any, *, skill_id: str, task_hash: str) -> Any:
    return app.mode_skill_runtime.policy_service.register_pending_install(
        session=session,
        mode_id="agent",
        phase="execute",
        task_hash=task_hash,
        skill_id=skill_id,
        source="ref:owner-repo-skill",
        acquisition_source="ref:owner-repo-skill",
        ref=f"owner/repo@{skill_id}",
        install_target=app.mode_skill_runtime.policy_service.resolve_install_target(session=session),
        requester={
            "actor_chat_id": "1",
            "session_uid": str(session.conversation_scope.session_uid),
        },
        origin_payload={
            "candidate": {
                "skill_id": skill_id,
                "title": skill_id,
                "description": f"Skill {skill_id}",
                "source": "ref:owner-repo-skill",
                "acquisition_source": "ref:owner-repo-skill",
                "ref": f"owner/repo@{skill_id}",
            },
            "acquired_skill": {
                "skill_id": skill_id,
                "title": skill_id,
                "description": f"Skill {skill_id}",
                "content": f"# {skill_id}\n\nИспользуй этот skill.",
                "source": "ref:owner-repo-skill",
                "ref": f"owner/repo@{skill_id}",
                "tags": ["test"],
                "metadata": {"created_by": "test"},
            },
        },
    )


def test_admin_monitor_server_readiness_requires_poll_spec() -> None:
    malformed = {"admin": {"monitor": {"servers": [{"server_id": "local", "transport": "local"}]}}}
    valid = {"admin": {"monitor": {"servers": [{"server_id": "local", "target": "local", "action_id": "scan_local"}]}}}

    assert AdminMode._has_monitor_servers(malformed) is False
    assert AdminMode._has_monitor_servers(valid) is True


def test_admin_snapshot_trace_keeps_total_server_count_when_rows_are_truncated() -> None:
    servers = tuple(
        AdminServerSnapshot(
            server_id=f"srv-{idx}",
            target="local",
            action_id="scan_local",
            ok=True,
            timed_out=False,
            returncode=0,
            duration_ms=10,
            metrics={},
            error=None,
            collected_at_ts=1710000000.0 + idx,
        )
        for idx in range(6)
    )
    snapshot = AdminMonitorSnapshot(
        created_at_ts=1710000000.0,
        total_servers=6,
        ok_servers=6,
        failed_servers=0,
        servers=servers,
    )

    trace = AdminMode._build_snapshot_trace(snapshot, target_transport="local")

    assert len(trace["snapshot_ids"]) == 6
    assert len(trace["last_monitor_snapshot"]["servers"]) == 5
    assert trace["last_monitor_snapshot"]["server_count"] == 6
    assert trace["snapshot_fidelity"]["server_count"] == 6


def test_admin_snapshot_trace_bounds_long_snapshot_entry_ids() -> None:
    long_name = "scan:docker_container:" + ("very-long-container-name-" * 8)
    snapshot = AdminMonitorSnapshot(
        created_at_ts=1710000000.0,
        total_servers=1,
        ok_servers=1,
        failed_servers=0,
        servers=(
            AdminServerSnapshot(
                server_id=long_name,
                target="ssh",
                action_id="diag_docker_container_" + ("very_long_action_" * 8),
                ok=True,
                timed_out=False,
                returncode=0,
                duration_ms=10,
                metrics={},
                error=None,
                collected_at_ts=1710000000.0,
            ),
        ),
    )

    trace = AdminMode._build_snapshot_trace(snapshot, target_transport="ssh")

    assert len(trace["snapshot_ids"]) == 1
    assert len(trace["snapshot_ids"][0]) <= 128
    assert trace["snapshot_fidelity"]["snapshot_ids"] == trace["snapshot_ids"]


def test_admin_servers_inline_keyboard_is_paginated(tmp_path) -> None:
    class _ServerSummary:
        def __init__(self, idx: int) -> None:
            self.server_id = f"srv-{idx:02d}"
            self.label = self.server_id

        def status(self) -> str:
            return "ok"

        def to_dict(self) -> dict[str, Any]:
            return {
                "server_id": self.server_id,
                "label": self.label,
                "transport": "local",
                "status": "ok",
            }

    class _AutonomyService:
        def __init__(self) -> None:
            self.items = [_ServerSummary(idx) for idx in range(10)]

        def list_servers(self):  # type: ignore[no-untyped-def]
            return list(self.items)

        def global_summary(self) -> dict[str, Any]:
            return {"server_count": len(self.items), "statuses": {"ok": len(self.items)}}

        def get_server_summary(self, server_id: str):  # type: ignore[no-untyped-def]
            for item in self.items:
                if item.server_id == server_id:
                    return item
            return None

    async def _run() -> None:
        mode = AdminMode()
        service = _AutonomyService()
        session = types.SimpleNamespace(id="s-admin", workdir=str(tmp_path))
        recorder = _RecordingModeMessaging()
        mode._messaging = lambda **_kwargs: recorder
        mode._autonomy_service = lambda **_kwargs: service

        await mode._cb_servers(
            bot_app=types.SimpleNamespace(),
            session=session,
            chat_id=1,
            context=None,
            query=None,
            page=0,
        )
        first_keyboard = recorder.events[-1]["reply_markup"].inline_keyboard
        first_texts = [button.text for row in first_keyboard for button in row]
        first_callbacks = [button.callback_data for row in first_keyboard for button in row]

        assert "🟢 srv-00" in first_texts
        assert "🟢 srv-07" in first_texts
        assert "🟢 srv-08" not in first_texts
        assert "1/2" in first_texts
        assert "▶️" in first_texts
        assert all("p=1" not in str(callback or "") for callback in first_callbacks if "srv" in str(callback or ""))

        await mode._cb_servers(
            bot_app=types.SimpleNamespace(),
            session=session,
            chat_id=1,
            context=None,
            query=None,
            page=1,
        )
        second_keyboard = recorder.events[-1]["reply_markup"].inline_keyboard
        second_texts = [button.text for row in second_keyboard for button in row]
        second_callbacks = [button.callback_data for row in second_keyboard for button in row]

        assert "🟢 srv-00" not in second_texts
        assert "🟢 srv-08" in second_texts
        assert "🟢 srv-09" in second_texts
        assert "◀️" in second_texts
        assert "2/2" in second_texts
        assert getattr(session, "_admin_server_tokens")["srv-08"] == "srv-08"
        assert any("ma:admin:srv" in str(callback or "") and "p=1" in str(callback or "") for callback in second_callbacks)

        await mode._cb_server_detail(
            bot_app=types.SimpleNamespace(),
            session=session,
            chat_id=1,
            context=None,
            query=None,
            server_token="srv-08",
            page=1,
        )
        detail_keyboard = recorder.events[-1]["reply_markup"].inline_keyboard
        detail_callbacks = [button.callback_data for row in detail_keyboard for button in row]

        assert any("ma:admin:servers" in str(callback or "") and "p=1" in str(callback or "") for callback in detail_callbacks)

    asyncio.run(_run())


def test_admin_enable_with_malformed_monitor_servers_does_not_start_runner(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)
            _write_admin_config(
                session,
                {
                    "admin": {
                        "monitor": {
                            "enabled": True,
                            "interval_sec": 30,
                            "servers": [{"server_id": "local", "transport": "local"}],
                        },
                    }
                },
            )
            mode = app.mode_registry.get("admin")
            assert mode is not None
            runner_cls = mode._ensure_runner_started.__func__.__globals__["AdminModeRunnerService"]
            mode._resolve_runner_service = lambda: runner_cls(app.config)
            mode._upsert_session_status(
                bot_app=app,
                session=session,
                chat_id=1,
                enabled=True,
                watch_enabled=True,
                updated_by=None,
            )

            await mode._ensure_runner_started(
                bot_app=app,
                session=session,
                chat_id=1,
                messaging=object(),
                context=object(),
            )

            assert app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="admin") == []
            status = dict(getattr(session, "admin_runtime_status") or {})
            assert status["pipeline_status"] == "idle"
            assert status["analyzer_message"] == "No valid monitor servers configured."
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_mode_loaded_and_command_registered_in_menu(tmp_path) -> None:
    app = _build_app(tmp_path)
    try:
        assert app.mode_registry.get("admin") is not None
        registry = build_command_registry(app)
        admin_entries = [item for item in registry if str(item.get("name") or "") == "admin"]
        assert len(admin_entries) == 1
        assert bool(admin_entries[0].get("menu")) is True

        telegram_menu = [cmd.command for cmd in app._bot_commands(include_admin=True)]
        assert "admin" in telegram_menu
        assert [name for name in telegram_menu if str(name).startswith("admin")] == ["admin"]
    finally:
        app.shutdown_html_process_pool()


def test_admin_command_not_registered_when_mode_not_loaded(tmp_path) -> None:
    app = _build_app(tmp_path)
    try:
        app.mode_registry.modes.pop("admin", None)
        names = {str(item.get("name") or "") for item in build_command_registry(app)}
        assert "admin" not in names
    finally:
        app.shutdown_html_process_pool()


def test_admin_registry_exposes_only_root_admin_command(tmp_path) -> None:
    app = _build_app(tmp_path)
    try:
        registry = build_command_registry(app)
        command_names = [str(item.get("name") or "") for item in registry]
        admin_like = sorted(name for name in command_names if name.startswith("admin"))
        assert admin_like == ["admin"]
    finally:
        app.shutdown_html_process_pool()


def test_admin_requires_selected_session_before_dispatch(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=1))
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["help"]), "admin")

            assert sent
            assert "Сначала откройте нужный topic или выберите сессию через /sessions." in str(sent[-1][1] or "")
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_help_and_status_subcommands_use_ui_and_md2(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None
            menu_calls = []
            status_calls = []
            old_build_menu = mode.build_menu
            old_build_status_text = mode._build_status_text

            def _build_menu_spy(*args, **kwargs):
                menu_calls.append((args, dict(kwargs)))
                return old_build_menu(*args, **kwargs)

            def _build_status_text_spy(*args, **kwargs):
                status_calls.append((args, dict(kwargs)))
                return old_build_status_text(*args, **kwargs)

            mode.build_menu = _build_menu_spy
            mode._build_status_text = _build_status_text_spy

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=1))

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["help"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["status"]), "admin")

            assert menu_calls
            assert status_calls
            assert sent[0][3].get("md2") is True
            assert sent[1][3].get("md2") is True
            assert "Admin статус" in str(sent[1][1] or "")
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_status_callback_reads_config_through_admin_config_service(tmp_path) -> None:
    class _RecordingAdminConfigService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def load_config(self, session_uid: str, *, effective: bool = True) -> dict:
            self.calls.append((session_uid, effective))
            return {
                "admin": {
                    "monitor": {
                        "enabled": True,
                        "interval_sec": 30,
                        "servers": [
                            {"id": "local", "target": "local", "action_id": "scan_local"},
                        ],
                    },
                    "runtime": {"scan_status": "ready"},
                    "generated": {"environment": {"services": {}}},
                }
            }

    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)
            recorder = _RecordingAdminConfigService()
            app.admin_config_service = recorder
            edits = []

            async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **kwargs):
                edits.append((chat_id, message_id, text, reply_markup, kwargs))
                return None

            app._edit_message = _edit_message
            handler = CallbackHandler(app)

            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:status")),
                context=object(),
            )

            assert (session_runtime_uid(session), False) in recorder.calls
            assert (session_runtime_uid(session), True) in recorder.calls
            assert edits
            assert "Admin статус" in str(edits[-1][2] or "")
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_enable_disable_updates_state_and_preserves_active_mode(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)
            session.modes.active_mode = "manager"

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            store = AdminStateStore(app.config.defaults.state_path)

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            enabled_state = store.get_session_state(session.id, chat_id=1)
            assert enabled_state is not None
            assert bool(enabled_state.get("enabled")) is True
            assert session.modes.active_mode == "manager"

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["disable"]), "admin")
            disabled_state = store.get_session_state(session.id, chat_id=1)
            assert disabled_state is not None
            assert bool(disabled_state.get("enabled")) is False
            assert session.modes.active_mode == "manager"

            assert "Admin включен." in str(sent[0][1] or "")
            assert "Admin выключен." in str(sent[-1][1] or "")
            assert sent[0][3].get("md2") is True
            assert sent[-1][3].get("md2") is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_state_isolated_between_sequential_sessions(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir_a = tmp_path / "a"
            workdir_b = tmp_path / "b"
            workdir_a.mkdir()
            workdir_b.mkdir()
            session_a = app.manager.create(1, "dummy", str(workdir_a))
            session_b = app.manager.create(1, "dummy", str(workdir_b))
            store = AdminStateStore(app.config.defaults.state_path)

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            app.manager.set_active(1, session_a.id)
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")

            app.manager.set_active(1, session_b.id)
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["disable"]), "admin")

            state_a = store.get_session_state(session_a.id, chat_id=1)
            state_b = store.get_session_state(session_b.id, chat_id=1)
            assert state_a is not None and state_b is not None
            assert bool(state_a.get("enabled")) is True
            assert bool(state_b.get("enabled")) is False

            app.manager.set_active(1, session_a.id)
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["disable"]), "admin")
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_runner_stops_on_admin_disable(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)
            mode = app.mode_registry.get("admin")
            assert mode is not None

            fake_runner = _FakeAdminRunnerRuntime(ready=True, block_run=True)
            mode._resolve_runner_service = lambda: fake_runner

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await asyncio.wait_for(fake_runner.started.wait(), timeout=_RUNNER_WAIT_TIMEOUT_S)

            session_uid = session_runtime_uid(session)
            running = app.mode_tasks.list(session_uid=session_uid, mode_id="admin")
            assert "run_admin_pipeline_loop" in running
            assert fake_runner.run_calls

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["disable"]), "admin")
            await asyncio.sleep(0)
            assert app.mode_tasks.list(session_uid=session_uid, mode_id="admin") == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_runner_continues_after_ui_switch(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir_a = tmp_path / "switch-a"
            workdir_b = tmp_path / "switch-b"
            workdir_a.mkdir()
            workdir_b.mkdir()
            session_a = app.manager.create(1, "dummy", str(workdir_a))
            session_b = app.manager.create(1, "dummy", str(workdir_b))
            app.manager.set_active(1, session_a.id)

            mode = app.mode_registry.get("admin")
            assert mode is not None
            fake_runner = _FakeAdminRunnerRuntime(ready=True, block_run=False)
            mode._resolve_runner_service = lambda: fake_runner

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await asyncio.wait_for(fake_runner.started.wait(), timeout=_RUNNER_WAIT_TIMEOUT_S)
            session_uid = session_runtime_uid(session_a)
            assert app.mode_tasks.list(session_uid=session_uid, mode_id="admin")
            assert fake_runner.run_calls

            app.manager.set_active(1, session_b.id)
            await asyncio.sleep(0.2)

            running = app.mode_tasks.list(session_uid=session_uid, mode_id="admin")
            assert "run_admin_pipeline_loop" in running

            app.manager.set_active(1, session_a.id)
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["disable"]), "admin")
            await asyncio.sleep(0)
            assert app.mode_tasks.list(session_uid=session_uid, mode_id="admin") == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_callbacks_blocked_by_busy_three_signals_and_recovery(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "busy"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            app.manager.set_active(1, session.id)
            store = AdminStateStore(app.config.defaults.state_path)

            sent = _install_send_recorder(app)
            handler = CallbackHandler(app)
            busy_msg = "Сессия занята. Переключение/выключение режима доступно только когда сессия свободна."

            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:enable")),
                context=object(),
            )
            assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is True  # type: ignore[union-attr]

            before_busy = len(sent)
            session.busy = True
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:disable")),
                context=object(),
            )
            assert len(sent) == before_busy + 1
            assert busy_msg in str(sent[-1][1] or "")
            assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is True  # type: ignore[union-attr]

            session.busy = False
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:disable")),
                context=object(),
            )
            assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is False  # type: ignore[union-attr]

            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:enable")),
                context=object(),
            )
            await session.run_lock.acquire()
            try:
                before_lock = len(sent)
                await handler.handle_callback(
                    types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:disable")),
                    context=object(),
                )
                assert len(sent) == before_lock + 1
                assert busy_msg in str(sent[-1][1] or "")
                assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is True  # type: ignore[union-attr]
            finally:
                session.run_lock.release()

            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:disable")),
                context=object(),
            )
            assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is False  # type: ignore[union-attr]

            tick_state = {"active": False}
            session.is_active_by_tick = lambda: bool(tick_state["active"])
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:enable")),
                context=object(),
            )
            tick_state["active"] = True
            before_tick = len(sent)
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:disable")),
                context=object(),
            )
            assert len(sent) == before_tick + 1
            assert busy_msg in str(sent[-1][1] or "")
            assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is True  # type: ignore[union-attr]

            tick_state["active"] = False
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:disable")),
                context=object(),
            )
            assert bool(store.get_session_state(session.id, chat_id=1).get("enabled")) is False  # type: ignore[union-attr]
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_rescan_callback_blocked_by_all_busy_signals_and_recovers(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "rescan"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            app.manager.set_active(1, session.id)
            session.modes.active_mode = "admin"
            tick_state = {"active": False}
            session.is_active_by_tick = lambda: bool(tick_state["active"])

            _install_send_recorder(app)
            edits = []

            async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **kwargs):
                edits.append((chat_id, message_id, text, reply_markup, kwargs))
                return None

            app._edit_message = _edit_message
            handler = CallbackHandler(app)
            mode = app.mode_registry.get("admin")
            assert mode is not None

            starts: list[str] = []
            notes: list[str] = []

            async def _fake_start_environment_scan(**_kwargs):
                starts.append("started")
                return True

            async def _fake_rerender_menu(**kwargs):
                notes.append(str(kwargs.get("note") or ""))
                return None

            mode._start_environment_scan = _fake_start_environment_scan
            mode._rerender_menu = _fake_rerender_menu

            query = types.SimpleNamespace(callback_query=_FakeQuery("ma:admin:rescan"))
            busy_msg = "Сессия занята. Пересканирование окружения доступно только когда сессия свободна."

            session.busy = True
            await handler.handle_callback(query, context=object())
            assert starts == []
            assert busy_msg in str(edits[-1][2] or "")

            session.busy = False
            await handler.handle_callback(query, context=object())
            assert starts == ["started"]
            assert notes[-1] == "Rescan окружения запущен."

            await session.run_lock.acquire()
            try:
                await handler.handle_callback(query, context=object())
                assert starts == ["started"]
                assert busy_msg in str(edits[-1][2] or "")
            finally:
                session.run_lock.release()

            await handler.handle_callback(query, context=object())
            assert starts == ["started", "started"]
            assert notes[-1] == "Rescan окружения запущен."

            tick_state["active"] = True
            await handler.handle_callback(query, context=object())
            assert starts == ["started", "started"]
            assert busy_msg in str(edits[-1][2] or "")

            tick_state["active"] = False
            await handler.handle_callback(query, context=object())
            assert starts == ["started", "started", "started"]
            assert notes[-1] == "Rescan окружения запущен."
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_session_close_cancels_runner_tasks(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "close"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            app.manager.set_active(1, session.id)
            mode = app.mode_registry.get("admin")
            assert mode is not None
            fake_runner = _FakeAdminRunnerRuntime(ready=True, block_run=True)
            mode._resolve_runner_service = lambda: fake_runner

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await asyncio.wait_for(fake_runner.started.wait(), timeout=_RUNNER_WAIT_TIMEOUT_S)
            session_uid = session_runtime_uid(session)
            assert app.mode_tasks.list(session_uid=session_uid, mode_id="admin")

            handler = CallbackHandler(app)
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery(f"sess_close:{session.id}")),
                context=object(),
            )

            for _ in range(20):
                if not app.mode_tasks.list(session_uid=session_uid, mode_id="admin"):
                    break
                await asyncio.sleep(0.05)

            assert app.mode_tasks.list(session_uid=session_uid, mode_id="admin") == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_enable_bootstraps_local_config_and_secrets(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "admin_wd"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            app.manager.set_active(1, session.id)

            config_path = Path(session.workdir) / ".cli-proxy" / ".admin" / "config.yaml"
            secrets_path = Path(session.workdir) / ".cli-proxy" / ".admin" / "secrets.env"
            if config_path.exists():
                config_path.unlink()
            if secrets_path.exists():
                secrets_path.unlink()

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")

            assert config_path.exists()
            assert secrets_path.exists()
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            assert isinstance(cfg, dict)
            assert isinstance(cfg.get("admin"), dict)
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_enable_persists_pinned_cli_to_runtime_config(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "admin_cli_wd"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            session.cli_mode = "codex"
            app.manager.set_active(1, session.id)

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")

            config_path = Path(session.workdir) / ".cli-proxy" / ".admin" / "config.yaml"
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

            assert cfg["admin"]["runtime"]["pinned_cli"] == {"name": "codex"}
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_enable_starts_initial_environment_scan_and_merges_generated_config(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "admin_scan_wd"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            session.cli_mode = "codex"
            app.manager.set_active(1, session.id)

            config_path = _write_admin_config(
                session,
                {
                    "admin": {
                        "monitor": {
                            "enabled": True,
                            "interval_sec": 30,
                            "servers": [],
                        },
                        "runtime": {
                            "pinned_cli": {},
                        },
                        "generated": {
                            "manual": {
                                "keep": True,
                            },
                        },
                    },
                },
            )

            mode = app.mode_registry.get("admin")
            assert mode is not None
            mode._resolve_runner_service = lambda: None

            started = threading.Event()
            release = threading.Event()
            seen = {}
            services_payload = {
                "python": {
                    "check_action_id": "scan_python",
                    "transport": "local",
                },
            }

            class _FakeScanner:
                def __init__(self, *, pinned_cli):
                    seen["pinned_cli"] = dict(pinned_cli)

                def scan(self):
                    started.set()
                    if not release.wait(timeout=1.0):
                        raise AssertionError("scanner was not released")
                    return {
                        "services": dict(services_payload),
                        "pinned_cli": dict(seen["pinned_cli"]),
                        "environment": {
                            "pinned_cli": dict(seen["pinned_cli"]),
                            "transport": "local",
                            "services": dict(services_payload),
                        },
                    }

            mode._build_environment_scanner = lambda *, pinned_cli: _FakeScanner(pinned_cli=pinned_cli)

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")

            assert await asyncio.to_thread(started.wait, 0.5) is True
            session_uid = session_runtime_uid(session)
            running = app.mode_tasks.list(session_uid=session_uid, mode_id="admin")
            assert "scan_admin_environment" in running
            assert seen["pinned_cli"] == {"name": "codex"}

            release.set()

            for _ in range(20):
                cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if cfg["admin"]["runtime"].get("last_scan_at"):
                    break
                await asyncio.sleep(0.05)

            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            assert cfg["admin"]["generated"]["manual"] == {"keep": True}
            assert cfg["admin"]["generated"]["environment"] == {
                "pinned_cli": {"name": "codex"},
                "transport": "local",
                "services": services_payload,
            }
            assert isinstance(cfg["admin"]["runtime"]["last_scan_at"], float)

            for _ in range(20):
                if "scan_admin_environment" not in app.mode_tasks.list(session_uid=session_uid, mode_id="admin"):
                    break
                await asyncio.sleep(0.05)

            assert "scan_admin_environment" not in app.mode_tasks.list(session_uid=session_uid, mode_id="admin")

            def _unexpected_scanner(*, pinned_cli):
                raise AssertionError(f"unexpected scanner build for pinned_cli={pinned_cli}")

            mode._build_environment_scanner = _unexpected_scanner
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            assert "scan_admin_environment" not in app.mode_tasks.list(session_uid=session_uid, mode_id="admin")
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_rescan_uses_single_ssh_monitor_as_scan_profile(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            workdir = tmp_path / "admin_ssh_rescan_wd"
            workdir.mkdir()
            session = app.manager.create(1, "dummy", str(workdir))
            session.cli_mode = "codex"
            app.manager.set_active(1, session.id)

            config_path = _write_admin_config(
                session,
                {
                    "admin": {
                        "monitor": {
                            "enabled": True,
                            "interval_sec": 30,
                            "servers": [
                                {
                                    "id": "mb_test",
                                    "target": "ssh",
                                    "transport": "ssh",
                                    "host": "83.69.203.41",
                                    "user": "la",
                                    "port": 37121,
                                    "password_env": "SSH_MB_TEST_PASSWORD",
                                    "action_id": "diag_host_health",
                                }
                            ],
                        },
                        "runtime": {
                            "pinned_cli": {"name": "codex"},
                            "last_scan_at": 1.0,
                            "scan_status": "ready",
                        },
                        "generated": {},
                    },
                },
            )

            mode = app.mode_registry.get("admin")
            assert mode is not None
            mode._resolve_runner_service = lambda: None

            seen = {}

            class _FakeScanner:
                def __init__(self, *, pinned_cli):
                    seen["pinned_cli"] = dict(pinned_cli)

                async def scan_async(self):
                    return {
                        "generated": {
                            "environment": {
                                "pinned_cli": dict(seen["pinned_cli"]),
                                "transport": str(seen["pinned_cli"].get("transport") or ""),
                                "services": {
                                    "systemd:nginx": {
                                        "category": "systemd",
                                        "transport": "ssh",
                                    },
                                },
                            },
                        },
                    }

            mode._build_environment_scanner = lambda *, pinned_cli, session_workdir="": _FakeScanner(pinned_cli=pinned_cli)

            started = await mode._start_environment_scan(
                bot_app=app,
                session=session,
                config_payload=yaml.safe_load(config_path.read_text(encoding="utf-8")),
                chat_id=1,
                context=None,
                force=True,
                initial=False,
            )
            assert started is True

            for _ in range(20):
                cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if cfg["admin"]["runtime"].get("last_scan_at") != 1.0:
                    break
                await asyncio.sleep(0.05)

            assert seen["pinned_cli"]["name"] == "codex"
            assert seen["pinned_cli"]["target"] == "ssh"
            assert seen["pinned_cli"]["transport"] == "ssh"
            assert seen["pinned_cli"]["host"] == "83.69.203.41"
            assert seen["pinned_cli"]["user"] == "la"
            assert seen["pinned_cli"]["port"] == 37121
            assert seen["pinned_cli"]["password_env"] == "SSH_MB_TEST_PASSWORD"

            effective = AdminConfigStore(str(workdir)).load_effective_config(validate=False)
            assert effective["admin"]["environment"]["transport"] == "ssh"
            assert "systemd:nginx" in effective["admin"]["environment"]["services"]
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_enable_applies_admin_schema_in_shared_db(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "state_wd"))
            app.manager.set_active(1, session.id)

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")

            db_path = Path(f"{app.config.defaults.state_path}.sqlite3")
            assert db_path.exists()
            expected_tables = {
                "admin_session_state",
                "admin_incidents",
                "admin_actions",
                "admin_alerts_state",
                "admin_acknowledgements",
                "admin_approved_overrides",
                "admin_digests",
            }
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'admin_%'"
                ).fetchall()
            found = {str(row[0]) for row in rows}
            assert expected_tables.issubset(found)
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_unknown_subcommand_rejected(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path))
            app.manager.set_active(1, session.id)

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["nope"]), "admin")

            assert sent
            assert "Неизвестная подкоманда" in str(sent[-1][1] or "")
            assert sent[-1][3].get("md2") is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_run_check_and_dry_run_flags_passed_to_executor(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "exec"))
            app.manager.set_active(1, session.id)
            _write_admin_config(session, _admin_exec_config())

            mode = app.mode_registry.get("admin")
            assert mode is not None
            captured = []

            async def _fake_execute(**kwargs):
                captured.append(kwargs)
                context = kwargs["context"]
                return AdminExecutionResult(
                    success=True,
                    text=(
                        f"EXECUTOR command={context.command} "
                        f"dry_run={context.dry_run} "
                        f"check_only={context.check_only}"
                    ),
                    returncode=0,
                )

            mode._executor.execute = _fake_execute

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["dry-run", "on"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["check", "srv-1"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["run", "safe_action", "srv-1"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["dry-run", "off"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["run", "safe_action", "srv-1"]), "admin")

            assert len(captured) == 3

            ctx_check = captured[0]["context"]
            assert ctx_check.command == "check"
            assert ctx_check.check_only is True
            assert ctx_check.dry_run is True
            assert ctx_check.target == "local"
            assert str(ctx_check.flags.get("server_id") or "") == "srv-1"

            ctx_run_dry = captured[1]["context"]
            assert ctx_run_dry.command == "run"
            assert ctx_run_dry.check_only is False
            assert ctx_run_dry.dry_run is True

            ctx_run_live = captured[2]["context"]
            assert ctx_run_live.command == "run"
            assert ctx_run_live.check_only is False
            assert ctx_run_live.dry_run is False

            assert any("Dry-run: on" in str(item[1] or "") for item in sent)
            assert any("Dry-run: off" in str(item[1] or "") for item in sent)
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_watch_loop_generates_run_artifacts_with_snapshot_ids_and_transport(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "watch-artifacts"))
            app.manager.set_active(1, session.id)
            _write_admin_config(
                session,
                {
                    "admin": {
                        "monitor": {
                            "enabled": True,
                            "interval_sec": 30,
                            "servers": [{"id": "srv-1", "target": "local", "action_id": "scan_local"}],
                        }
                    }
                },
            )

            mode = app.mode_registry.get("admin")
            assert mode is not None
            step_result = AdminPipelineStepResult(
                monitor_analyzer=AdminMonitorAnalyzerStepResult(
                    snapshot=AdminMonitorSnapshot(
                        created_at_ts=1710000000.0,
                        total_servers=1,
                        ok_servers=1,
                        failed_servers=0,
                        servers=(
                            AdminServerSnapshot(
                                server_id="srv-1",
                                target="local",
                                action_id="scan_local",
                                ok=True,
                                timed_out=False,
                                returncode=0,
                                duration_ms=15,
                                metrics={"cpu": 20},
                                error=None,
                                collected_at_ts=1710000000.25,
                            ),
                        ),
                    ),
                    decision={
                        "action": "notify_admin",
                        "confidence": "high",
                        "diagnosis": "all_good",
                        "reason": "stable",
                    },
                ),
                executor_notifier=AdminExecutorNotifierStepResult(
                    execution_result=AdminExecutionResult(
                        success=True,
                        text="notified",
                        returncode=0,
                        logged_action_id="analyzer:session:notify_admin:1",
                    ),
                    action_notification=None,
                    incident_notification=None,
                    target_transport="local",
                    native_transport_execution=False,
                    destructive_execution=False,
                    action_id="notify_admin",
                    server_id="srv-1",
                ),
            )

            class _FakeRunner:
                capabilities = frozenset({"run_admin_pipeline"})

                def __init__(self) -> None:
                    self.notifier_ensured = False
                    self.started = asyncio.Event()
                    self.run_calls: list[dict[str, Any]] = []

                def ensure_notifier(self, *, state_store):  # type: ignore[no-untyped-def]
                    _ = state_store
                    self.notifier_ensured = True
                    return object()

                def is_pipeline_ready(self) -> bool:
                    return bool(self.notifier_ensured)

                async def run_pipeline_once(self, **kwargs):  # type: ignore[no-untyped-def]
                    self.run_calls.append(dict(kwargs))
                    self.started.set()
                    return step_result

            fake_runner = _FakeRunner()
            mode._resolve_runner_service = lambda: fake_runner

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await asyncio.wait_for(fake_runner.started.wait(), timeout=_RUNNER_WAIT_TIMEOUT_S)

            store = RunArtifactStore(app.config)
            run = None
            state = {}
            for _ in range(20):
                run = store.latest_run(session=session, mode_id="admin")
                if run is None:
                    await asyncio.sleep(0.05)
                    continue
                state = store.load_state(run)
                if str((state.get("mode_context") or {}).get("snapshot_id") or "").strip():
                    break
                await asyncio.sleep(0.05)

            assert run is not None
            ctx = dict(state.get("mode_context") or {})
            checkpoints = store.load_checkpoints(run)
            assert state["phase"] == "complete"
            assert state["status"] == "completed"
            assert ctx["target_transport"] == "local"
            assert str(ctx["snapshot_id"]).startswith("snapshot:")
            assert ctx["snapshot_ids"] == ["srv-1:local:scan_local:1710000000250"]
            assert ctx["snapshot_fidelity"]["snapshot_id"] == ctx["snapshot_id"]
            assert ctx["snapshot_fidelity"]["snapshot_ids"] == ctx["snapshot_ids"]
            assert ctx["last_monitor_snapshot"]["server_count"] == 1
            assert len(list(checkpoints.get("items") or [])) >= 2

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["disable"]), "admin")
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_manual_check_run_artifacts_bypass_skill_selector_and_capture_transport(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "manual-artifacts"))
            app.manager.set_active(1, session.id)
            _write_admin_config(session, _admin_exec_config())
            setattr(session, "run_prompt", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run_prompt should not be used")))

            mode = app.mode_registry.get("admin")
            assert mode is not None

            async def _fake_execute(**kwargs):
                context = kwargs["context"]
                return AdminExecutionResult(
                    success=True,
                    text=f"{context.command}:{context.action_id}:{context.target}",
                    returncode=0,
                    logged_action_id="executor:manual:1",
                )

            mode._executor.execute = _fake_execute

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["check", "srv-1"]), "admin")

            store = RunArtifactStore(app.config)
            run = store.latest_run(session=session, mode_id="admin")
            assert run is not None
            state = store.load_state(run)
            ctx = dict(state.get("mode_context") or {})
            execution_context = dict(ctx.get("execution_context") or {})
            assert state["phase"] == "complete"
            assert state["status"] == "completed"
            assert ctx["target_transport"] == "local"
            assert ctx["operation_payload"]["kind"] == "manual_check"
            assert execution_context["skill_selector_bypassed"] is True
            assert execution_context["skill_selector_bypass_reason"] == "native_admin_transport"
            assert execution_context["check_only"] is True
            assert execution_context["destructive_execution"] is False
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_run_artifacts_isolate_sequential_runs_with_different_commands(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "manual-sequential"))
            app.manager.set_active(1, session.id)
            _write_admin_config(session, _admin_exec_config())

            mode = app.mode_registry.get("admin")
            assert mode is not None

            async def _fake_execute(**kwargs):
                context = kwargs["context"]
                return AdminExecutionResult(
                    success=True,
                    text=f"{context.command}:{context.action_id}:{context.target}",
                    returncode=0,
                    logged_action_id=f"executor:{context.command}:{context.action_id}",
                )

            mode._executor.execute = _fake_execute

            _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["check", "srv-1"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["dry-run", "off"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["run", "safe_action", "srv-1"]), "admin")

            store = RunArtifactStore(app.config)
            runs = store.list_runs(session=session, mode_id="admin", limit=2)
            assert len(runs) == 2
            assert runs[0].run_id != runs[1].run_id
            states_by_kind = {}
            for run in runs:
                state = store.load_state(run)
                kind = str(((state.get("mode_context") or {}).get("operation_payload") or {}).get("kind") or "")
                states_by_kind[kind] = state

            assert {"manual_check", "manual_run"} <= set(states_by_kind)
            assert states_by_kind["manual_run"]["source_prompt_hash"] != states_by_kind["manual_check"]["source_prompt_hash"]
            assert states_by_kind["manual_run"]["mode_context"]["execution_context"]["destructive_execution"] is True
            assert states_by_kind["manual_check"]["mode_context"]["execution_context"]["destructive_execution"] is False
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_run_rejects_invalid_server_and_not_allowlisted_action(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "reject"))
            app.manager.set_active(1, session.id)
            _write_admin_config(session, _admin_exec_config())

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await app.handlers.cmd_mode(
                update,
                types.SimpleNamespace(args=["run", "forbidden_action", "srv-1"]),
                "admin",
            )
            await app.handlers.cmd_mode(
                update,
                types.SimpleNamespace(args=["run", "safe_action", "srv-x"]),
                "admin",
            )

            assert "не входит в allowlist" in str(sent[-2][1] or "")
            assert "не найден" in str(sent[-1][1] or "")
            assert "admin" in str(sent[-1][1] or "")
            assert sent[-2][3].get("md2") is True
            assert sent[-1][3].get("md2") is True
        finally:
            await app.shutdown_runtime()
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_run_blocks_free_text_arguments(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "free-text"))
            app.manager.set_active(1, session.id)
            _write_admin_config(session, _admin_exec_config())

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["enable"]), "admin")
            await app.handlers.cmd_mode(
                update,
                types.SimpleNamespace(args=["run", "safe_action", "srv-1", "&&", "rm", "-rf", "/"]),
                "admin",
            )

            assert "Формат: /admin run" in str(sent[-1][1] or "")
            assert sent[-1][3].get("md2") is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_incidents_and_actions_commands_are_session_scoped(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session_a = app.manager.create(1, "dummy", str(tmp_path / "list-a"))
            session_b = app.manager.create(1, "dummy", str(tmp_path / "list-b"))
            app.manager.set_active(1, session_a.id)

            store = AdminStateStore(app.config.defaults.state_path)
            store.create_incident("inc-a1", session_id=session_a.id, chat_id=1, payload={"severity": "high"})
            store.create_incident("inc-b1", session_id=session_b.id, chat_id=1, payload={"severity": "low"})
            store.create_action("act-a1", session_id=session_a.id, chat_id=1, payload={"kind": "restart"})
            store.create_action("act-b1", session_id=session_b.id, chat_id=1, payload={"kind": "noop"})

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["incidents"]), "admin")
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["actions"]), "admin")

            incidents_text = str(sent[-2][1] or "")
            actions_text = str(sent[-1][1] or "")
            assert "inc-a1" in incidents_text
            assert "inc-b1" not in incidents_text
            assert "act-a1" in actions_text
            assert "act-b1" not in actions_text
            assert sent[-2][3].get("md2") is True
            assert sent[-1][3].get("md2") is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_ack_command_updates_ack_and_alert_state(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "ack"))
            app.manager.set_active(1, session.id)
            store = AdminStateStore(app.config.defaults.state_path)
            store.create_incident(
                "inc-1",
                session_id=session.id,
                chat_id=1,
                payload={"alert_id": "alert-1"},
            )
            store.create_alert_state("alert-1", session_id=session.id, chat_id=1, payload={"state": "firing"})

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["ack", "inc-1"]), "admin")

            ack = store.get_acknowledgement(f"{session.id}:inc-1", chat_id=1)
            alert = store.get_alert_state("alert-1", chat_id=1)

            assert ack is not None
            assert ack["payload"].get("incident_id") == "inc-1"
            assert ack["payload"].get("alert_id") == "alert-1"
            assert ack["payload"].get("acked_by_user_id") == 42
            assert alert is not None
            assert bool(alert["payload"].get("acknowledged")) is True
            assert str(alert["payload"].get("acknowledgement_id") or "") == f"{session.id}:inc-1"

            assert "ACK выполнен: incident_id=inc-1" in str(sent[-1][1] or "")
            assert sent[-1][3].get("md2") is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_mute_and_unmute_update_session_state(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session = app.manager.create(1, "dummy", str(tmp_path / "mute"))
            app.manager.set_active(1, session.id)
            store = AdminStateStore(app.config.defaults.state_path)

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["mute", "10"]), "admin")
            muted_state = store.get_session_state(session.id, chat_id=1)
            assert muted_state is not None
            assert muted_state.get("muted_until_ts") is not None
            assert float(muted_state["muted_until_ts"]) > 0

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["unmute"]), "admin")
            unmuted_state = store.get_session_state(session.id, chat_id=1)
            assert unmuted_state is not None
            assert unmuted_state.get("muted_until_ts") is None

            assert "Alerts muted until_ts=" in str(sent[-2][1] or "")
            assert "Alerts unmuted." in str(sent[-1][1] or "")
            assert sent[-2][3].get("md2") is True
            assert sent[-1][3].get("md2") is True
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_approvals_commands_are_scoped_and_manage_entries(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session_a = app.manager.create(1, "dummy", str(tmp_path / "approvals-a"))
            session_b = app.manager.create(1, "dummy", str(tmp_path / "approvals-b"))
            app.manager.set_active(1, session_a.id)

            store = AdminStateStore(app.config.defaults.state_path)
            override_a1 = f"override:1:{session_a.id}:a1"
            override_a2 = f"override:1:{session_a.id}:a2"
            override_b1 = f"override:1:{session_b.id}:b1"
            store.create_approved_override(
                override_a1,
                session_id=session_a.id,
                chat_id=1,
                payload={"hash": "hash-a1", "approved": True},
            )
            store.create_approved_override(
                override_a2,
                session_id=session_a.id,
                chat_id=1,
                payload={"hash": "hash-a2", "approved": True},
            )
            store.create_approved_override(
                override_b1,
                session_id=session_b.id,
                chat_id=1,
                payload={"hash": "hash-b1", "approved": True},
            )

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["approvals", "list"]), "admin")
            await app.handlers.cmd_mode(
                update,
                types.SimpleNamespace(args=["approvals", "revoke", override_a1]),
                "admin",
            )
            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["approvals", "clear"]), "admin")

            list_text = str(sent[-3][1] or "")
            assert override_a1 in list_text
            assert override_a2 in list_text
            assert override_b1 not in list_text
            assert "hash-a1" in list_text
            assert "hash-a2" in list_text

            assert "Approval revoked: override_id=" in str(sent[-2][1] or "")
            assert store.get_approved_override(override_a1, chat_id=1) is None

            assert "Approvals cleared: 1" in str(sent[-1][1] or "")
            assert store.list_approved_overrides(session_a.id, chat_id=1, limit=10) == []
            assert store.get_approved_override(override_b1, chat_id=1) is not None
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_skill_install_commands_are_scoped_and_manage_pending_entries(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        try:
            session_a = app.manager.create(1, "dummy", str(tmp_path / "skills-a"))
            session_b = app.manager.create(1, "dummy", str(tmp_path / "skills-b"))
            app.manager.set_active(1, session_a.id)

            record_a1 = _register_pending_skill_install(app, session_a, skill_id="playwright-cli-local", task_hash="sha256:a1")
            record_a2 = _register_pending_skill_install(app, session_a, skill_id="xlsx-local", task_hash="sha256:a2")
            record_b1 = _register_pending_skill_install(app, session_b, skill_id="hidden-b", task_hash="sha256:b1")
            assert record_a1 is not None
            assert record_a2 is not None
            assert record_b1 is not None

            sent = _install_send_recorder(app)
            update = types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=1),
                effective_user=types.SimpleNamespace(id=42),
            )

            await app.handlers.cmd_mode(update, types.SimpleNamespace(args=["skills", "list"]), "admin")
            await app.handlers.cmd_mode(
                update,
                types.SimpleNamespace(args=["skills", "approve", record_a1.approval_id]),
                "admin",
            )
            await app.handlers.cmd_mode(
                update,
                types.SimpleNamespace(args=["skills", "reject", record_a2.approval_id]),
                "admin",
            )

            list_text = str(sent[-3][1] or "")
            assert record_a1.approval_id in list_text
            assert record_a2.approval_id in list_text
            assert record_b1.approval_id not in list_text
            assert "playwright-cli-local" in list_text
            assert "xlsx-local" in list_text

            approve_text = str(sent[-2][1] or "")
            assert "установлен локально" in approve_text
            installed_manifest = Path(session_a.workdir) / ".cli-proxy" / "skills" / "playwright-cli-local" / "SKILL.md"
            assert installed_manifest.exists()
            assert app.mode_skill_runtime.policy_service.get_pending_install(
                session=session_a,
                approval_id=record_a1.approval_id,
            ) is None

            reject_text = str(sent[-1][1] or "")
            assert "отклонена" in reject_text
            decision = app.mode_skill_runtime.policy_service.evaluate_install_request(
                session=session_a,
                mode_id="agent",
                phase="execute",
                task_hash="sha256:a2",
                skill_id="xlsx-local",
            )
            assert decision.status == "rejected_lockout"
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_admin_chat_approve_plan_callback_sends_plan_result(tmp_path) -> None:
    async def _run() -> None:
        workdir = tmp_path / "chat-plan-callback"
        workdir.mkdir()
        session = types.SimpleNamespace(id="sid-plan", workdir=str(workdir))
        _write_admin_config(
            session,
            {
                "admin": {
                    "actions": {
                        "local": {
                            "check_disk": {
                                "argv": ["bash", "-lc", "echo PLAN_STEP_OK"],
                                "timeout_sec": 5,
                                "risk_level": "low",
                            },
                        },
                        "ssh": {},
                    },
                    "allowlist": {
                        "local": {
                            "check_disk": {
                                "argv": ["bash", "-lc", "echo PLAN_STEP_OK"],
                                "timeout_sec": 5,
                            },
                        },
                        "ssh": {},
                    },
                    "monitor": {"enabled": False, "interval_sec": 30, "servers": []},
                }
            },
        )
        approval_id = "chat-plan-callback"
        ChatPendingStore(str(workdir)).save(
            approval_id,
            {
                "approval_id": approval_id,
                "session_id": session.id,
                "intent": {
                    "type": "propose_plan",
                    "text": "run safe plan",
                    "steps": [{"target": "local", "action_id": "check_disk"}],
                    "stop_on_error": True,
                },
            },
        )
        mode = AdminMode()
        ms = _RecordingModeMessaging()
        mode._messaging = lambda **_kwargs: ms

        result = await mode._cb_chat_approve(
            bot_app=types.SimpleNamespace(),
            session=session,
            chat_id=1,
            user_id=42,
            context=None,
            query=None,
            approval_id=approval_id,
            save_action=False,
        )

        sent_text = "\n".join(str(item.get("text") or "") for item in ms.events)
        assert result.success is True
        assert "Plan выполнен (1/1 шагов)" in sent_text
        assert "PLAN_STEP_OK" in sent_text
        assert "execute_pending failed" not in sent_text
        assert ChatPendingStore(str(workdir)).get(approval_id) is None
        assert any("PLAN_STEP_OK" in item.text for item in ChatMemory(str(workdir)).load_messages())

    asyncio.run(_run())
