import asyncio
import os

import pytest

from app.services.run_artifact_store import RunArtifactStore
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from modes.analyst.state_store import AnalystStateStore, build_context_key
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, CallbackModel, MessageModel, ToolResult
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager, session_runtime_uid
from utils import cli_proxy_artifact_path


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


class _AnalystModeStub(BaseMode):
    mode_id = "analyst"
    display_name = "Analyst"

    def pre_run_reset_mode_id(self):
        return self.mode_id

    async def handle_input(self, message: MessageModel, ctx: dict) -> ToolResult:
        _ = message
        _ = ctx
        return ToolResult.ok()

    async def handle_callback(self, callback: CallbackModel, ctx: dict) -> ToolResult:
        _ = callback
        _ = ctx
        return ToolResult.ok()

    async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
        _ = session
        _ = bot_app
        _ = context
        _ = dest
        return f"analyst:{user_text}"


class _CacheRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def clear_session_cache(self, session_id: str) -> None:
        self.calls.append(str(session_id))


@pytest.mark.asyncio
async def test_desktop_analyst_prerun_reset_keeps_session_core_state_and_clears_transient(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    os.makedirs(os.path.dirname(cfg.defaults.state_path) or ".", exist_ok=True)

    registry = ModeRegistry()
    registry.register(_AnalystModeStub())
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
    session.modes.active_mode = "analyst"
    session.manager_quiet_mode = True
    session.agent_memory = {"k": "v"}
    session.project_root = str(tmp_path / "project")
    session.modes.analyst_mode = "awaiting_input"
    session.analyst_runtime_template_id = "audit"
    session.resume_token = "resume-keep"

    store = AnalystStateStore(cli_proxy_artifact_path(session.workdir, ".analyst_data"))
    context_key = build_context_key(session.chat_id, session.id)
    ctx = store.load(context_key)
    ctx.mode = "awaiting_input"
    ctx.active_flow = "audit"
    ctx.runtime_template_id = "audit"
    ctx.needs_clarification = True
    ctx.clarification_is_blocking = True
    ctx.clarification_topic = "scope"
    ctx.source_user_text = "старый запрос"
    ctx.clarification_answers = ["mobile"]
    ctx.last_draft = "stale draft"
    ctx.last_draft_updated_at = 123.0
    store.save(ctx)
    run_store = RunArtifactStore(cfg)
    run = run_store.start_run(
        session=session,
        mode_id="analyst",
        run_id="run_20260412T101000Z_desktop_reset",
        phase="intent",
    )

    runtime = _CacheRuntime()
    facade.register_mode_runtime("cache", runtime)

    bot_app = facade._desktop_bot_app()
    qid = "q-stale"
    session_uid = session_runtime_uid(session)
    bot_app.ui_state.pending_questions[qid] = {
        "session_uid": session_uid,
        "session_id": session.id,
        "options": ["A", "B"],
    }
    bot_app.ui_state.active_ask_question_by_chat[session_uid] = qid

    registry_obj = getattr(bot_app, "_tool_registry", None)
    if registry_obj is None:
        class _RegistryStub:
            def __init__(self) -> None:
                self.pending_questions = {}

        registry_obj = _RegistryStub()
        bot_app._tool_registry = registry_obj
    fut = asyncio.get_running_loop().create_future()
    registry_obj.pending_questions[qid] = fut

    pipeline = facade._desktop_mode_pipeline_service()
    await pipeline.run_mode_pipeline(
        session,
        "goal",
        {"kind": "desktop", "session_uid": session_uid},
        object(),
        mode_id="analyst",
    )

    assert session.modes.active_mode == "analyst"
    assert session.manager_quiet_mode is True
    assert session.agent_memory == {"k": "v"}
    assert session.project_root == str(tmp_path / "project")
    assert session.resume_token == "resume-keep"

    assert session.modes.analyst_mode == "spec"
    assert session.analyst_runtime_template_id == ""

    assert runtime.calls == [session.scoped_key]
    assert qid not in bot_app.ui_state.pending_questions
    assert bot_app.ui_state.active_ask_question_by_chat.get(session.id) is None
    assert qid not in registry_obj.pending_questions
    assert fut.cancelled() is True

    updated_ctx = store.load(context_key)
    assert updated_ctx.mode == "spec"
    assert updated_ctx.active_flow == ""
    assert updated_ctx.runtime_template_id == ""
    assert updated_ctx.needs_clarification is False
    assert updated_ctx.clarification_is_blocking is False
    assert updated_ctx.clarification_topic == ""
    assert updated_ctx.source_user_text == ""
    assert updated_ctx.clarification_answers == []
    assert updated_ctx.last_draft == ""
    assert updated_ctx.last_draft_updated_at == 0.0
    assert run_store.load_state(run)["status"] == "superseded"
