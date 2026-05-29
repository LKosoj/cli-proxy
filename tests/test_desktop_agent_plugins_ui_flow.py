import os
import asyncio

import pytest

from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.sandbox_service import AgentSandboxService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.agent.mode import AgentMode
from modes.manager.mode import ManagerMode
from modes.registry import ModeRegistry
from modes.sdk import encode_mode_dirs
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager, session_runtime_uid
from utils import sandbox_session_dir, sandbox_shared_dir


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


def test_desktop_fake_botapp_mode_private_expectations_inventory(tmp_path) -> None:
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
    private_callables = {
        name
        for name in dir(bot_app)
        if name.startswith("_")
        and not name.startswith("__")
        and callable(getattr(bot_app, name))
    }
    baseline_private_callables = {
        "_Metrics",
        "_clear_message_reply_markup",
        "_clear_pending_question",
        "_delete_message",
        "_edit_message",
        "_handle_cli_input",
        "_mode_allows_plugin_ui",
        "_parse_progress_text",
        "_send_ask_question",
        "_send_document",
        "_send_message",
    }
    baseline_private_by_mode = {
        "agent": {"_clear_message_reply_markup", "_clear_pending_question", "_send_ask_question", "_send_message"},
        "manager": {"_send_message"},
        "analyst": {"_delete_message", "_edit_message", "_send_message"},
        "webmaster": {"_send_document", "_send_message"},
        "admin": set(),
    }

    assert private_callables == baseline_private_callables
    for expected in baseline_private_by_mode.values():
        assert expected <= private_callables


