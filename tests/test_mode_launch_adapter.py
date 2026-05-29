from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import time
from urllib.parse import quote

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from app.security import SecurityFacade
from app.security.audit import EventBusAuditService
from app.services.actor_identity import miniapp_actor_id
from app.services import ConfigService, SessionService, TaskService
from app.services.config_service import ConfigProvider
from app.events.bus import (
    MiniAppCommandEvent,
    ModeLaunchCompletedEvent,
    ScheduledJobEvent,
    SystemEventBus,
    WebhookReceivedEvent,
)
from app.services.mode_launch_adapter import ModeLaunchAdapterService, ModeLaunchPolicy
from app.services.project_registry import ProjectRegistry
from app.services.shared_http_ingress import SharedHttpIngress
from app.services.webhook_delivery_repository import WebhookDeliveryRepository
from app.services.webhook_ingress_service import WebhookIngressService
from bot import BotApp
from desktop.services.application_facade import ApplicationFacade
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    TelegramConfig,
    ToolConfig,
    WebhooksConfig,
)
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ModeInputRoutingService, ModeRegistryService, ToolResult
from miniapp.routes import MiniAppRoutes
from session import SessionManager, session_runtime_uid
from sessions.conversation_scope import ConversationScope
from sessions.session_state_access import get_active_mode


class _CaptureMode(BaseMode):
    def __init__(self, mode_id: str, calls: list[dict]) -> None:
        super().__init__()
        self.mode_id = mode_id
        self._calls = calls

    async def handle_input(self, message, ctx):
        context = ctx.get("context")
        launch_request = getattr(context, "launch_request", None)
        if launch_request is None and hasattr(context, "raw_context"):
            launch_request = getattr(context.raw_context, "launch_request", None)
        self._calls.append(
            {
                "mode_id": self.mode_id,
                "text": str(message.text or ""),
                "session_id": str(getattr(ctx.get("session"), "id", "") or ""),
                "session_uid": session_runtime_uid(ctx.get("session")),
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


def _build_config(tmp_path: Path, *, intent: str, secret_token: str = "secret-token") -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[101], admlist_chat_ids=[101]),
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
        webhooks=WebhooksConfig(
            enabled=True,
            path="/webhooks/telegram",
            secret_token=secret_token,
            max_payload_bytes=4096,
        ),
    )


def _security_from_bot_app(bot_app) -> SecurityFacade:
    return SecurityFacade.from_app_config(
        getattr(bot_app, "config", None),
        is_admin_fn=getattr(bot_app, "is_admin", lambda _chat_id: False),
        is_user_fn=getattr(bot_app, "is_user", lambda _chat_id: False),
        system_event_bus=getattr(bot_app, "system_event_bus", None),
    )


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


def _build_runtime(
    tmp_path: Path,
    *,
    intent: str,
    allowlist: dict[str, set[str]],
    mode_id: str = "capture",
) -> dict[str, object]:
    cfg = _build_config(tmp_path, intent=intent)
    manager = SessionManager(cfg)
    registry = ModeRegistry()
    registry_service = ModeRegistryService(registry)
    calls: list[dict] = []
    registry.register(_CaptureMode(mode_id, calls))
    router = ModeInputRoutingService(
        mode_registry=registry_service,
        dialogs=None,
        send_message=None,
        send_output=None,
    )
    bus = SystemEventBus()
    project_registry = ProjectRegistry(cfg.defaults.state_path)
    workdir = Path(cfg.defaults.workdir) / "project"
    workdir.mkdir(parents=True, exist_ok=True)
    session = manager.create(101, "dummy", str(workdir))
    session.name = f"Session {intent}"
    session.conversation_scope = ConversationScope.from_parts(-100777000111, 101)
    manager.persist_session(101, session.id)
    project = project_registry.register_project(path=str(workdir), owner_id=101, name=session.name)
    bot_app = SimpleNamespace(
        config=cfg,
        manager=manager,
        mode_registry_service=registry_service,
        mode_input_router=router,
        system_event_bus=bus,
        project_registry=project_registry,
        container=SimpleNamespace(config_service=SimpleNamespace()),
        _last_delivery_error=None,
    )
    bot_app.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
    bot_app.webhook_delivery_repository = WebhookDeliveryRepository(cfg.defaults.state_path)
    bot_app.mode_launch_adapter = ModeLaunchAdapterService(
        bot_app,
        policy=ModeLaunchPolicy(allowlist),
    )
    return {
        "config": cfg,
        "bus": bus,
        "calls": calls,
        "session": session,
        "project": project,
        "bot_app": bot_app,
    }


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


def _build_init_data(bot_token: str, *, user_id: int = 101) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps(
            {"id": user_id, "username": "admin", "first_name": "Admin"},
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}"
        f"&hash={signature}"
    )


