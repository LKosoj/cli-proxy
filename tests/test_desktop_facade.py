from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.events.bus import ModeLaunchCompletedEvent
from app.security.audit import EventBusAuditService
from app.services import ConfigService, SessionService, TaskService
from app.services.actor_identity import DESKTOP_ACTOR_ID
from app.services.config_service import ConfigProvider
from app.services.project_registry import ProjectOwnershipError
from app.services.scheduler_service import SchedulerValidationError
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ModeRegistryService, ToolResult
from session import SessionManager, get_session_execution_backend, session_runtime_uid


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
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


class _CaptureMode(BaseMode):
    def __init__(self, mode_id: str, calls: list[dict]) -> None:
        super().__init__()
        self.mode_id = mode_id
        self._calls = calls

    async def handle_input(self, message, ctx):
        context = ctx.get("context")
        launch_request = getattr(context, "launch_request", None)
        self._calls.append(
            {
                "mode_id": self.mode_id,
                "text": str(message.text or ""),
                "session_id": str(getattr(ctx.get("session"), "id", "") or ""),
                "session_uid": str(
                    getattr(getattr(ctx.get("session"), "conversation_scope", None), "session_uid", "") or ""
                ),
                "dest": dict(ctx.get("dest") or {}),
                "project_slug": (
                    str(getattr(launch_request, "project", {}).get("slug", "") or "")
                    if launch_request is not None
                    else ""
                ),
                "origin_key": (
                    str(getattr(launch_request, "origin", {}).get("key", "") or "")
                    if launch_request is not None
                    else ""
                ),
                "actor": dict(getattr(launch_request, "actor", {}) or {}) if launch_request is not None else {},
            }
        )
        return ToolResult.ok()

    async def handle_callback(self, callback, ctx):
        return ToolResult.ok()


def _build_config(tmp_path: Path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[], admlist_chat_ids=[]),
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
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(enabled=False),
    )


def _build_desktop_facade_runtime(tmp_path: Path, *, intent: str, mode_id: str = "capture") -> dict[str, object]:
    cfg = _build_config(tmp_path, intent=intent)
    registry = ModeRegistry()
    registry_service = ModeRegistryService(registry)
    calls: list[dict] = []
    registry.register(_CaptureMode(mode_id, calls))
    task_service = TaskService()
    session_service = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
        mode_registry_service=registry_service,
    )
    workdir = Path(cfg.defaults.workdir) / "desktop-project"
    workdir.mkdir(parents=True, exist_ok=True)
    session = session_service.create_desktop_session("dummy", str(workdir))
    session.name = f"Desktop {intent}"
    return {
        "config": cfg,
        "calls": calls,
        "facade": facade,
        "session": session,
    }


@pytest.mark.asyncio
async def test_desktop_final_questions_are_rendered_as_plain_message(tmp_path: Path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="final_questions")
    facade: ApplicationFacade = runtime["facade"]
    facade.config = runtime["config"]
    session = runtime["session"]
    notifications = []
    unsubscribe = facade.subscribe(notifications.append)
    text = "Итог.\n\nДва вопроса:\n1. Оставить первый блок?\n2. Удалить второй блок?"

    try:
        await facade._desktop_bot_app().send_output(
            session,
            {"kind": "desktop", "session_uid": session_runtime_uid(session)},
            text,
            context=None,
        )
    finally:
        unsubscribe()

    assert [notification.event for notification in notifications] == ["ui:message"]
    assert notifications[0].payload["text"] == text
    assert facade._desktop_bot_app().ui_state.pending_questions == {}


