import asyncio
import pytest
from collections import deque

from agent.tooling import helpers as tooling_helpers
from app.services.input_dispatch_service import InputDispatchService
from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ToolResult
from modes.sdk.services.mode_registry import ModeRegistryService
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


_DESKTOP_BOT_APP_PUBLIC_SURFACE = {
    "access_policy_service",
    "config",
    "get_runtime_by_capability",
    "input_dispatch_service",
    "is_admin",
    "is_allowed",
    "is_user",
    "metrics",
    "mode_input_router",
    "mode_registry",
    "mode_registry_service",
    "mode_run_operations",
    "notify",
    "pending_input_ui",
    "report_history_service",
    "run_prompt",
    "security",
    "send_output",
    "ssh_service",
    "system_event_bus",
    "ui_state",
}


def _desktop_bot_app_public_surface(bot_app) -> set[str]:
    return {
        name
        for name in (set(vars(type(bot_app))) | set(vars(bot_app)))
        if not name.startswith("_")
    }


def test_desktop_bot_app_legacy_adapter_public_surface_is_allowlisted(tmp_path) -> None:
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

    bot_app = facade._desktop_bot_app()
    facade.config = _build_config(tmp_path)
    refreshed = facade._desktop_bot_app()

    assert refreshed is bot_app
    doc = str(type(bot_app).__doc__ or "")
    assert "Legacy compatibility shim" in doc
    assert "not an extension point" in doc
    assert _desktop_bot_app_public_surface(bot_app) == _DESKTOP_BOT_APP_PUBLIC_SURFACE