@pytest.mark.asyncio
async def test_desktop_agent_plugins_ui_callbacks_flow(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestAgentMode(AgentMode):
        # Avoid initializing the heavy orchestrator runtime in this focused UI test.
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestAgentMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "agent"

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    # Fake plugin runtime: provides a plugin menu and actions.
    class _Plugin:
        def __init__(self):
            self._awaiting = False

        def get_plugin_id(self) -> str:
            return "plug"

        async def _dispatch_callback(self, update, context) -> None:
            data = str(getattr(getattr(update, "callback_query", None), "data", "") or "")
            if data.startswith("cb:plug:do"):
                await update.callback_query.message.reply_text("PLUGIN_OK")

        def awaiting_input(self, _chat_id: int) -> bool:
            return bool(self._awaiting)

    plugin = _Plugin()

    class _FakeToolRegistry:
        def __init__(self, plugin_obj):
            self.plugins = {"p": plugin_obj}

        def list_tool_names(self):
            return []

    class _FakePluginUiRuntime:
        def supports_capability(self, cap: str) -> bool:
            return str(cap or "").strip() == "plugin_ui"

        def get_plugin_ui(self, profile):
            return {
                "plugin_menu": [
                    {
                        "plugin_id": "plug",
                        "label": "MyPlugin",
                        "actions": [{"label": "Do", "action": "do"}],
                        "plugin": plugin,
                    }
                ]
            }

    # Wire ToolRegistry into Desktop bot_app adapter and runtime into facade.
    bot_app = facade._desktop_bot_app()
    bot_app._tool_registry = _FakeToolRegistry(plugin)
    facade.register_mode_runtime("fake_plugin_ui", _FakePluginUiRuntime())

    # 1) Agent plugin list menu (mode callback).
    ok = await facade.handle_mode_callback(session_uid, data="ma:agent:plugins:{}")
    assert ok is True
    assert any(ev == "ui:mode_menu" and (p.get("text") or "").startswith("Плагины") for ev, p in events)
    first_menu = next((p for ev, p in events if ev == "ui:mode_menu" and (p.get("text") or "").startswith("Плагины")), None)
    assert first_menu is not None
    first_rows = first_menu.get("rows") or []
    back_buttons = [
        b.get("data")
        for row in first_rows
        for b in (row or [])
        if str(b.get("text") or "").startswith("⬅️")
    ]
    assert back_buttons
    assert back_buttons[0] == f"sess_mode_pick:{session_uid}"

    # 1.5) Back from plugin list should re-open the agent mode menu.
    events.clear()
    ok = await facade.handle_mode_callback(session_uid, data=back_buttons[0])
    assert ok is True
    assert any(ev == "ui:mode_menu" and "Агент сейчас включен" in str(p.get("text") or "") for ev, p in events)

    # 2) Select a plugin: should render actions that include cb:... callback.
    events.clear()
    ok = await facade.handle_mode_callback(session_uid, data="ma:agent:plugin:p=plug")
    assert ok is True
    menu = next((p for ev, p in events if ev == "ui:mode_menu"), None)
    assert menu is not None
    rows = menu.get("rows") or []
    flat = [b.get("data") for row in rows for b in (row or [])]
    assert "cb:plug:do" in flat

    # 3) Press the action button: Desktop must dispatch to plugin callback handler.
    events.clear()
    ok = await facade.handle_mode_callback(session_uid, data="cb:plug:do")
    assert ok is True
    assert any(ev == "ui:message" and (p.get("text") or "") == "PLUGIN_OK" for ev, p in events)


@pytest.mark.asyncio
async def test_desktop_agent_status_uses_sdk_state_services(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestAgentMode(AgentMode):
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestAgentMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "agent"

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    bot_app = facade._desktop_bot_app()
    await bot_app._send_ask_question(
        object(),
        chat_id=session_uid,
        session_id=session_uid,
        question_id="q_desktop_agent",
        question="Continue?",
        options=["Yes", "No"],
        allow_custom=True,
    )
    await facade._desktop_dirs_flow_service().start_flow(
        session_uid,
        object(),
        root=str(tmp_path),
        mode_token=encode_mode_dirs("agent", "project"),
    )
    assert (
        facade._get_mode_dialogs().pending_questions_count(
            session=session,
            chat_id=session_uid,
        )
        == 1
    )

    events.clear()
    ok = await facade.handle_mode_callback(session_uid, data="ma:agent:status")

    assert ok is True
    status_payload = next(
        (
            p
            for ev, p in events
            if ev in {"ui:message", "ui:mode_menu"}
            and "🤖 Статус Агента" in str(p.get("text") or "")
        ),
        None,
    )
    assert status_payload is not None
    text = str(status_payload.get("text") or "")
    assert "🤖 Статус Агента" in text
    assert "Pending questions: 1; active=q_desktop_agent; custom=нет" in text
    assert "Active plugin flow: dirs:project" in text


@pytest.mark.asyncio
async def test_desktop_manager_resume_cancel_clears_pending_state(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestManagerMode(ManagerMode):
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestManagerMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "manager"
    facade._manager_resume_pending[session_uid] = {"prompt": "p"}

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    ok = await facade.handle_mode_callback(session_uid, data="ma:manager:resume_cancel")
    assert ok is True
    assert session_uid not in facade._manager_resume_pending
    assert any(ev == "ui:message" and (p.get("text") or "") == "Отменено." for ev, p in events)


@pytest.mark.asyncio
async def test_desktop_agent_cleanup_actions_remove_data_and_report_actual_result(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestAgentMode(AgentMode):
        # Avoid initializing the heavy orchestrator runtime in this focused UI test.
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestAgentMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "agent"

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    sandbox = AgentSandboxService(cfg.defaults.workdir)
    sandbox.configure()
    chat_workspace = sandbox.chat_workspace(session_uid)
    os.makedirs(chat_workspace, exist_ok=True)
    with open(os.path.join(chat_workspace, "marker.txt"), "w", encoding="utf-8") as f:
        f.write("chat")
    chat_shared_file = os.path.join(sandbox_shared_dir(cfg.defaults.workdir), "chats", f"chat_{session_uid}.md")
    os.makedirs(os.path.dirname(chat_shared_file), exist_ok=True)
    with open(chat_shared_file, "w", encoding="utf-8") as f:
        f.write("shared")

    session_path = sandbox_session_dir(cfg.defaults.workdir, session.scoped_key)
    os.makedirs(session_path, exist_ok=True)
    with open(os.path.join(session_path, "session.txt"), "w", encoding="utf-8") as f:
        f.write("session")

    class _CacheRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def clear_session_cache(self, session_id: str) -> None:
            self.calls.append(str(session_id))

    cache_runtime = _CacheRuntime()
    facade.register_mode_runtime("cache", cache_runtime)

    ok = await facade.handle_mode_callback(session_uid, data="ma:agent:clean_all")
    assert ok is True
    assert not os.path.exists(chat_workspace)
    assert not os.path.exists(chat_shared_file)
    assert os.path.isdir(session_path)
    assert cache_runtime.calls == [session.scoped_key]
    clean_all_msg = next((p.get("text") or "" for ev, p in events if ev == "ui:mode_menu"), "")
    assert "Песочница текущего чата очищена. Удалено: 2." in clean_all_msg

    events.clear()
    ok = await facade.handle_mode_callback(session_uid, data="ma:agent:clean_session")
    assert ok is True
    assert not os.path.exists(session_path)
    assert cache_runtime.calls == [session.scoped_key, session.scoped_key]
    clean_session_msg = next((p.get("text") or "" for ev, p in events if ev == "ui:mode_menu"), "")
    assert "Файлы текущей сессии удалены." in clean_session_msg


@pytest.mark.asyncio
async def test_desktop_agent_mode_input_does_not_show_busy_on_first_and_sequential_runs(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestAgentMode(AgentMode):
        # Avoid heavy runtime bootstrap in focused Desktop routing test.
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestAgentMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "agent"

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    class _FakeRunAgentRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def supports_capability(self, cap: str) -> bool:
            return str(cap or "").strip() == "run_agent"

        async def run(self, session, user_text, bot_app, context, dest):
            _ = session
            _ = bot_app
            _ = context
            _ = dest
            self.calls.append(str(user_text or ""))
            await asyncio.sleep(0)
            return "ok"

    runtime = _FakeRunAgentRuntime()
    facade.register_mode_runtime("fake_run_agent", runtime)

    await facade.run_session_input(session_uid, "first")
    await facade.run_session_input(session_uid, "second")

    # Let background mode tasks flush.
    for _ in range(40):
        if len(runtime.calls) >= 2:
            break
        await asyncio.sleep(0)

    assert runtime.calls[:2] == ["first", "second"]
    assert not any(
        ev == "ui:mode_menu" and "Сессия занята" in str(payload.get("text") or "")
        for ev, payload in events
    )


@pytest.mark.asyncio
async def test_desktop_agent_mode_double_launch_does_not_raise_false_busy_prompt(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestAgentMode(AgentMode):
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestAgentMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "agent"

    events: list[tuple[str, dict]] = []
    facade.subscribe(lambda note: events.append((note.event, note.payload)))

    class _SlowRunAgentRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = asyncio.Event()
            self.unblock = asyncio.Event()
            self.finished = asyncio.Event()

        def supports_capability(self, cap: str) -> bool:
            return str(cap or "").strip() == "run_agent"

        async def run(self, session, user_text, bot_app, context, dest):
            _ = session, bot_app, context, dest
            self.calls.append(str(user_text or ""))
            self.started.set()
            await self.unblock.wait()
            self.finished.set()
            return "ok"

    runtime = _SlowRunAgentRuntime()
    facade.register_mode_runtime("fake_run_agent_slow", runtime)

    first_output = await facade.run_session_input(session_uid, "first")
    for _ in range(200):
        if runtime.started.is_set():
            break
        await asyncio.sleep(0.01)
    assert runtime.started.is_set()

    second_output = await facade.run_session_input(session_uid, "second")
    assert runtime.calls[:2] == ["first", "second"]
    assert isinstance(first_output, str)
    assert second_output == ""
    assert not any(
        ev == "ui:mode_menu" and "Сессия занята" in str(payload.get("text") or "")
        for ev, payload in events
    )

    runtime.unblock.set()
    for _ in range(200):
        if runtime.finished.is_set():
            break
        await asyncio.sleep(0.01)
    assert runtime.finished.is_set()


@pytest.mark.asyncio
async def test_desktop_mode_switch_from_agent_to_manager_routes_to_active_mode_runtime(tmp_path) -> None:
    cfg = _build_config(tmp_path)

    class _TestAgentMode(AgentMode):
        def build_runtime(self, config):
            return None

    class _TestManagerMode(ManagerMode):
        def build_runtime(self, config):
            return None

    mode_registry = ModeRegistry()
    mode_registry.register(_TestAgentMode())
    mode_registry.register(_TestManagerMode())
    mode_registry_service = ModeRegistryService(mode_registry)

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
    session.modes.active_mode = "agent"

    class _RunAgentRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def supports_capability(self, cap: str) -> bool:
            return str(cap or "").strip() == "run_agent"

        async def run(self, session, user_text, bot_app, context, dest):
            _ = session, bot_app, context, dest
            self.calls.append(str(user_text or ""))
            await asyncio.sleep(0)
            return "agent_ok"

    class _RunManagerRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def supports_capability(self, cap: str) -> bool:
            return str(cap or "").strip() == "run_manager"

        async def run(self, session, user_text, bot_app, context, dest):
            _ = session, bot_app, context, dest
            self.calls.append(str(user_text or ""))
            await asyncio.sleep(0)
            return "manager_ok"

    agent_runtime = _RunAgentRuntime()
    manager_runtime = _RunManagerRuntime()
    facade.register_mode_runtime("fake_run_agent_switch", agent_runtime)
    facade.register_mode_runtime("fake_run_manager_switch", manager_runtime)

    await facade.run_session_input(session_uid, "agent message")
    for _ in range(80):
        if agent_runtime.calls:
            break
        await asyncio.sleep(0)
    assert agent_runtime.calls == ["agent message"]

    session.modes.active_mode = "manager"
    await facade.run_session_input(session_uid, "manager message")
    for _ in range(80):
        if manager_runtime.calls:
            break
        await asyncio.sleep(0)
    assert manager_runtime.calls == ["manager message"]
    assert agent_runtime.calls == ["agent message"]
