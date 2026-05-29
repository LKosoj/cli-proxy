from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import types
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.services.config_service import ConfigProvider, ConfigService
from app.services.run_doctor_service import RunDoctorReport
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from app.services.telegram_transport import TelegramTransportContext
from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from miniapp.routes import MiniAppRoutes
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ModeCallbackRouterService, ModeRegistryService, ToolResult
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


class _RecoveryMode:
    def __init__(self, *, progress_text: str, final_text: str) -> None:
        self.calls: list[dict] = []
        self.progress_text = str(progress_text)
        self.final_text = str(final_text)

    async def execute_recovery_action(
        self,
        *,
        session,
        action: str,
        run,
        state,
        report,
        bot_app,
        context,
        dest,
    ):
        self.calls.append(
            {
                "session_id": str(getattr(session, "id", "") or ""),
                "action": str(action or ""),
                "context": context,
                "dest": dict(dest or {}),
                "run_id": str(getattr(run, "run_id", "") or ""),
                "state": dict(state or {}),
                "report_action": str(getattr(report, "recommended_action", "") or ""),
            }
        )
        if context is not None:
            await bot_app._send_message(
                context,
                chat_id=dest.get("chat_id"),
                text=self.progress_text,
                md2=True,
                **(
                    {"message_thread_id": dest.get("message_thread_id")}
                    if dest.get("message_thread_id") is not None
                    else {}
                ),
            )
        return {
            "status": "ok",
            "message": self.final_text,
            "executed_operation": str(action or ""),
            "executed_via": "test_recovery_mode",
        }


def _build_config(tmp_path: Path) -> AppConfig:
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
            openai_api_key="k",
            openai_model="m",
            openai_big_model="m-big",
            run_artifacts_enabled=True,
            run_doctor_enabled=True,
            run_boundary_validation_enabled=True,
            run_metrics_enabled=True,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def _build_init_data(bot_token: str, user_id: int) -> str:
    payload = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps(
            {"id": int(user_id), "username": f"user-{int(user_id)}", "first_name": "Mini"},
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"


def _doctor_report(*, mode_id: str, action: str, phase: str = "plan") -> RunDoctorReport:
    return RunDoctorReport(
        mode_id=str(mode_id or ""),
        phase=str(phase or ""),
        status="needs_recovery",
        issues=[],
        recommended_action=str(action or ""),
        can_resume=False,
        diagnosed_at=1_710_000_000.0,
        last_consistent_checkpoint=0,
    )


def _run_operation_result(*, operation: str, mode_id: str, run_id: str, message: str = "OK") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        operation=str(operation),
        status="ok",
        mode_id=str(mode_id or ""),
        phase="execute",
        message=str(message),
        run_id=str(run_id or ""),
        recommended_action=None,
        blocked_by=(),
        report={"status": "ok"},
    )


class _ProbeMode(BaseMode):
    mode_id = "agent"

    async def handle_input(self, message, ctx):
        return ToolResult.ok()

    async def handle_callback(self, callback, ctx):
        raise AssertionError("shared run operation must not route to plugin callback")


class _FakeTelegramQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = types.SimpleNamespace(id=42)
        self.message = types.SimpleNamespace(chat_id=1, message_id=10, message_thread_id=None)


class _TelegramSurfaceBotApp:
    def __init__(self, session: object) -> None:
        self.session = session
        self.mode_registry = ModeRegistry()
        self.mode_registry.register(_ProbeMode())
        self.mode_registry_service = ModeRegistryService(self.mode_registry)
        self.access_policy_service = types.SimpleNamespace(
            is_mode_allowed_for_chat=lambda _chat_id, _mode_id: True,
            is_admin=lambda _chat_id, scope="generic": True,
        )
        self.mode_run_artifacts = object()

    def build_telegram_reply_dest(self, session, chat_id, *, user_id=None):
        dest = {"kind": "telegram", "chat_id": int(chat_id)}
        if user_id is not None:
            dest["user_id"] = int(user_id)
        return dest

    def build_telegram_transport_context(
        self,
        context,
        *,
        session,
        chat_id,
        dest=None,
        user_id=None,
        message_thread_id=None,
    ):
        _ = dest, user_id
        return TelegramTransportContext(
            raw_context=context,
            chat_id=int(chat_id),
            message_thread_id=message_thread_id,
            session_uid=session_runtime_uid(session),
        )