def test_scheduler_events_launch_allowed_mode_and_keep_requests_isolated(tmp_path) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="scheduler_launch",
        allowlist={"scheduler": {"capture"}},
    )

    async def _run() -> None:
        bot_app = runtime["bot_app"]
        bus = runtime["bus"]
        session = runtime["session"]
        project = runtime["project"]
        await bot_app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

        await bus.publish(
            ScheduledJobEvent(
                job_id="job-a",
                job_name="Scheduler A",
                status="manual",
                scheduled_for=1.0,
                cron="*/5 * * * *",
                target_mode="capture",
                owner_id=101,
                notification_target={"telegram_session_uid": session_runtime_uid(session)},
                payload={"prompt": "run alpha", "project_slug": project.slug},
            )
        )
        await bus.publish(
            ScheduledJobEvent(
                job_id="job-b",
                job_name="Scheduler B",
                status="manual",
                scheduled_for=2.0,
                cron="*/5 * * * *",
                target_mode="capture",
                owner_id=101,
                notification_target={"telegram_session_uid": session_runtime_uid(session)},
                payload={"prompt": "run beta", "project_slug": project.slug},
            )
        )

        assert [item["text"] for item in runtime["calls"]] == ["run alpha", "run beta"]
        assert [item["origin_key"] for item in runtime["calls"]] == ["scheduler", "scheduler"]
        assert [item["project_slug"] for item in runtime["calls"]] == [project.slug, project.slug]
        assert [item["dest"]["message_thread_id"] for item in runtime["calls"]] == [101, 101]
        assert str(get_active_mode(session, "") or "") == "capture"

        await bot_app.mode_launch_adapter.stop()

    asyncio.run(_run())


def test_scheduler_events_launch_keep_payloads_isolated_and_preserve_project_metadata(tmp_path) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="scheduler_payload_contract",
        allowlist={"scheduler": {"capture"}},
    )

    async def _run() -> None:
        bot_app = runtime["bot_app"]
        bus = runtime["bus"]
        session = runtime["session"]
        project = runtime["project"]
        mode = bot_app.mode_registry_service.get("capture")
        captured_requests: list[dict[str, object]] = []
        original_handle_input = mode.handle_input

        async def _capture_handle_input(message, ctx):  # type: ignore[no-untyped-def]
            context = ctx.get("context")
            launch_request = getattr(context, "launch_request", None)
            if launch_request is None and hasattr(context, "raw_context"):
                launch_request = getattr(context.raw_context, "launch_request", None)
            captured_requests.append(
                {
                    "prompt": str(message.text or ""),
                    "payload": dict(getattr(launch_request, "payload", {}) or {}),
                    "project": dict(getattr(launch_request, "project", {}) or {}),
                }
            )
            return await original_handle_input(message, ctx)

        mode.handle_input = _capture_handle_input
        await bot_app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

        payload_alpha = {
            "prompt": "run alpha payload",
            "project_slug": project.slug,
            "project": {"name": "Alpha", "branch": "main"},
            "intent": {"kind": "digest", "params": {"limit": 3, "sections": ["summary"]}},
            "launch": {"dry_run": False, "inputs": [{"kind": "note", "path": "notes/alpha.md"}]},
        }
        payload_beta = {
            "prompt": "run beta payload",
            "project_slug": project.slug,
            "project": {"name": "Alpha", "branch": "release"},
            "intent": {"kind": "audit", "params": {"limit": 1, "sections": ["checks"]}},
            "launch": {"dry_run": False, "inputs": [{"kind": "note", "path": "notes/beta.md"}]},
        }

        await bus.publish(
            ScheduledJobEvent(
                job_id="job-alpha-payload",
                job_name="Scheduler payload alpha",
                status="manual",
                scheduled_for=1.0,
                cron="*/5 * * * *",
                target_mode="capture",
                owner_id=101,
                notification_target={"telegram_session_uid": session_runtime_uid(session)},
                payload=payload_alpha,
            )
        )
        await bus.publish(
            ScheduledJobEvent(
                job_id="job-beta-payload",
                job_name="Scheduler payload beta",
                status="manual",
                scheduled_for=2.0,
                cron="*/5 * * * *",
                target_mode="capture",
                owner_id=101,
                notification_target={"telegram_session_uid": session_runtime_uid(session)},
                payload=payload_beta,
            )
        )

        assert captured_requests == [
            {
                "prompt": "run alpha payload",
                "payload": payload_alpha,
                "project": {"name": "Alpha", "branch": "main", "slug": project.slug},
            },
            {
                "prompt": "run beta payload",
                "payload": payload_beta,
                "project": {"name": "Alpha", "branch": "release", "slug": project.slug},
            },
        ]
        assert [item["text"] for item in runtime["calls"]] == ["run alpha payload", "run beta payload"]
        assert [item["project_slug"] for item in runtime["calls"]] == [project.slug, project.slug]

        await bot_app.mode_launch_adapter.stop()

    asyncio.run(_run())


def test_disallowed_webhook_launch_logs_deny_reason_and_skips_mode(tmp_path, caplog) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="webhook_deny",
        allowlist={"webhook:telegram": {"capture"}},
    )

    async def _run() -> None:
        bot_app = runtime["bot_app"]
        session = runtime["session"]
        await bot_app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))
        await runtime["bus"].publish(
            WebhookReceivedEvent(
                source="telegram",
                path="/webhooks/telegram",
                method="POST",
                payload={
                    "mode_id": "forbidden",
                    "prompt": "blocked",
                    "notification_target": {"telegram_session_uid": session_runtime_uid(session)},
                },
            )
        )
        await bot_app.mode_launch_adapter.stop()

    caplog.set_level(logging.WARNING)
    asyncio.run(_run())

    assert runtime["calls"] == []
    assert any("event mode launch denied reason=mode_not_allowlisted" in record.message for record in caplog.records)