@pytest.mark.asyncio
async def test_desktop_tmux_reread_detaches_monitor_without_interrupting_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, TmuxRecoveryRequest

    runtime = _build_desktop_facade_runtime(tmp_path, intent="tmux_reread")
    facade: ApplicationFacade = runtime["facade"]
    facade.config = runtime["config"]
    session = runtime["session"]
    session_uid = session_runtime_uid(session)

    monkeypatch.setattr(
        "desktop.services.application_facade.get_session_execution_backend",
        lambda _session: "tmux",
    )

    async def _get_recovery(_backend, _session):
        return TmuxRecoveryRequest(
            request_id="recover-desktop",
            started_at=10.0,
            offset=0,
            prompt="original request",
            dest={},
        )

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _get_recovery)

    cancelled: list[bool] = []

    async def _cancel_session(uid, *, timeout_s=1.0):
        cancelled.append(bool(getattr(session, "_preserve_tmux_on_shutdown", False)))
        return 1

    facade.task_service.cancel_session = _cancel_session
    started: list[str] = []
    facade._start_desktop_tmux_recovery = lambda _session, request: started.append(request.request_id)
    facade._desktop_bot_app().ui_state.pending_questions["q1"] = {
        "session_uid": session_uid,
        "question": "stale",
        "options": ["1. Да"],
    }

    outcome = await facade.reread_tmux_output(session_uid)

    assert outcome == "started"
    # Монитор снимается с взведённым флагом сохранения tmux: Ctrl+C в pane не уходит.
    assert cancelled == [True]
    assert session._preserve_tmux_on_shutdown is False
    assert facade._desktop_bot_app().ui_state.pending_questions == {}
    assert started == ["recover-desktop"]


@pytest.mark.asyncio
async def test_desktop_tmux_reread_observes_live_pane_after_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, TmuxRecoveryRequest

    runtime = _build_desktop_facade_runtime(tmp_path, intent="tmux_observe")
    facade: ApplicationFacade = runtime["facade"]
    facade.config = runtime["config"]
    session = runtime["session"]
    session_uid = session_runtime_uid(session)

    monkeypatch.setattr(
        "desktop.services.application_facade.get_session_execution_backend",
        lambda _session: "tmux",
    )

    async def _no_recovery(_backend, _session):
        return None

    async def _observe(_backend, _session):
        return TmuxRecoveryRequest(
            request_id="observe-desktop",
            started_at=10.0,
            offset=4096,
            prompt="",
            dest={},
        )

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _no_recovery)
    monkeypatch.setattr(TmuxExecutionBackend, "build_observe_request", _observe)

    async def _cancel_session(uid, *, timeout_s=1.0):
        return 1

    facade.task_service.cancel_session = _cancel_session
    started: list[str] = []
    facade._start_desktop_tmux_recovery = lambda _session, request: started.append(request.request_id)

    outcome = await facade.reread_tmux_output(session_uid)

    assert outcome == "started"
    assert started == ["observe-desktop"]


@pytest.mark.asyncio
async def test_desktop_startup_observes_live_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.cli_backends.tmux_backend import TmuxExecutionBackend, TmuxRecoveryRequest

    runtime = _build_desktop_facade_runtime(tmp_path, intent="tmux_startup_observe")
    facade: ApplicationFacade = runtime["facade"]
    facade.config = runtime["config"]

    monkeypatch.setattr(
        "desktop.services.application_facade.get_session_execution_backend",
        lambda _session: "tmux",
    )

    async def _no_recovery(_backend, _session):
        return None

    checked: list[bool] = []

    async def _observe(_backend, _session, *, require_recent_activity=False):
        checked.append(require_recent_activity)
        return TmuxRecoveryRequest(
            request_id="observe-startup",
            started_at=10.0,
            offset=4096,
            prompt="",
            dest={},
            observe=True,
        )

    monkeypatch.setattr(TmuxExecutionBackend, "get_recovery_request", _no_recovery)
    monkeypatch.setattr(TmuxExecutionBackend, "build_observe_request", _observe)
    started: list[str] = []
    facade._start_desktop_tmux_recovery = lambda _session, request: started.append(request.request_id)

    recovered = await facade._recover_tmux_sessions()

    assert recovered == 1
    assert started == ["observe-startup"]
    # На старте наблюдение цепляется только к панелям, печатающим прямо сейчас.
    assert checked == [True]