def test_desktop_mode_launch_router_uses_lint_evolution_hook(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    cfg.lint_evolution.enabled = True
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

    bot_app = facade._desktop_bot_app()
    facade._desktop_mode_launch_adapter_instance()

    assert callable(bot_app.mode_input_router.lint_evolution_hook)

    cfg.lint_evolution.enabled = False
    facade._desktop_mode_launch_adapter_instance()

    assert bot_app.mode_input_router.lint_evolution_hook is None


@pytest.mark.asyncio
async def test_desktop_mode_callback_and_dialog_message_flow(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    registry = ModeRegistry()

    class DialogMode(BaseMode):
        mode_id = "dlg"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            if callback.action == "start_dialog":
                dialogs = self.require_service("dialogs")

                async def _on_message(msg, dctx):
                    # For Desktop, chat_id is session_uid (string), use hash as int fallback
                    try:
                        cid = int(msg.chat_id)
                    except (ValueError, TypeError):
                        cid = hash(str(msg.chat_id)) % 1000000
                    dialogs.end(chat_id=cid, session_id=session_uid, mode_id=self.mode_id)
                    return ToolResult.ok("DIALOG:" + msg.text)

                # For Desktop, callback.chat_id is session_uid (string)
                try:
                    cid = int(callback.chat_id)
                except (ValueError, TypeError):
                    cid = hash(str(callback.chat_id)) % 1000000
                dialogs.start(chat_id=cid, session_id=session_uid, mode_id=self.mode_id, on_message=_on_message)
                return ToolResult.ok("dialog_started")
            return ToolResult.ok("cb:" + str(callback.action))

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            return "PIPE:" + str(user_text)

    registry.register(DialogMode())
    mode_registry_service = ModeRegistryService(registry)

    task_service = TaskService()
    sessions = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "dlg"

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    # Start a dialog via callback router.
    ok = await facade.handle_mode_callback(session_uid, data="ma:dlg:start_dialog:{}")
    assert ok is True

    # Dialog message should be intercepted and routed.
    out = await facade.handle_dialog_message(session_uid, text="hello")
    assert out == "DIALOG:hello"

    # Ensure mode callbacks can send UI messages (ToolResult.output).
    assert any(ev == "ui:message" for ev, _p in events)


@pytest.mark.asyncio
async def test_desktop_approval_flow_syncs_with_pending_store(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)

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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    tooling_helpers.configure_pending_commands_store(cfg.defaults.state_path)
    tooling_helpers.set_approval_callback(facade._request_command_approval)

    approve_id = tooling_helpers._store_pending_command(  # type: ignore[attr-defined]
        session_uid, 1, "printf approved", str(tmp_path), "Dangerous"
    )
    facade._request_command_approval(session_uid, approve_id, "printf approved", "Dangerous")

    assert any(
        ev == "ui:mode_menu"
        and any(btn.get("data") == f"approve_cmd:{approve_id}" for row in (payload.get("rows") or []) for btn in row)
        for ev, payload in events
    )

    ok_approve = await facade.handle_mode_callback(session_uid, data=f"approve_cmd:{approve_id}")
    assert ok_approve is True
    assert tooling_helpers.pop_pending_command(approve_id) is None
    assert any(
        ev == "ui:message" and str(payload.get("text") or "").startswith("Одобрено.")
        for ev, payload in events
    )
    assert any(
        ev == "ui:message" and "approved" in str(payload.get("text") or "")
        for ev, payload in events
    )

    deny_id = tooling_helpers._store_pending_command(  # type: ignore[attr-defined]
        session_uid, 1, "printf denied", str(tmp_path), "Dangerous"
    )
    ok_deny = await facade.handle_mode_callback(session_uid, data=f"deny_cmd:{deny_id}")
    assert ok_deny is True
    assert tooling_helpers.pop_pending_command(deny_id) is None
    assert any(
        ev == "ui:message" and str(payload.get("text") or "") == "Команда отклонена."
        for ev, payload in events
    )


@pytest.mark.asyncio
async def test_desktop_busy_prompt_queue_actions_match_telegram_flow(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass
    session.busy = True

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    bot_app = facade._desktop_bot_app()
    await bot_app.input_dispatch_service.handle_cli_input(
        session, "hello", session_uid,
        context=object(),
        dest={"kind": "desktop", "session_uid": session_uid},
    )

    assert any(
        ev == "ui:mode_menu"
        and str(payload.get("text") or "") == InputDispatchService.queue_confirm_prompt_text()
        for ev, payload in events
    )
    pending = InputDispatchService.pending_head(bot_app.ui_state.pending, session_uid)
    assert pending is not None
    assert str(getattr(pending, "text", "") or "") == "hello"
    assert list(session.queue) == []

    ok = await facade.handle_mode_callback(session_uid, data="queue_input")
    assert ok is True
    assert [item["text"] for item in list(session.queue)] == ["hello"]

    session.busy = False
    run_calls = []

    async def _fake_run_session_input(session_uid, text, **kwargs):
        run_calls.append(
            {
                "session_id": str(session_uid),
                "text": str(text or ""),
                "kwargs": dict(kwargs or {}),
            }
        )
        return "ok"

    facade.run_session_input = _fake_run_session_input  # type: ignore[assignment]
    await facade._kick_session_queue_if_idle(session_uid=session_uid)
    for _ in range(10):
        if run_calls:
            break
        await asyncio.sleep(0)
    assert run_calls and run_calls[0]["text"] == "hello"
    assert list(session.queue) == []


@pytest.mark.asyncio
async def test_desktop_queue_input_enqueues_before_dispatch_when_session_is_idle(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.busy = True

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    bot_app = facade._desktop_bot_app()
    await bot_app.input_dispatch_service.handle_cli_input(
        session, "hello", session_uid,
        context=object(),
        dest={"kind": "desktop", "session_uid": session_uid},
    )

    session.busy = False
    run_calls = []

    async def _fake_run_session_input(session_uid, text, **kwargs):
        run_calls.append(
            {
                "session_id": str(session_uid),
                "text": str(text or ""),
                "kwargs": dict(kwargs or {}),
            }
        )
        return "ok"

    facade.run_session_input = _fake_run_session_input  # type: ignore[assignment]

    ok = await facade.handle_mode_callback(session_uid, data="queue_input")

    assert ok is True
    for _ in range(10):
        if run_calls:
            break
        await asyncio.sleep(0)
    assert run_calls == [
        {
            "session_id": session_uid,
            "text": "hello",
            "kwargs": {"prepared_attachments": None},
        }
    ]
    assert list(session.queue) == []
    assert bot_app.ui_state.pending.get(session_uid) is None
    assert any(
        ev == "ui:message" and str(payload.get("text") or "") == "Ввод поставлен в очередь."
        for ev, payload in events
    )


@pytest.mark.asyncio
async def test_desktop_busy_prompt_cancel_current_interrupts_and_drops_pending(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass
    session.busy = True
    session.queue.append({"text": "queued", "dest": {"kind": "desktop", "chat_id": session_uid}})

    interrupted = {"ok": False}

    def _interrupt() -> None:
        interrupted["ok"] = True

    session.interrupt = _interrupt

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    bot_app = facade._desktop_bot_app()
    await bot_app.input_dispatch_service.handle_cli_input(
        session, "hello", session_uid,
        context=object(),
        dest={"kind": "desktop", "session_uid": session_uid},
    )

    ok = await facade.handle_mode_callback(session_uid, data="cancel_current")
    assert ok is True
    assert interrupted["ok"] is True
    assert any(
        ev == "ui:message" and "Текущая генерация прервана" in str(payload.get("text") or "")
        for ev, payload in events
    )


@pytest.mark.asyncio
async def test_desktop_try_queue_busy_input_requires_confirmation_before_queue(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass
    session.busy = True

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    queued = await facade.try_queue_busy_input(session_uid, "from_ui")
    assert queued is True
    assert any(
        ev == "ui:mode_menu"
        and str(payload.get("text") or "") == InputDispatchService.queue_confirm_prompt_text()
        for ev, payload in events
    )
    pending = InputDispatchService.pending_head(facade._desktop_bot_app().ui_state.pending, session_uid)
    assert pending is not None
    assert str(getattr(pending, "text", "") or "") == "from_ui"
    assert list(session.queue) == []

    session.busy = False
    session.queue.clear()
    not_queued = await facade.try_queue_busy_input(session_uid, "free_session")
    assert not_queued is False


@pytest.mark.parametrize("signal_name", ["busy", "run_lock", "tick"])
@pytest.mark.asyncio
async def test_desktop_try_queue_busy_input_running_signals_recover_after_signal_clears(
    tmp_path,
    signal_name: str,
) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    tick_state = {"active": False}
    session.is_active_by_tick = lambda: bool(tick_state["active"])

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    if signal_name == "busy":
        session.busy = True
    elif signal_name == "run_lock":
        await session.run_lock.acquire()
    elif signal_name == "tick":
        tick_state["active"] = True

    queued = await facade.try_queue_busy_input(session_uid, f"{signal_name}-from-ui")

    assert queued is True
    pending = InputDispatchService.pending_head(facade._desktop_bot_app().ui_state.pending, session_uid)
    assert pending is not None
    assert str(getattr(pending, "text", "") or "") == f"{signal_name}-from-ui"
    assert any(
        ev == "ui:mode_menu"
        and str(payload.get("text") or "") == InputDispatchService.queue_confirm_prompt_text()
        for ev, payload in events
    )

    if signal_name == "busy":
        session.busy = False
    elif signal_name == "run_lock":
        session.run_lock.release()
    elif signal_name == "tick":
        tick_state["active"] = False

    not_queued = await facade.try_queue_busy_input(session_uid, f"{signal_name}-after-clear")

    assert not_queued is False


@pytest.mark.asyncio
async def test_desktop_stage_session_input_shows_take_in_work_prompt_for_free_session(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    await facade.stage_session_input(session_uid, "free_session")

    bot_app = facade._desktop_bot_app()
    pending = InputDispatchService.pending_head(bot_app.ui_state.pending, session_uid)
    assert pending is not None
    assert str(getattr(pending, "text", "") or "") == "free_session"
    assert list(session.queue) == []
    assert any(
        ev == "ui:mode_menu"
        and str(payload.get("text") or "") == InputDispatchService.take_in_work_prompt_text()
        and payload.get("rows") == [[
            {"text": "✅ Взять в работу", "data": "take_pending_input"},
        ], [
            {"text": "❌ Отмена ввода", "data": "discard_input"},
        ]]
        for ev, payload in events
    )


@pytest.mark.asyncio
async def test_desktop_stage_session_input_clears_previous_prompt_without_stale_message(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    await facade.stage_session_input(session_uid, "first")
    await facade.stage_session_input(session_uid, "second")

    assert any(
        ev == "ui:mode_menu"
        and str(payload.get("text") or "") == ""
        and payload.get("rows") == []
        for ev, payload in events
    )
    assert not any(
        ev == "ui:message" and str(payload.get("text") or "") == "Сообщение обновлено."
        for ev, payload in events
    )
    pending = InputDispatchService.pending_head(facade._desktop_bot_app().ui_state.pending, session_uid)
    assert pending is not None
    assert str(getattr(pending, "text", "") or "") == "first\n\nsecond"


@pytest.mark.asyncio
async def test_desktop_try_queue_busy_input_recovers_when_pending_store_is_corrupted(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass
    session.busy = True

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    bot_app = facade._desktop_bot_app()
    bot_app.ui_state.pending = []  # type: ignore[assignment]
    session.queue.append({"text": "queued", "dest": {"kind": "desktop", "chat_id": session_uid}})

    queued = await facade.try_queue_busy_input(session_uid, "recover_pending")
    assert queued is True
    assert isinstance(bot_app.ui_state.pending, dict)
    pending_queue = bot_app.ui_state.pending.get(session_uid)
    assert isinstance(pending_queue, deque)
    assert [item.text for item in pending_queue] == ["recover_pending"]
    assert any(
        ev == "ui:mode_menu"
        and "В очереди уже есть сообщение" in str(payload.get("text") or "")
        for ev, payload in events
    )


@pytest.mark.asyncio
async def test_desktop_pending_busy_queue_is_fifo_and_notifies_on_overflow(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass
    session.busy = True

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    for idx in range(1, 7):
        queued = await facade.try_queue_busy_input(session_uid, f"msg-{idx}")
        assert queued is True

    busy_prompt_before = sum(
        1
        for ev, payload in events
        if ev == "ui:mode_menu" and InputDispatchService.queue_confirm_prompt_text() in str(payload.get("text") or "")
    )

    bot_app = facade._desktop_bot_app()
    pending_queue = bot_app.ui_state.pending.get(session_uid)
    assert isinstance(pending_queue, deque)
    assert [item["text"] for item in list(session.queue)] == []
    assert [item.text for item in pending_queue] == ["msg-1\n\nmsg-2\n\nmsg-3\n\nmsg-4\n\nmsg-5\n\nmsg-6"]

    session.busy = False
    run_calls = []

    async def _fake_run_session_input(session_uid, text, **kwargs):
        run_calls.append(
            {
                "session_id": str(session_uid),
                "text": str(text or ""),
                "kwargs": dict(kwargs or {}),
            }
        )
        return "ok"

    facade.run_session_input = _fake_run_session_input  # type: ignore[assignment]
    ok = await facade.handle_mode_callback(session_uid, data="queue_input")
    assert ok is True
    for _ in range(10):
        if len(run_calls) >= 1:
            break
        await asyncio.sleep(0)
    assert len(run_calls) == 1
    assert run_calls[0]["text"] == "msg-1\n\nmsg-2\n\nmsg-3\n\nmsg-4\n\nmsg-5\n\nmsg-6"
    assert list(session.queue) == []
    assert bot_app.ui_state.pending.get(session_uid) is None
    busy_prompt_after = sum(
        1
        for ev, payload in events
        if ev == "ui:mode_menu" and InputDispatchService.queue_confirm_prompt_text() in str(payload.get("text") or "")
    )
    assert busy_prompt_after == busy_prompt_before


@pytest.mark.asyncio
async def test_desktop_metrics_snapshot_supports_busy_queue_counter(tmp_path) -> None:
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

    session = sessions.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    pass
    session.busy = True

    queued = await facade.try_queue_busy_input(session_uid, "hello")
    assert queued is True

    snapshot = facade.get_metrics_snapshot()
    assert "queued: 1" in snapshot


@pytest.mark.asyncio
async def test_desktop_bot_app_run_prompt_passes_image_paths_from_dest(tmp_path) -> None:
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

    class _FakeSession:
        def __init__(self) -> None:
            self.captured_text = ""
            self.captured_images = None

        async def run_prompt(self, text: str, image_paths=None):
            self.captured_text = str(text or "")
            self.captured_images = image_paths
            return "ok"

    bot_app = facade._desktop_bot_app()
    fake = _FakeSession()
    result = await bot_app.run_prompt(
        fake,
        "hello",
        {"kind": "desktop", "chat_id": 1, "image_paths": ["/tmp/a.png", "/tmp/b.png"]},
        context=object(),
    )
    assert result == "ok"
    assert fake.captured_text == "hello"
    assert fake.captured_images == ["/tmp/a.png", "/tmp/b.png"]