def test_webhook_event_launch_allowlist_parity(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="webhook_allowlist_parity", secret_token="webhook-secret")
        cfg.telegram.whitelist_chat_ids = [101]
        cfg.telegram.admlist_chat_ids = []
        cfg.telegram.user_modes = {101: ["agent"]}
        workdir = Path(cfg.defaults.workdir) / "webhook-allowlist-project"
        workdir.mkdir(parents=True, exist_ok=True)
        cfg.telegram.user_workdirs = {101: [str(workdir)]}

        app = BotApp(cfg)
        app.mode_registry.register(_CaptureMode("capture", []))
        calls = app.mode_registry.get("capture")._calls
        app.mode_launch_adapter = ModeLaunchAdapterService(app)

        audits: list[dict] = []
        completed: list[ModeLaunchCompletedEvent] = []
        callback_sent: list[tuple[int, str]] = []

        async def _capture_audit(_event_name: str, payload: dict) -> None:
            audits.append(dict(payload))

        async def _capture_completed(event: ModeLaunchCompletedEvent) -> None:
            completed.append(event)

        async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
            callback_sent.append((int(chat_id), str(text or "")))
            return True

        app.system_event_bus.subscribe(EventBusAuditService.EVENT_NAME, _capture_audit)
        app.system_event_bus.subscribe(ModeLaunchCompletedEvent, _capture_completed)
        app.mode_callback_router.send_message = _send_message

        session = app.manager.create(101, "dummy", str(workdir))
        session.name = "Webhook Allowlist Session"
        session.conversation_scope = ConversationScope.from_parts(-100777000111, 101)
        project = app.project_registry.register_project(
            path=str(workdir),
            owner_id=101,
            name="Webhook Allowlist Project",
        )

        assert app.access_policy_service.is_mode_allowed_for_chat(101, "capture") is False

        await app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

        handled = await app.mode_callback_router.handle_mode_action_callback(
            data="ma:capture:enable",
            chat_id=101,
            query=SimpleNamespace(
                from_user=SimpleNamespace(id=101),
                message=SimpleNamespace(chat_id=101, message_id=701),
            ),
            context=object(),
            bot_app=app,
        )
        assert handled is True
        assert callback_sent == [(101, "Режим недоступен для вашего пользователя.")]
        assert calls == []

        await app.system_event_bus.publish(
            WebhookReceivedEvent(
                source="telegram",
                path="/webhooks/telegram",
                method="POST",
                correlation_id="webhook-allowlist-corr",
                payload={
                    "mode_id": "capture",
                    "prompt": "webhook denied launch",
                    "notification_target": {"telegram_session_uid": session_runtime_uid(session)},
                    "project_slug": project.slug,
                    "actor": {
                        "kind": "webhook",
                        "chat_id": 101,
                        "actor_id": "telegram:101",
                    },
                },
            )
        )
        await app.mode_launch_adapter.stop()

        assert calls == []
        assert len(completed) == 1
        assert completed[0].status == "denied"
        assert completed[0].mode_id == "capture"
        assert completed[0].session_uid == session_runtime_uid(session)
        assert completed[0].result == {"error": "mode_not_allowlisted"}

        mode_launch_audits = [payload for payload in audits if payload.get("category") == "mode_launch"]
        assert len(mode_launch_audits) == 2

        callback_audit = next(payload for payload in mode_launch_audits if payload.get("action") == "enable")
        webhook_audit = next(payload for payload in mode_launch_audits if payload.get("action") == "event_launch")

        assert callback_audit["status"] == "denied"
        assert callback_audit["reason"] == "mode_not_allowed"
        assert callback_audit["subject"] == "capture"
        assert callback_audit["context"]["chat_id"] == 101

        assert webhook_audit["status"] == "denied"
        assert webhook_audit["reason"] == "mode_not_allowed"
        assert webhook_audit["subject"] == "capture"
        assert webhook_audit["context"]["chat_id"] == 101
        assert webhook_audit["context"]["actor_id"] == "telegram:101"
        assert webhook_audit["context"]["origin"] == "webhook:telegram"
        assert webhook_audit["context"]["session_id"] == session_runtime_uid(session)
        assert webhook_audit["user_id"] == "telegram:101"

    asyncio.run(_run())