@pytest.mark.asyncio
async def test_desktop_tmux_reread_callback_closes_menu_without_messages(tmp_path: Path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="tmux_reread_menu")
    facade: ApplicationFacade = runtime["facade"]
    facade.config = runtime["config"]
    session = runtime["session"]
    session_uid = session_runtime_uid(session)

    async def _reread(uid: str) -> str:
        assert uid == session_uid
        return "started"

    facade.reread_tmux_output = _reread
    notifications = []
    unsubscribe = facade.subscribe(notifications.append)
    try:
        ok = await facade.handle_mode_callback(session_uid, data=f"sess_tmux_reread:{session_uid}")
    finally:
        unsubscribe()

    assert ok is True
    # Меню закрывается пустым ui:mode_menu, других сообщений нет.
    assert [notification.event for notification in notifications] == ["ui:mode_menu"]
    assert notifications[0].payload["text"] == ""
    assert notifications[0].payload["rows"] == []


@pytest.mark.asyncio
async def test_desktop_tmux_reread_callback_reports_failure(tmp_path: Path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="tmux_reread_menu_fail")
    facade: ApplicationFacade = runtime["facade"]
    facade.config = runtime["config"]
    session = runtime["session"]
    session_uid = session_runtime_uid(session)

    async def _reread(_uid: str) -> str:
        return "no_request"

    facade.reread_tmux_output = _reread
    notifications = []
    unsubscribe = facade.subscribe(notifications.append)
    try:
        ok = await facade.handle_mode_callback(session_uid, data=f"sess_tmux_reread:{session_uid}")
    finally:
        unsubscribe()

    assert ok is True
    assert [notification.event for notification in notifications] == ["ui:mode_menu", "ui:message"]
    assert notifications[1].payload["text"] == "Нет активного запроса tmux для перечитывания"


@pytest.mark.asyncio
async def test_desktop_facade_rejects_session_execution_backend_update(tmp_path: Path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="execution_backend")
    cfg: AppConfig = runtime["config"]
    session = runtime["session"]
    facade: ApplicationFacade = runtime["facade"]

    cfg.tools["dummy"].execution_backends = ["headless", "tmux"]
    cfg.tools["dummy"].interactive_cmd = ["bash", "-lc", "cat"]
    session_uid = session_runtime_uid(session)

    ok = await facade.update_session_setting(session_uid, "execution_backend", "tmux")

    assert ok is False
    assert get_session_execution_backend(session) == "headless"
    settings = facade.get_execution_backend_settings(session_uid)
    assert settings is not None
    assert settings["execution_backend"] == "headless"
    assert settings["available_execution_backends"] == ["headless", "tmux"]
    assert settings["backend_switch_allowed"] is False
    assert settings["backend_switch_blockers"] == ["configured in settings"]


@pytest.mark.asyncio
async def test_desktop_facade_execution_backend_follows_config_default(tmp_path: Path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="execution_backend_busy")
    cfg: AppConfig = runtime["config"]
    session = runtime["session"]
    facade: ApplicationFacade = runtime["facade"]

    cfg.tools["dummy"].execution_backends = ["headless", "tmux"]
    cfg.tools["dummy"].interactive_cmd = ["bash", "-lc", "cat"]
    cfg.tools["dummy"].default_execution_backend = "tmux"
    session_uid = session_runtime_uid(session)

    assert get_session_execution_backend(session) == "tmux"
    settings = facade.get_execution_backend_settings(session_uid)
    assert settings is not None
    assert settings["execution_backend"] == "tmux"
    assert settings["backend_switch_allowed"] is False
    assert settings["backend_switch_blockers"] == ["configured in settings"]


class _EditTextCallbackPlugin:
    plugin_id = "edit-demo"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_plugin_id(self) -> str:
        return self.plugin_id

    async def _dispatch_callback(self, update, _context) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(str(update.callback_query.data or ""))
        await update.callback_query.message.edit_text("Desktop callback updated")