@pytest.mark.asyncio
async def test_bot_recover_run_preserves_live_telegram_context_for_progress_delivery(tmp_path, monkeypatch) -> None:
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path / "workdir"))
    session.modes.active_mode = "analyst"

    store = app.mode_run_operations.artifact_store
    run = store.start_run(session=session, mode_id="analyst", run_id="run_20260314T120000Z_vectorbot", phase="plan")
    store.save_state(run, {"phase": "plan", "status": "running", "mode_context": {}})
    app.mode_run_operations.doctor_service.diagnose = lambda *_a, **_k: _doctor_report(  # type: ignore[method-assign]
        mode_id="analyst",
        action="rollback_to_checkpoint",
    )

    mode = _RecoveryMode(
        progress_text="Telegram recovery progress",
        final_text="Telegram recovery completed.",
    )
    app.mode_registry_service = types.SimpleNamespace(get=lambda _mode_id: mode)

    sent: list[dict] = []

    async def _send_message(context, **kwargs):
        sent.append({"context": context, "kwargs": dict(kwargs)})
        return True

    monkeypatch.setattr(app, "_send_message", _send_message)

    raw_context = types.SimpleNamespace(bot=object())
    reply_dest = app.build_telegram_reply_dest(session, 1)
    transport_context = app.build_telegram_transport_context(
        raw_context,
        session=session,
        chat_id=1,
        dest=reply_dest,
    )

    result = await app.mode_run_operations.recover_run(
        session=session,
        mode_id="analyst",
        run_id=run.run_id,
        context=transport_context,
        dest=reply_dest,
    )

    assert result.status == "ok"
    assert result.message == "Telegram recovery completed."
    assert mode.calls[0]["context"] is transport_context
    assert mode.calls[0]["dest"]["chat_id"] == 1
    assert sent[0]["context"] is transport_context
    assert sent[0]["kwargs"]["chat_id"] == 1
    assert sent[0]["kwargs"]["text"] == "Telegram recovery progress"


@pytest.mark.asyncio
async def test_bot_recover_run_without_context_returns_structured_degradation(tmp_path) -> None:
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path / "workdir"))
    session.modes.active_mode = "analyst"

    store = app.mode_run_operations.artifact_store
    run = store.start_run(session=session, mode_id="analyst", run_id="run_20260314T120500Z_degrade", phase="plan")
    store.save_state(run, {"phase": "plan", "status": "running", "mode_context": {}})
    app.mode_run_operations.doctor_service.diagnose = lambda *_a, **_k: _doctor_report(  # type: ignore[method-assign]
        mode_id="analyst",
        action="rollback_to_checkpoint",
    )

    mode = _RecoveryMode(
        progress_text="Telegram recovery progress",
        final_text="Telegram recovery completed.",
    )
    app.mode_registry_service = types.SimpleNamespace(get=lambda _mode_id: mode)

    reply_dest = app.build_telegram_reply_dest(session, 1)

    result = await app.mode_run_operations.recover_run(
        session=session,
        mode_id="analyst",
        run_id=run.run_id,
        context=None,
        dest=reply_dest,
    )

    assert result.status == "ok"
    assert "Живой Telegram transport context недоступен" in result.message
    assert mode.calls[0]["context"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["doctor", "recover", "resume", "apply_recommendation"])