def test_miniapp_event_launch_origin_not_allowlisted_emits_diagnostic_audit(tmp_path) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="miniapp_origin_deny",
        allowlist={"scheduler": {"capture"}},
    )

    async def _run() -> None:
        audits: list[dict] = []
        completed: list[ModeLaunchCompletedEvent] = []
        bot_app = runtime["bot_app"]
        bus = runtime["bus"]
        session = runtime["session"]
        project = runtime["project"]

        async def _capture_audit(_event_name: str, payload: dict) -> None:
            audits.append(dict(payload))

        async def _capture_completed(event: ModeLaunchCompletedEvent) -> None:
            completed.append(event)

        bot_app.is_admin = lambda _chat_id: False
        bot_app.is_user = lambda _chat_id: True
        bot_app.security = _security_from_bot_app(bot_app)
        bus.subscribe(EventBusAuditService.EVENT_NAME, _capture_audit)
        bus.subscribe(ModeLaunchCompletedEvent, _capture_completed)

        await bot_app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))
        await bus.publish(
            MiniAppCommandEvent(
                user_id="telegram:101",
                session_uid=session_runtime_uid(session),
                project_slug=str(project.slug),
                command="capture",
                correlation_id="miniapp-origin-deny",
                payload={
                    "mode_id": "capture",
                    "prompt": "should be denied by origin",
                    "actor": {
                        "kind": "miniapp",
                        "user_id": 101,
                        "actor_id": "telegram:101",
                    },
                },
            )
        )
        await bot_app.mode_launch_adapter.stop()

        assert runtime["calls"] == []
        assert len(completed) == 1
        assert completed[0].status == "denied"
        assert completed[0].result == {"error": "origin_not_allowlisted"}
        assert audits[-1]["category"] == "mode_launch"
        assert audits[-1]["action"] == "event_launch"
        assert audits[-1]["status"] == "denied"
        assert audits[-1]["reason"] == "origin_not_allowlisted"
        assert audits[-1]["context"]["origin"] == "miniapp"

    asyncio.run(_run())


def test_miniapp_event_launch_actor_unresolved_emits_diagnostic_audit(tmp_path) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="miniapp_actor_unresolved",
        allowlist={"miniapp": {"capture"}},
    )

    async def _run() -> None:
        audits: list[dict] = []
        completed: list[ModeLaunchCompletedEvent] = []
        bot_app = runtime["bot_app"]
        bus = runtime["bus"]
        session = runtime["session"]
        project = runtime["project"]

        async def _capture_audit(_event_name: str, payload: dict) -> None:
            audits.append(dict(payload))

        async def _capture_completed(event: ModeLaunchCompletedEvent) -> None:
            completed.append(event)

        bot_app.is_admin = lambda _chat_id: False
        bot_app.is_user = lambda _chat_id: True
        bot_app.security = _security_from_bot_app(bot_app)
        bot_app.access_policy_service = SimpleNamespace(
            is_mode_allowed_for_chat=lambda _chat_id, _mode_id: True,
        )
        bus.subscribe(EventBusAuditService.EVENT_NAME, _capture_audit)
        bus.subscribe(ModeLaunchCompletedEvent, _capture_completed)

        await bot_app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))
        await bus.publish(
            MiniAppCommandEvent(
                user_id="",
                session_uid=session_runtime_uid(session),
                project_slug=str(project.slug),
                command="capture",
                correlation_id="miniapp-actor-unresolved",
                payload={
                    "mode_id": "capture",
                    "prompt": "missing actor id",
                    "actor": {
                        "kind": "miniapp",
                        "user_id": 101,
                    },
                },
            )
        )
        await bot_app.mode_launch_adapter.stop()

        assert runtime["calls"] == []
        assert len(completed) == 1
        assert completed[0].status == "denied"
        assert completed[0].result == {"error": "actor_unresolved"}
        assert audits[-1]["category"] == "mode_launch"
        assert audits[-1]["action"] == "event_launch"
        assert audits[-1]["status"] == "denied"
        assert audits[-1]["reason"] == "actor_unresolved"
        assert audits[-1]["context"]["origin"] == "miniapp"
        assert audits[-1]["context"]["chat_id"] == 0

    asyncio.run(_run())


def test_webhook_ingress_event_triggers_mode_launch_integration(tmp_path) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="webhook_integration",
        allowlist={"webhook:telegram": {"capture"}},
    )

    async def _run() -> None:
        bot_app = runtime["bot_app"]
        session = runtime["session"]
        project = runtime["project"]
        adapter = bot_app.mode_launch_adapter
        ingress_service = WebhookIngressService(bot_app)

        await adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))
        await ingress_service.start()
        await bot_app.shared_http_ingress.start()
        port = bot_app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={
                    "mode_id": "capture",
                    "prompt": "launch from webhook",
                    "notification_target": {"telegram_session_uid": session_runtime_uid(session)},
                    "project_slug": project.slug,
                    "actor": {"kind": "webhook", "user_id": 5001},
                },
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-evt-5",
                    "X-Correlation-Id": "corr-launch-1",
                },
            )
            assert response.status == 202
            payload = await response.json()
            assert payload == {"ok": True, "provider": "telegram", "duplicate": False}

        assert runtime["calls"] == [
            {
                "mode_id": "capture",
                "text": "launch from webhook",
                "session_id": str(session.id),
                "session_uid": session_runtime_uid(session),
                "dest": {
                    "kind": "telegram",
                    "chat_id": -100777000111,
                    "message_thread_id": 101,
                    "user_id": 5001,
                },
                "project_slug": str(project.slug),
                "origin_key": "webhook:telegram",
                "actor": {"kind": "webhook", "user_id": 5001},
            }
        ]

        await ingress_service.stop()
        await bot_app.shared_http_ingress.stop()
        await adapter.stop()

    asyncio.run(_run())