def test_desktop_facade_start_can_skip_secret_validation(tmp_path) -> None:
    async def _run() -> None:
        runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_secret_skip")
        cfg = runtime["config"]
        facade = runtime["facade"]
        cfg.telegram.token = ""

        await facade.start(validate_secrets=False)
        assert facade.started is True
        await facade.shutdown()

        task_service = TaskService()
        strict_facade = ApplicationFacade(
            config_service=ConfigService(_InMemoryConfigProvider(cfg)),
            session_service=SessionService(SessionManager(cfg), task_service),
            task_service=task_service,
            mode_registry_service=None,
        )
        with pytest.raises(RuntimeError):
            await strict_facade.start()

    asyncio.run(_run())


def test_desktop_mode_launch_allowlist_policy(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_allowlist_policy")

    async def _run() -> None:
        cfg = runtime["config"]
        facade = runtime["facade"]
        session = runtime["session"]
        calls = runtime["calls"]
        session_uid = session_runtime_uid(session)
        completed: list[ModeLaunchCompletedEvent] = []
        audits: list[dict] = []

        await facade.start(validate_secrets=False)
        project_slug = facade.resolve_scheduler_project_slug(session.id)
        assert project_slug is not None

        bus = facade._desktop_system_event_bus_instance()

        async def _capture_completed(event: ModeLaunchCompletedEvent) -> None:
            completed.append(event)

        async def _capture_audit(_event_name: str, payload: dict) -> None:
            audits.append(dict(payload))

        bus.subscribe(ModeLaunchCompletedEvent, _capture_completed)
        bus.subscribe(EventBusAuditService.EVENT_NAME, _capture_audit)

        response_unresolved = await facade.publish_mode_launch_request(
            project_slug=str(project_slug),
            session_uid=session_uid,
            mode_id="capture",
            prompt="desktop unresolved actor",
            correlation_id="desktop-policy-unresolved",
        )

        assert response_unresolved["queued"] is True
        assert calls == []
        assert completed[-1].status == "denied"
        assert completed[-1].result == {"error": "actor_unresolved"}
        assert audits[-1]["action"] == "event_launch"
        assert audits[-1]["reason"] == "actor_unresolved"

        cfg.telegram.whitelist_chat_ids = [101]
        cfg.telegram.admlist_chat_ids = []
        cfg.telegram.user_workdirs = {101: [str(session.workdir)]}
        cfg.telegram.user_modes = {101: ["capture"]}
        provider = facade._desktop_identity_provider_service()
        provider.resolve_mode_launch_actor_chat_id = lambda _session: 101

        assert facade._desktop_bot_app().access_policy_service.is_mode_allowed_for_chat(101, "capture") is True

        response_allowed = await facade.publish_mode_launch_request(
            project_slug=str(project_slug),
            session_uid=session_uid,
            mode_id="capture",
            prompt="desktop explicit actor allowed",
            correlation_id="desktop-policy-allowed",
        )

        assert response_allowed["queued"] is True
        assert calls == [
            {
                "mode_id": "capture",
                "text": "desktop explicit actor allowed",
                "session_id": str(session.id),
                "session_uid": session_uid,
                "dest": {
                    "kind": "desktop",
                    "chat_id": session_uid,
                    "session_id": session_uid,
                },
                "project_slug": str(project_slug),
                "origin_key": "desktop",
                "actor": {
                    "kind": "desktop",
                    "actor_id": "desktop:default",
                    "chat_id": 101,
                    "owner_id": "desktop:default",
                },
            }
        ]
        assert completed[-1].status == "dispatched"
        assert completed[-1].correlation_id == "desktop-policy-allowed"
        assert audits[-1]["status"] == "allowed"
        assert audits[-1]["reason"] == ""
        assert audits[-1]["context"]["chat_id"] == 101

        cfg.telegram.user_modes = {101: ["agent"]}
        assert facade._desktop_bot_app().access_policy_service.is_mode_allowed_for_chat(101, "capture") is False

        response_denied = await facade.publish_mode_launch_request(
            project_slug=str(project_slug),
            session_uid=session_uid,
            mode_id="capture",
            prompt="desktop explicit actor denied",
            correlation_id="desktop-policy-denied",
        )

        assert response_denied["queued"] is True
        assert len(calls) == 1
        assert completed[-1].status == "denied"
        assert completed[-1].result == {"error": "mode_not_allowed"}
        assert audits[-1]["status"] == "denied"
        assert audits[-1]["reason"] == "mode_not_allowed"
        assert audits[-1]["context"]["chat_id"] == 101

        await facade.shutdown()

    asyncio.run(_run())


def test_desktop_scheduler_presentation_shape_and_project_scope(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_scheduler_presentation")
    facade = runtime["facade"]
    session = runtime["session"]
    cfg = runtime["config"]
    facade.config = cfg

    project_slug = facade.resolve_scheduler_project_slug(session.id)
    assert project_slug is not None
    session_uid = session_runtime_uid(session)
    targets = facade.list_scheduler_notification_targets(project_slug=str(project_slug))
    assert [item["session_uid"] for item in targets] == [session_uid]

    created = facade.create_scheduler_job(
        project_slug=str(project_slug),
        cron="*/20 * * * *",
        target_mode="capture",
        notification_target_session_uid=session_uid,
        job_name="Desktop digest",
        payload={"intent": "digest"},
    )

    assert created["job_name"] == "Desktop digest"
    assert created["owner_id"] == DESKTOP_ACTOR_ID
    assert created["project_slug"] == str(project_slug)
    assert created["notification_target"] == {"telegram_session_uid": session_uid}
    assert created["payload"] == {
        "intent": "digest",
        "project_slug": str(project_slug),
    }
    assert {
        "job_id",
        "job_name",
        "owner_id",
        "cron",
        "target_mode",
        "enabled",
        "next_run_at",
        "last_fired_at",
        "last_status",
        "last_error",
        "run_count",
        "scheduled_for",
        "project_slug",
        "notification_target",
        "payload",
    } <= set(created)
    assert facade.list_scheduler_jobs(project_slug=str(project_slug)) == [created]
    assert facade.get_scheduler_job(
        project_slug=str(project_slug),
        job_id=created["job_id"],
    ) == created

    beta_dir = Path(cfg.defaults.workdir) / "desktop-project-beta"
    beta_dir.mkdir(parents=True, exist_ok=True)
    beta_session = facade.session_service.create_desktop_session("dummy", str(beta_dir))
    beta_slug = facade.resolve_scheduler_project_slug(beta_session.id)
    assert beta_slug is not None

    with pytest.raises(ProjectOwnershipError, match="outside owned project"):
        facade.get_scheduler_job(project_slug=str(beta_slug), job_id=created["job_id"])
    with pytest.raises(SchedulerValidationError, match="scheduled job is not found"):
        facade.get_scheduler_job(project_slug=str(project_slug), job_id="missing-job")


def test_desktop_access_policy_refreshes_after_config_reload(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_policy_reload")

    async def _run() -> None:
        cfg = runtime["config"]
        facade = runtime["facade"]
        session = runtime["session"]

        await facade.start(validate_secrets=False)
        cfg.telegram.whitelist_chat_ids = [101]
        cfg.telegram.admlist_chat_ids = []
        cfg.telegram.user_workdirs = {101: [str(session.workdir)]}
        cfg.telegram.user_modes = {101: ["capture"]}
        bot_app = facade._desktop_bot_app()
        assert bot_app.access_policy_service.is_mode_allowed_for_chat(101, "capture") is True

        facade.mode_registry_service.registry.register(_CaptureMode("agent", runtime["calls"]))
        fresh = _build_config(tmp_path, intent="desktop_policy_reload_fresh")
        fresh.telegram.whitelist_chat_ids = [101]
        fresh.telegram.admlist_chat_ids = []
        fresh.telegram.user_workdirs = {101: [str(session.workdir)]}
        fresh.telegram.user_modes = {101: ["agent"]}
        facade.config_service.provider.config = fresh

        await facade.reload()
        refreshed = facade._desktop_bot_app()
        assert refreshed is bot_app
        assert refreshed.access_policy_service.is_mode_allowed_for_chat(101, "capture") is False
        assert refreshed.access_policy_service.is_mode_allowed_for_chat(101, "agent") is True
        await facade.shutdown()

    asyncio.run(_run())


def test_desktop_run_session_input_notifies_returned_output(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_run_output_notify")

    async def _run() -> None:
        facade = runtime["facade"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)
        notifications = []

        async def _fake_run(**_kwargs):
            return "desktop answer"

        unsubscribe = facade.subscribe(notifications.append)
        await facade.start(validate_secrets=False)
        try:
            facade._run_desktop_cli_prompt_with_skill_hook = _fake_run  # type: ignore[method-assign]
            result = await facade.run_session_input(session_uid, "hello")
            assert result == "desktop answer"
            assert any(
                item.event == "ui:message"
                and item.payload.get("session_id") == session_uid
                and item.payload.get("text") == "desktop answer"
                for item in notifications
            )
        finally:
            unsubscribe()
            await facade.shutdown()

    asyncio.run(_run())


def test_desktop_stage_session_input_direct_fallback_without_confirmation(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_no_pending_fallback")

    async def _run() -> None:
        cfg = runtime["config"]
        facade = runtime["facade"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)
        notifications = []

        async def _fake_run(**_kwargs):
            return "direct fallback"

        cfg.defaults.pending_input_confirmation_enabled = False
        unsubscribe = facade.subscribe(notifications.append)
        await facade.start(validate_secrets=False)
        try:
            facade._run_desktop_cli_prompt_with_skill_hook = _fake_run  # type: ignore[method-assign]
            await facade.stage_session_input(session_uid, "hello")
            assert any(
                item.event == "ui:message"
                and item.payload.get("session_id") == session_uid
                and item.payload.get("text") == "direct fallback"
                for item in notifications
            )
        finally:
            unsubscribe()
            await facade.shutdown()

    asyncio.run(_run())


def test_desktop_start_stops_stale_scheduler_on_config_change(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_scheduler_reload")

    async def _run() -> None:
        cfg = runtime["config"]
        facade = runtime["facade"]
        cfg.scheduler.enabled = True
        await facade.start(validate_secrets=False)

        class _FakeScheduler:
            def __init__(self) -> None:
                self.stopped = 0

            async def stop(self) -> None:
                self.stopped += 1

        fake_scheduler = _FakeScheduler()
        facade._desktop_scheduler_service = fake_scheduler
        facade._desktop_scheduler_started_instance = fake_scheduler

        fresh = _build_config(tmp_path, intent="desktop_scheduler_reload_fresh")
        fresh.scheduler.enabled = False
        facade.config_service.provider.config = fresh

        await facade.start(validate_secrets=False)
        assert fake_scheduler.stopped == 1
        assert facade._desktop_scheduler_started_instance is not fake_scheduler
        assert facade._desktop_scheduler_service is not fake_scheduler
        await facade.shutdown()

    asyncio.run(_run())


def test_callback_emulation_edit_message(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_callback_edit")

    async def _run() -> None:
        facade = runtime["facade"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)
        notifications = []
        plugin = _EditTextCallbackPlugin()

        unsubscribe = facade.subscribe(notifications.append)
        await facade.start(validate_secrets=False)
        try:
            bot_app = facade._desktop_bot_app()
            bot_app._mode_allows_plugin_ui = lambda _session: True
            bot_app._tool_registry = SimpleNamespace(plugins={"edit-demo": plugin})

            dispatched = await facade._dispatch_plugin_callback(session_uid, data="cb:edit-demo:apply")
            assert dispatched is True
            assert plugin.calls == ["cb:edit-demo:apply"]
            assert notifications[-1].event == "ui:message"
            assert notifications[-1].payload["session_uid"] == session_uid
            assert notifications[-1].payload["text"] == "Desktop callback updated"

            compat_result = await bot_app._edit_message(
                None,
                chat_id=session_uid,
                message_id=42,
                text="Desktop alias edit",
                md2=True,
            )
            assert getattr(compat_result, "message_id", 0) > 0
            assert notifications[-1].payload["session_uid"] == session_uid
            assert notifications[-1].payload["text"] == "Desktop alias edit"
        finally:
            unsubscribe()
            await facade.shutdown()

    asyncio.run(_run())


def test_ask_user_transport_contract(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_ask_user_contract")

    async def _run() -> None:
        facade = runtime["facade"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)
        notifications = []

        unsubscribe = facade.subscribe(notifications.append)
        await facade.start(validate_secrets=False)
        try:
            bot_app = facade._desktop_bot_app()
            await bot_app._send_ask_question(
                object(),
                session_uid,
                "session-legacy-id",
                "q-desktop-1",
                "Нужен выбор?",
                ["Да", "Нет"],
                False,
                True,
            )

            pending = bot_app.ui_state.pending_questions["q-desktop-1"]
            assert pending["question_id"] == "q-desktop-1"
            assert pending["question"] == "Нужен выбор?"
            assert pending["options"] == ["Да", "Нет"]
            assert pending["allow_custom"] is False
            assert pending["chat_id"] == session_uid
            assert pending["session_uid"] == session_uid
            assert pending["session_id"] == "session-legacy-id"
            assert bot_app.ui_state.active_ask_question_by_chat[session_uid] == "q-desktop-1"

            assert notifications[-1].event == "ui:ask_question"
            assert notifications[-1].payload["session_uid"] == session_uid
            assert notifications[-1].payload["session_id"] == "session-legacy-id"
            assert notifications[-1].payload["question_id"] == "q-desktop-1"
            assert notifications[-1].payload["question"] == "Нужен выбор?"
            assert notifications[-1].payload["options"] == ["Да", "Нет"]
            assert notifications[-1].payload["allow_custom"] is False
        finally:
            unsubscribe()
            await facade.shutdown()


@pytest.mark.asyncio
async def test_facade_reload_runtime_config_delegates_to_bot_app(tmp_path: Path) -> None:
    """ApplicationFacade.reload_runtime_config() delegates to bot_app.reload_runtime_config."""
    from unittest.mock import AsyncMock, MagicMock, patch

    runtime = _build_desktop_facade_runtime(tmp_path, intent="reload_runtime")
    facade: ApplicationFacade = runtime["facade"]

    expected = {"status": "ok", "applied": ["defaults.idle_timeout_sec"], "restart_required": []}
    fake_bot_app = MagicMock()
    fake_bot_app.reload_runtime_config = AsyncMock(return_value=expected)

    with patch.object(facade, "_desktop_bot_app", return_value=fake_bot_app):
        result = await facade.reload_runtime_config()

    fake_bot_app.reload_runtime_config.assert_awaited_once()
    assert result == expected


@pytest.mark.asyncio
async def test_facade_reload_runtime_config_returns_error_when_unavailable(tmp_path: Path) -> None:
    """ApplicationFacade.reload_runtime_config() returns error dict when bot_app lacks the method."""
    from unittest.mock import MagicMock, patch

    runtime = _build_desktop_facade_runtime(tmp_path, intent="reload_runtime_unavail")
    facade: ApplicationFacade = runtime["facade"]

    fake_bot_app = MagicMock(spec=[])  # no reload_runtime_config attribute

    with patch.object(facade, "_desktop_bot_app", return_value=fake_bot_app):
        result = await facade.reload_runtime_config()

    assert result.get("status") == "error"
    assert "reload_runtime_config not available" in result.get("warnings", [""])[0]