async def test_telegram_mode_callback_run_operations_pass_execution_vector(operation: str) -> None:
    session = types.SimpleNamespace(
        id="s1",
        chat_id=1,
        active_mode="agent",
        busy=False,
        queue=[],
        run_lock=asyncio.Lock(),
        is_active_by_tick=lambda: False,
        conversation_scope=types.SimpleNamespace(session_uid="telegram:session:1", chat_id=1),
    )
    app = _TelegramSurfaceBotApp(session)
    calls: list[dict] = []

    class _RunOps:
        async def doctor_run(self, **kwargs):
            calls.append({"operation": "doctor", **kwargs})
            return types.SimpleNamespace(message="doctor:OK")

        async def recover_run(self, **kwargs):
            calls.append({"operation": "recover", **kwargs})
            return types.SimpleNamespace(message="recover:OK")

        async def resume_run(self, **kwargs):
            calls.append({"operation": "resume", **kwargs})
            return types.SimpleNamespace(message="resume:OK")

        async def apply_recommendation_run(self, **kwargs):
            calls.append({"operation": "apply_recommendation", **kwargs})
            return types.SimpleNamespace(message="apply_recommendation:OK")

    app.mode_run_operations = _RunOps()
    sent: list[dict] = []

    async def _send_message(context, **kwargs):
        sent.append({"context": context, "kwargs": dict(kwargs)})
        return True

    router = ModeCallbackRouterService(
        mode_registry=app.mode_registry_service,
        send_message=_send_message,
        get_session=lambda _chat_id: session,
    )
    raw_context = types.SimpleNamespace(bot=object())
    await router.handle_mode_action_callback(
        data=f"ma:agent:{operation}",
        chat_id=1,
        query=_FakeTelegramQuery(f"ma:agent:{operation}"),
        context=raw_context,
        bot_app=app,
    )

    assert calls[0]["operation"] == operation
    assert calls[0]["session"] is session
    assert calls[0]["mode_id"] == "agent"
    assert isinstance(calls[0]["context"], TelegramTransportContext)
    assert calls[0]["context"].raw_context is raw_context
    assert calls[0]["context"].chat_id == 1
    assert calls[0]["context"].session_uid == "telegram:session:1"
    assert calls[0]["dest"] == {"kind": "telegram", "chat_id": 1, "user_id": 42}
    assert sent[-1]["context"] is calls[0]["context"]


@pytest.mark.asyncio
async def test_telegram_mode_callback_promote_skills_passes_execution_vector() -> None:
    session = types.SimpleNamespace(
        id="s1",
        chat_id=1,
        active_mode="agent",
        conversation_scope=types.SimpleNamespace(session_uid="telegram:session:promote", chat_id=1),
    )
    app = _TelegramSurfaceBotApp(session)
    calls: list[dict] = []

    class _SkillRuntime:
        def promote_run_skills(self, **kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(message="PROMOTE_OK")

    class _BotAppSkillRuntime:
        def promote_run_skills(self, **_kwargs):
            raise AssertionError("telegram promote_skills must use SDK skill_runtime")

    app.mode_skill_runtime = _BotAppSkillRuntime()
    mode = app.mode_registry.get("agent")
    assert mode is not None
    mode.initialize(
        services={
            "skill_runtime": _SkillRuntime(),
            "run_artifacts": app.mode_run_artifacts,
        }
    )

    async def _send_message(_context, **_kwargs):
        return True

    router = ModeCallbackRouterService(
        mode_registry=app.mode_registry_service,
        send_message=_send_message,
        get_session=lambda _chat_id: session,
    )
    raw_context = types.SimpleNamespace(bot=object())
    await router.handle_mode_action_callback(
        data="ma:agent:promote_skills",
        chat_id=1,
        query=_FakeTelegramQuery("ma:agent:promote_skills"),
        context=raw_context,
        bot_app=app,
    )

    assert calls[0]["session"] is session
    assert calls[0]["mode_id"] == "agent"
    assert calls[0]["actor_chat_id"] == 1
    assert isinstance(calls[0]["context"], TelegramTransportContext)
    assert calls[0]["context"].raw_context is raw_context
    assert calls[0]["dest"] == {"kind": "telegram", "chat_id": 1, "user_id": 42}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["doctor", "recover", "resume", "apply_recommendation"])