def test_external_launch_logs_correlation_chain_and_dry_run_skip(tmp_path, caplog) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="webhook_dry_run",
        allowlist={"webhook:telegram": {"capture"}},
    )

    async def _run() -> None:
        bot_app = runtime["bot_app"]
        session = runtime["session"]
        project = runtime["project"]
        adapter = bot_app.mode_launch_adapter
        ingress_service = WebhookIngressService(bot_app)

        await adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))
        await ingress_service.start()
        await bot_app.shared_http_ingress.start()
        port = bot_app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={
                    "mode_id": "capture",
                    "prompt": "dry run launch",
                    "project_slug": project.slug,
                    "dry_run": True,
                    "notification_target": {"telegram_session_uid": session_runtime_uid(session)},
                },
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-dry",
                    "X-Correlation-Id": "corr-dry-1",
                },
            )
            assert response.status == 202

        assert runtime["calls"] == []
        messages = [record.message for record in caplog.records]
        assert any(
            "webhook ingress accepted provider=telegram correlation_id=corr-dry-1" in message
            for message in messages
        )
        assert any(
            "event mode launch dry_run skipped correlation_id=corr-dry-1 "
            "origin=webhook:telegram provider=telegram" in message
            for message in messages
        )

        await ingress_service.stop()
        await bot_app.shared_http_ingress.stop()
        await adapter.stop()

    caplog.clear()
    caplog.set_level(logging.INFO, logger="miniapp")
    caplog.set_level(logging.INFO, logger="app.services.mode_launch_adapter")

    miniapp_logger = logging.getLogger("miniapp")
    adapter_logger = logging.getLogger("app.services.mode_launch_adapter")
    old_miniapp_level = miniapp_logger.level
    old_adapter_level = adapter_logger.level
    miniapp_logger.addHandler(caplog.handler)
    adapter_logger.addHandler(caplog.handler)
    miniapp_logger.setLevel(logging.INFO)
    adapter_logger.setLevel(logging.INFO)
    try:
        asyncio.run(_run())
    finally:
        miniapp_logger.removeHandler(caplog.handler)
        adapter_logger.removeHandler(caplog.handler)
        miniapp_logger.setLevel(old_miniapp_level)
        adapter_logger.setLevel(old_adapter_level)


def test_desktop_facade_publish_mode_launch_request_triggers_mode_via_system_bus(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_launch")

    async def _run() -> None:
        facade = runtime["facade"]
        cfg = runtime["config"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)
        cfg.telegram.whitelist_chat_ids = [101]
        cfg.telegram.admlist_chat_ids = []
        cfg.telegram.user_workdirs = {101: [str(session.workdir)]}
        cfg.telegram.user_modes = {101: ["capture"]}
        await facade.start(validate_secrets=False)
        facade._desktop_identity_provider_service().resolve_mode_launch_actor_chat_id = lambda _session: 101
        project_slug = facade.resolve_scheduler_project_slug(session.id)
        assert project_slug is not None

        response = await facade.publish_mode_launch_request(
            project_slug=str(project_slug),
            session_uid=session_uid,
            mode_id="capture",
            prompt="launch from desktop",
        )

        assert response["ok"] is True
        assert response["queued"] is True
        assert response["project_slug"] == str(project_slug)
        assert response["session_uid"] == session_uid

        assert runtime["calls"] == [
            {
                "mode_id": "capture",
                "text": "launch from desktop",
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

        await facade.shutdown()

    asyncio.run(_run())


def test_desktop_facade_start_starts_scheduler_before_adapter_and_skips_double_start(tmp_path, caplog) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_scheduler_start")
    cfg = runtime["config"]
    cfg.scheduler.enabled = True
    cfg.scheduler.tick_interval_sec = 60

    async def _run() -> None:
        facade = runtime["facade"]
        facade.config = cfg
        scheduler = facade._desktop_scheduler_service_instance()
        adapter = facade._desktop_mode_launch_adapter_instance()
        scheduler_start_calls = 0
        call_order: list[str] = []

        real_scheduler_start = scheduler.start
        real_adapter_start = adapter.start

        async def _scheduler_start() -> None:
            nonlocal scheduler_start_calls
            scheduler_start_calls += 1
            assert facade._desktop_system_event_bus is not None
            call_order.append("scheduler")
            await real_scheduler_start()

        async def _adapter_start(*, application=None) -> None:
            assert scheduler_start_calls == 1
            call_order.append("adapter")
            await real_adapter_start(application=application)

        scheduler.start = _scheduler_start
        adapter.start = _adapter_start
        try:
            await facade.start(validate_secrets=False)
            await facade._ensure_desktop_event_runtime_started()

            assert scheduler_start_calls == 1
            assert call_order[:2] == ["scheduler", "adapter"]
        finally:
            await facade.shutdown()

    caplog.set_level(logging.WARNING, logger="desktop.services.application_facade")
    caplog.set_level(logging.WARNING, logger="app.services.scheduler_service")
    asyncio.run(_run())

    scheduler_warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "scheduler" in f"{record.name} {record.message}".lower()
    ]
    assert scheduler_warnings == []


def test_desktop_facade_scheduler_autostarts_and_dispatches_jobs_when_enabled(tmp_path) -> None:
    runtime = _build_desktop_facade_runtime(tmp_path, intent="desktop_scheduler_job")
    cfg = runtime["config"]
    cfg.scheduler.enabled = True
    cfg.scheduler.tick_interval_sec = 60

    async def _run() -> None:
        facade = runtime["facade"]
        session = runtime["session"]
        session_uid = session_runtime_uid(session)

        await facade.start(validate_secrets=False)
        scheduler = facade._desktop_scheduler_service
        assert scheduler is not None
        assert scheduler._runner_task is not None
        assert not scheduler._runner_task.done()

        project_slug = facade.resolve_scheduler_project_slug(session.id)
        assert project_slug is not None
        targets = facade.list_scheduler_notification_targets(project_slug=str(project_slug))
        assert len(targets) == 1
        assert targets[0]["session_id"] == str(session.id)
        assert targets[0]["session_uid"] == session_uid
        assert targets[0]["workdir"] == str(session.workdir)
        assert targets[0]["project_slug"] == str(project_slug)
        assert str(session.id) in str(targets[0]["label"])
        assert str(session.name) in str(targets[0]["label"])

        created = facade.create_scheduler_job(
            project_slug=str(project_slug),
            cron="* * * * *",
            target_mode="capture",
            notification_target_session_uid=session_uid,
            job_name="Desktop Scheduled Launch",
            payload={"prompt": "launch from desktop scheduler"},
        )

        emitted = await scheduler.run_due_jobs(now=float(created["next_run_at"]))
        assert [event.job_id for event in emitted] == [str(created["job_id"])]

        assert runtime["calls"] == [
            {
                "mode_id": "capture",
                "text": "launch from desktop scheduler",
                "session_id": str(session.id),
                "session_uid": session_uid,
                "dest": {
                    "kind": "desktop",
                    "chat_id": session_uid,
                    "session_id": session_uid,
                },
                "project_slug": str(project_slug),
                "origin_key": "scheduler",
                "actor": {
                    "kind": "scheduler",
                    "owner_id": "desktop:default",
                },
            }
        ]

        await facade.shutdown()

    asyncio.run(_run())


def test_miniapp_route_launches_mode_via_system_bus_with_canonical_session_uid(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="miniapp_launch", secret_token="miniapp-secret")
        cfg.telegram.whitelist_chat_ids = [101]
        cfg.telegram.admlist_chat_ids = [101]
        cfg.miniapp.enabled = True
        app = BotApp(cfg)
        app.mode_registry.register(_CaptureMode("capture", []))
        calls = app.mode_registry.get("capture")._calls
        app.mode_launch_adapter = ModeLaunchAdapterService(app)

        workdir = Path(cfg.defaults.workdir) / "miniapp-project"
        workdir.mkdir(parents=True, exist_ok=True)
        session = app.manager.create(101, "dummy", str(workdir))
        session.name = "MiniApp Session"
        session_uid = session_runtime_uid(session)
        project = app.project_registry.register_project(
            path=str(workdir),
            owner_id=miniapp_actor_id(101),
            name="MiniApp Project",
        )

        await app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data(cfg.telegram.token, user_id=101)}
            response = await client.post(
                "/api/v1/modes/launch",
                headers=headers,
                json={
                    "project_slug": project.slug,
                    "mode_id": "capture",
                    "prompt": "launch from miniapp",
                    "session_uid": session_uid,
                },
            )
            assert response.status == 202
            payload = await response.json()
            assert payload["ok"] is True
            assert payload["queued"] is True
            assert payload["project_slug"] == project.slug
            assert payload["session_uid"] == session_uid

            assert calls == [
                {
                    "mode_id": "capture",
                    "text": "launch from miniapp",
                    "session_id": str(session.id),
                    "session_uid": session_uid,
                    "dest": {
                        "kind": "miniapp",
                        "chat_id": 101,
                        "user_id": 101,
                    },
                    "project_slug": str(project.slug),
                    "origin_key": "miniapp",
                    "actor": {
                        "kind": "miniapp",
                        "user_id": 101,
                        "actor_id": "telegram:101",
                    },
                }
            ]
        finally:
            await client.close()
            await server.close()
            await app.mode_launch_adapter.stop()

    asyncio.run(_run())


def test_desktop_facade_publish_mode_launch_request_triggers_event_bus_integration(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="desktop_launch")
    manager = SessionManager(cfg)
    task_service = TaskService()
    session_service = SessionService(manager, task_service)
    registry = ModeRegistry()
    registry_service = ModeRegistryService(registry)
    calls: list[dict] = []
    registry.register(_CaptureMode("capture", calls))
    router = ModeInputRoutingService(
        mode_registry=registry_service,
        dialogs=None,
        send_message=None,
        send_output=None,
    )
    bus = SystemEventBus()
    bot_app = SimpleNamespace(
        config=cfg,
        manager=manager,
        mode_registry_service=registry_service,
        mode_input_router=router,
        system_event_bus=bus,
        _last_delivery_error=None,
    )
    bot_app.is_admin = lambda _chat_id: False
    bot_app.is_user = lambda _chat_id: False
    bot_app.security = _security_from_bot_app(bot_app)
    adapter = ModeLaunchAdapterService(
        bot_app,
        policy=ModeLaunchPolicy({"desktop": {"capture"}}),
    )
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
        mode_registry_service=registry_service,
    )
    facade.config = cfg
    facade._desktop_system_event_bus = bus

    workdir = Path(cfg.defaults.workdir) / "desktop-project"
    workdir.mkdir(parents=True, exist_ok=True)
    session = session_service.create_desktop_session("dummy", str(workdir))
    session.name = "Desktop Launch Session"
    session_uid = session_runtime_uid(session)
    cfg.telegram.whitelist_chat_ids = [101]
    cfg.telegram.admlist_chat_ids = []
    cfg.telegram.user_workdirs = {101: [str(workdir)]}
    cfg.telegram.user_modes = {101: ["capture"]}
    project_slug = facade.resolve_scheduler_project_slug(session.id)
    assert project_slug is not None

    async def _run() -> None:
        await adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))
        facade._desktop_identity_provider_service().resolve_mode_launch_actor_chat_id = lambda _session: 101

        result_first = await facade.publish_mode_launch_request(
            project_slug=str(project_slug),
            session_uid=session_uid,
            mode_id="capture",
            prompt="desktop alpha",
            correlation_id="desktop-corr-1",
        )
        result_second = await facade.publish_mode_launch_request(
            project_slug=str(project_slug),
            session_uid=session_uid,
            mode_id="capture",
            prompt="desktop beta",
            correlation_id="desktop-corr-2",
        )

        assert result_first["queued"] is True
        assert result_first["session_uid"] == session_uid
        assert result_second["queued"] is True
        assert result_second["session_uid"] == session_uid
        assert [item["text"] for item in calls] == ["desktop alpha", "desktop beta"]
        assert [item["origin_key"] for item in calls] == ["desktop", "desktop"]
        assert [item["project_slug"] for item in calls] == [project_slug, project_slug]
        assert [item["session_uid"] for item in calls] == [
            session_uid,
            session_uid,
        ]
        assert [item["dest"] for item in calls] == [
            {
                "kind": "desktop",
                "chat_id": session_uid,
                "session_id": session_uid,
            },
            {
                "kind": "desktop",
                "chat_id": session_uid,
                "session_id": session_uid,
            },
        ]
        assert calls[0]["actor"] == {
            "kind": "desktop",
            "actor_id": "desktop:default",
            "chat_id": 101,
            "owner_id": "desktop:default",
        }
        assert str(get_active_mode(session, "") or "") == "capture"

        await adapter.stop()

    asyncio.run(_run())