async def test_miniapp_run_actions_pass_execution_vector(operation: str, tmp_path) -> None:
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path / "workdir"))
    store = app.mode_run_operations.artifact_store
    run = store.start_run(session=session, mode_id="agent", run_id=f"run_20260314T122000Z_{operation}", phase="execute")
    store.save_state(run, {"phase": "execute", "status": "running", "mode_context": {}})
    calls: list[dict] = []

    class _RunOps:
        artifact_store = store

        async def doctor_run(self, **kwargs):
            calls.append({"operation": "doctor", **kwargs})
            return _run_operation_result(operation="doctor", mode_id=kwargs["mode_id"], run_id=kwargs["run_id"])

        async def recover_run(self, **kwargs):
            calls.append({"operation": "recover", **kwargs})
            return _run_operation_result(operation="recover", mode_id=kwargs["mode_id"], run_id=kwargs["run_id"])

        async def resume_run(self, **kwargs):
            calls.append({"operation": "resume", **kwargs})
            return _run_operation_result(operation="resume", mode_id=kwargs["mode_id"], run_id=kwargs["run_id"])

        async def apply_recommendation_run(self, **kwargs):
            calls.append({"operation": "apply_recommendation", **kwargs})
            return _run_operation_result(
                operation="apply_recommendation",
                mode_id=kwargs["mode_id"],
                run_id=kwargs["run_id"],
            )

    app.mode_run_operations = _RunOps()

    web_app = web.Application()
    MiniAppRoutes(app).register(web_app)
    server = TestServer(web_app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post(
            f"/api/runs/{run.run_id}/{operation}",
            headers={"X-Telegram-Init-Data": _build_init_data("t", 1)},
            json={"session_uid": session_runtime_uid(session), "mode_id": "agent"},
        )
        assert resp.status == 200
        payload = await resp.json()
    finally:
        await client.close()
        await server.close()

    assert payload["ok"] is True
    assert calls[0]["operation"] == operation
    assert calls[0]["session"] is session
    assert calls[0]["mode_id"] == "agent"
    assert calls[0]["run_id"] == run.run_id
    assert getattr(calls[0]["context"], "transport", "") == "miniapp"
    assert getattr(calls[0]["context"], "session_uid", "") == session_runtime_uid(session)
    assert calls[0]["dest"]["kind"] == "miniapp"
    assert calls[0]["dest"]["session_uid"] == session_runtime_uid(session)
    assert calls[0]["dest"]["user_id"] == 1


@pytest.mark.asyncio
async def test_miniapp_promote_skills_passes_execution_vector(tmp_path) -> None:
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = _build_config(tmp_path)
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(tmp_path / "workdir"))
    store = app.mode_run_operations.artifact_store
    run = store.start_run(session=session, mode_id="agent", run_id="run_20260314T122500Z_promote", phase="execute")
    store.save_state(run, {"phase": "execute", "status": "running", "selected_skill_ids": ["playwright-cli"]})
    calls: list[dict] = []

    class _SkillRuntime:
        def promote_run_skills(self, **kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(
                to_dict=lambda: {
                    "status": "ok",
                    "message": "PROMOTE_OK",
                    "mode_id": kwargs.get("mode_id"),
                    "run_id": kwargs.get("run_id"),
                    "promoted_skill_ids": ["playwright-cli"],
                    "skipped_skill_ids": [],
                    "results": [],
                }
            )

    app.mode_skill_runtime = _SkillRuntime()

    web_app = web.Application()
    MiniAppRoutes(app).register(web_app)
    server = TestServer(web_app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post(
            f"/api/runs/{run.run_id}/promote_skills",
            headers={"X-Telegram-Init-Data": _build_init_data("t", 1)},
            json={"session_uid": session_runtime_uid(session), "mode_id": "agent"},
        )
        assert resp.status == 200
        payload = await resp.json()
    finally:
        await client.close()
        await server.close()

    assert payload["ok"] is True
    assert calls[0]["session"] is session
    assert calls[0]["mode_id"] == "agent"
    assert calls[0]["run_id"] == run.run_id
    assert calls[0]["is_admin"] is True
    assert getattr(calls[0]["context"], "transport", "") == "miniapp"
    assert calls[0]["dest"]["kind"] == "miniapp"
    assert calls[0]["dest"]["session_uid"] == session_runtime_uid(session)


def test_desktop_recover_run_routes_progress_messages_to_ui_with_execution_vector(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    facade._modes_initialized = True
    facade.get_admin_status_payload = lambda _uid: {"active": True}  # type: ignore[method-assign]

    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    session.modes.active_mode = "analyst"

    mode = _RecoveryMode(
        progress_text="Desktop recovery progress",
        final_text="Desktop recovery completed.",
    )
    facade.mode_registry_service = types.SimpleNamespace(get=lambda _mode_id: mode)

    service = facade._desktop_run_operations()
    store = service.artifact_store
    run = store.start_run(session=session, mode_id="analyst", run_id="run_20260314T121000Z_desktop", phase="plan")
    store.save_state(run, {"phase": "plan", "status": "running", "mode_context": {}})
    service.doctor_service.diagnose = lambda *_a, **_k: _doctor_report(  # type: ignore[method-assign]
        mode_id="analyst",
        action="rollback_to_checkpoint",
    )

    notifications = []
    facade.subscribe(lambda note: notifications.append(note))

    result = asyncio.run(
        facade.recover_run(
            session.conversation_scope.session_uid,
            mode_id="analyst",
            run_id=run.run_id,
        )
    )

    session_uid = session_runtime_uid(session)
    assert result["status"] == "ok"
    assert mode.calls[0]["context"] is not None
    assert getattr(mode.calls[0]["context"], "session_uid", "") == session_uid
    assert mode.calls[0]["dest"]["chat_id"] == session_uid
    assert any(
        note.event == "ui:message" and note.payload.get("text") == "Desktop recovery progress"
        for note in notifications
    )
    assert any(
        note.event == "ui:message" and note.payload.get("text") == "Desktop recovery completed."
        for note in notifications
    )


@pytest.mark.parametrize(
    ("facade_method", "operation"),
    [
        ("doctor_run", "doctor"),
        ("recover_run", "recover"),
        ("resume_run", "resume"),
        ("apply_recommendation_run", "apply_recommendation"),
    ],
)
def test_desktop_run_operations_pass_execution_vector(tmp_path, facade_method: str, operation: str) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    facade.get_admin_status_payload = lambda _uid: {"active": True}  # type: ignore[method-assign]
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    calls: list[dict] = []

    class _RunOps:
        async def doctor_run(self, **kwargs):
            calls.append({"operation": "doctor", **kwargs})
            return _run_operation_result(operation="doctor", mode_id=kwargs["mode_id"], run_id=kwargs["run_id"])

        async def recover_run(self, **kwargs):
            calls.append({"operation": "recover", **kwargs})
            return _run_operation_result(operation="recover", mode_id=kwargs["mode_id"], run_id=kwargs["run_id"])

        async def resume_run(self, **kwargs):
            calls.append({"operation": "resume", **kwargs})
            return _run_operation_result(operation="resume", mode_id=kwargs["mode_id"], run_id=kwargs["run_id"])

        async def apply_recommendation_run(self, **kwargs):
            calls.append({"operation": "apply_recommendation", **kwargs})
            return _run_operation_result(
                operation="apply_recommendation",
                mode_id=kwargs["mode_id"],
                run_id=kwargs["run_id"],
            )

    facade._desktop_run_operations_service = _RunOps()
    result = asyncio.run(
        getattr(facade, facade_method)(
            session.conversation_scope.session_uid,
            mode_id="agent",
            run_id="run_20260314T123000Z_desktop_vector",
        )
    )

    assert result["status"] == "ok"
    assert calls[0]["operation"] == operation
    assert calls[0]["session"] is session
    assert getattr(calls[0]["context"], "transport", "") == "desktop"
    assert getattr(calls[0]["context"], "session_uid", "") == session_runtime_uid(session)
    assert calls[0]["dest"]["kind"] == "desktop"
    assert calls[0]["dest"]["chat_id"] == session_runtime_uid(session)


def test_desktop_promote_run_skills_passes_execution_vector(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    facade.config = cfg
    facade.get_admin_status_payload = lambda _uid: {"active": True}  # type: ignore[method-assign]
    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))
    calls: list[dict] = []

    class _SkillRuntime:
        def promote_run_skills(self, **kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(
                to_dict=lambda: {
                    "status": "ok",
                    "message": "PROMOTE_OK",
                    "mode_id": kwargs.get("mode_id"),
                    "run_id": kwargs.get("run_id"),
                    "promoted_skill_ids": ["playwright-cli"],
                    "skipped_skill_ids": [],
                    "results": [],
                }
            )

    facade._desktop_mode_dependencies_instance = types.SimpleNamespace(skill_runtime=_SkillRuntime())
    facade._desktop_run_operations_service = types.SimpleNamespace(artifact_store=object())

    result = asyncio.run(
        facade.promote_run_skills(
            session.conversation_scope.session_uid,
            mode_id="agent",
            run_id="run_20260314T123500Z_desktop_promote",
        )
    )

    assert result["status"] == "ok"
    assert calls[0]["session"] is session
    assert calls[0]["mode_id"] == "agent"
    assert calls[0]["is_admin"] is True
    assert getattr(calls[0]["context"], "transport", "") == "desktop"
    assert calls[0]["dest"]["kind"] == "desktop"
    assert calls[0]["dest"]["chat_id"] == session_runtime_uid(session)