def test_miniapp_route_modes_launch_triggers_event_bus_integration(tmp_path) -> None:
    runtime = _build_runtime(
        tmp_path,
        intent="miniapp_launch",
        allowlist={"miniapp": {"capture"}},
    )

    async def _run() -> None:
        bot_app = runtime["bot_app"]
        session = runtime["session"]
        project = runtime["project"]
        bot_app.security = SimpleNamespace(
            authenticate=lambda credentials, strategy=None: SimpleNamespace(
                authenticated=True,
                reason="",
                claims={"user_id": 101, "username": "admin", "first_name": "Admin"},
            ),
            authorize=lambda chat_id, scope="generic", require_admin=False: SimpleNamespace(
                allowed=True,
                reason="",
                is_admin=True,
                is_user=True,
            ),
        )

        adapter = bot_app.mode_launch_adapter
        await adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

        web_app = web.Application()
        MiniAppRoutes(bot_app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data(bot_app.config.telegram.token, user_id=101)}
            first = await client.post(
                "/api/v1/modes/launch",
                headers=headers,
                json={
                    "project_slug": project.slug,
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "capture",
                    "prompt": "miniapp alpha",
                    "correlation_id": "miniapp-corr-1",
                },
            )
            second = await client.post(
                "/api/v1/modes/launch",
                headers=headers,
                json={
                    "project_slug": project.slug,
                    "session_uid": session_runtime_uid(session),
                    "mode_id": "capture",
                    "prompt": "miniapp beta",
                    "correlation_id": "miniapp-corr-2",
                },
            )

            first_payload = await first.json()
            second_payload = await second.json()
            assert first.status == 202
            assert second.status == 202
            assert first_payload == {
                "ok": True,
                "queued": True,
                "mode_id": "capture",
                "project_slug": str(project.slug),
                "session_uid": session_runtime_uid(session),
                "correlation_id": "miniapp-corr-1",
            }
            assert second_payload == {
                "ok": True,
                "queued": True,
                "mode_id": "capture",
                "project_slug": str(project.slug),
                "session_uid": session_runtime_uid(session),
                "correlation_id": "miniapp-corr-2",
            }

            assert [item["text"] for item in runtime["calls"]] == ["miniapp alpha", "miniapp beta"]
            assert [item["origin_key"] for item in runtime["calls"]] == ["miniapp", "miniapp"]
            assert [item["project_slug"] for item in runtime["calls"]] == [project.slug, project.slug]
            assert [item["session_uid"] for item in runtime["calls"]] == [
                session_runtime_uid(session),
                session_runtime_uid(session),
            ]
            assert [item["dest"] for item in runtime["calls"]] == [
                {"kind": "miniapp", "chat_id": -100777000111, "user_id": 101},
                {"kind": "miniapp", "chat_id": -100777000111, "user_id": 101},
            ]
            assert runtime["calls"][0]["actor"] == {
                "kind": "miniapp",
                "user_id": 101,
                "actor_id": "telegram:101",
            }
            assert str(get_active_mode(session, "") or "") == "capture"
        finally:
            await client.close()
            await server.close()
            await adapter.stop()

    asyncio.run(_run())


def test_miniapp_event_launch_allowlist_bypass_regression(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="miniapp_launch_allowlist_regression", secret_token="miniapp-secret")
        cfg.telegram.whitelist_chat_ids = [101]
        cfg.telegram.admlist_chat_ids = []
        cfg.telegram.user_modes = {101: ["agent"]}
        workdir = Path(cfg.defaults.workdir) / "miniapp-allowlist-project"
        workdir.mkdir(parents=True, exist_ok=True)
        cfg.telegram.user_workdirs = {101: [str(workdir)]}
        cfg.miniapp.enabled = True

        app = BotApp(cfg)
        app.mode_registry.register(_CaptureMode("capture", []))
        calls = app.mode_registry.get("capture")._calls
        app.mode_launch_adapter = ModeLaunchAdapterService(app)

        audits: list[dict] = []
        completed: list[ModeLaunchCompletedEvent] = []
        callback_sent: list[tuple[int, str]] = []

        async def _capture_audit(_event_name: str, payload: dict) -> None:
            audits.append(dict(payload))

        async def _capture_completed(event: ModeLaunchCompletedEvent) -> None:
            completed.append(event)

        async def _send_message(_context, *, chat_id: int, text: str, **_kwargs):
            callback_sent.append((int(chat_id), str(text or "")))
            return True

        app.system_event_bus.subscribe(EventBusAuditService.EVENT_NAME, _capture_audit)
        app.system_event_bus.subscribe(ModeLaunchCompletedEvent, _capture_completed)
        app.mode_callback_router.send_message = _send_message

        session = app.manager.create(101, "dummy", str(workdir))
        session.name = "MiniApp Allowlist Session"
        project = app.project_registry.register_project(
            path=str(workdir),
            owner_id=miniapp_actor_id(101),
            name="MiniApp Allowlist Project",
        )

        assert app.access_policy_service.is_mode_allowed_for_chat(101, "capture") is False

        await app.mode_launch_adapter.start(application=SimpleNamespace(bot=SimpleNamespace()))

        handled = await app.mode_callback_router.handle_mode_action_callback(
            data="ma:capture:enable",
            chat_id=101,
            query=SimpleNamespace(
                from_user=SimpleNamespace(id=101),
                message=SimpleNamespace(chat_id=101, message_id=501),
            ),
            context=object(),
            bot_app=app,
        )
        assert handled is True
        assert callback_sent == [(101, "Режим недоступен для вашего пользователя.")]
        assert calls == []

        web_app = web.Application()
        MiniAppRoutes(app).register(web_app)
        server = TestServer(web_app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            headers = {"X-Telegram-Init-Data": _build_init_data(cfg.telegram.token, user_id=101)}
            response = await client.post(
                "/api/v1/modes/launch",
                headers=headers,
                json={
                    "project_slug": project.slug,
                    "mode_id": "capture",
                    "prompt": "miniapp denied launch",
                    "session_uid": session_runtime_uid(session),
                    "correlation_id": "miniapp-allowlist-corr",
                },
            )
            payload = await response.json()
            assert response.status == 202
            assert payload == {
                "ok": True,
                "queued": True,
                "mode_id": "capture",
                "project_slug": str(project.slug),
                "session_uid": session_runtime_uid(session),
                "correlation_id": "miniapp-allowlist-corr",
            }
        finally:
            await client.close()
            await server.close()
            await app.mode_launch_adapter.stop()

        assert calls == []
        assert len(completed) == 1
        assert completed[0].status == "denied"
        assert completed[0].mode_id == "capture"
        assert completed[0].session_uid == session_runtime_uid(session)
        assert completed[0].result == {"error": "mode_not_allowlisted"}

        mode_launch_audits = [payload for payload in audits if payload.get("category") == "mode_launch"]
        assert len(mode_launch_audits) == 2

        callback_audit = next(payload for payload in mode_launch_audits if payload.get("action") == "enable")
        miniapp_audit = next(payload for payload in mode_launch_audits if payload.get("action") == "event_launch")

        assert callback_audit["status"] == "denied"
        assert callback_audit["reason"] == "mode_not_allowed"
        assert callback_audit["subject"] == "capture"
        assert callback_audit["context"]["chat_id"] == 101

        assert miniapp_audit["status"] == "denied"
        assert miniapp_audit["reason"] == "mode_not_allowed"
        assert miniapp_audit["subject"] == "capture"
        assert miniapp_audit["context"]["chat_id"] == 101
        assert miniapp_audit["context"]["actor_id"] == "telegram:101"
        assert miniapp_audit["context"]["origin"] == "miniapp"
        assert miniapp_audit["context"]["session_id"] == session_runtime_uid(session)
        assert miniapp_audit["user_id"] == "telegram:101"

    asyncio.run(_run())
