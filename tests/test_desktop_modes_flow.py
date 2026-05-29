import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from app.services.input_dispatch_service import InputDispatchService
from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.main_window import MainWindow
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
            run_artifacts_retention_days=21,
            skill_discovery_mode="auto",
            skill_install_policy="admin_approve",
            skill_registry_paths=[".cli-proxy/skills", ".cli-proxy/desktop-skills"],
            skill_allowlisted_sources=["local:global-registry", "registry:npx-skills"],
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_desktop_facade_injects_foundation_mode_dependencies(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    registry = ModeRegistry()

    class ProbeMode(BaseMode):
        mode_id = "probe"
        display_name = "Probe"

        async def handle_input(self, message, ctx):
            return ToolResult.ok(message.text)

        async def handle_callback(self, callback, ctx):
            return ToolResult.ok(callback.action)

    registry.register(ProbeMode())
    mode_registry_service = ModeRegistryService(registry)
    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)

    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg

    facade._ensure_modes_ready()

    plugin = registry.get("probe")
    assert plugin is not None
    deps = plugin.mode_dependencies
    assert deps is not None
    assert deps.session_manager is session_service._manager
    assert deps.registry is mode_registry_service
    assert deps.run_artifacts is not None
    assert deps.run_artifacts.retention_window_days() == 21
    assert deps.run_observability is not None
    assert deps.run_observability.is_enabled() is True
    assert deps.run_doctor is not None
    assert deps.run_doctor.is_enabled() is True
    assert deps.run_boundary_validation is not None
    assert deps.run_boundary_validation.is_enabled() is True
    assert deps.skill_runtime is not None
    assert deps.skill_runtime.allows_auto_discovery() is True
    assert deps.skill_runtime.registry_path_list() == [".cli-proxy/skills", ".cli-proxy/desktop-skills"]
    assert deps.skill_runtime.allows_source("registry:npx-skills") is True


@pytest.mark.asyncio
async def test_desktop_modes_flow_select_mode_run_updates_ui(qtbot, tmp_path) -> None:
    """
    Интеграционный сценарий Desktop:
    1) Выбрать режим через ModePanelWidget.
    2) Отправить сообщение -> desktop сначала staging'ит ввод и показывает confirm-меню.
    3) Взять ввод в работу -> facade.run_session_input идёт через mode pipeline.
    4) UI: индикаторы "Working/Idle" и loading toggles; ответ появляется одним финальным сообщением.
    """
    cfg = _build_config(tmp_path)
    (tmp_path / "workdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    entered = asyncio.Event()
    release = asyncio.Event()

    registry = ModeRegistry()

    class BlockingEchoMode(BaseMode):
        mode_id = "echo"
        display_name = "Echo"

        async def handle_input(self, message, ctx):
            pipeline = self.require_service("pipeline")
            await pipeline.run_mode_pipeline(
                ctx["session"],
                message.text,
                dict(ctx.get("dest") or {}),
                ctx.get("context"),
                mode_id=self.mode_id,
            )
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            if str(callback.action or "").strip() in ("enable", "on"):
                await self._activate_mode(session=ctx["session"], bot_app=ctx["bot_app"])
            elif str(callback.action or "").strip() in ("disable", "off"):
                await self._deactivate_mode(session=ctx["session"], bot_app=ctx["bot_app"], cancel_tasks=True, timeout_s=0.2)
            return ToolResult.ok()

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            entered.set()
            await release.wait()
            return "MODE:" + str(user_text)

    registry.register(BlockingEchoMode())
    mode_registry_service = ModeRegistryService(registry)
    mode_registry_service.initialize_plugins(config=cfg, services={})

    task_service = TaskService()
    session_manager = SessionManager(cfg)
    session_service = SessionService(session_manager, task_service)

    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    # start() в этом тесте не вызываем: достаточно установить config для _ensure_modes_ready().
    facade.config = cfg

    session = session_service.create_session(1, "dummy", str(tmp_path / "workdir"))

    # Запрещаем fallback: при активном mode должен быть вызван pipeline, не run_prompt.
    async def _no_prompt(*_a, **_k):
        raise AssertionError("session.run_prompt must not be called when mode is active")

    session.run_prompt = _no_prompt  # type: ignore[assignment]

    ui_state_service = MagicMock()
    ui_state_service.state = MagicMock(
        window_geometry=None,
        window_state=None,
        active_tab="chat",
        splitter_sizes=[200, 600],
    )
    ui_state_service.save = AsyncMock()

    class _SessionManagerStub(QWidget):
        sessionSelected = Signal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.actor_id = "desktop"

    class _GitPanelStub(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def set_session(self, _session):
            return None

    class _ConfigEditorStub(QWidget):
        configSaved = Signal()

        def __init__(self, *args, **kwargs):
            super().__init__()

        def load_config(self):
            return None

    class _RunsPanelStub(QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def set_session_id(self, _session_uid):
            return None

    class _FilesPanelStub(QWidget):
        def set_session(self, _session):
            return None

        def refresh(self):
            return None

    class _StatusPanelStub(QWidget):
        def set_session(self, _session, _session_uid=""):
            return None

        def refresh_mode(self, _session):
            return None

    class _SchedulerPanelStub(QWidget):
        def set_context_session(self, _session_uid):
            return None

    # ensure_async в Desktop может не увидеть running loop в тестовой среде; подменяем на loop.create_task().
    def _ensure_async(coro, parent=None):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        if parent is not None and hasattr(parent, "_background_tasks"):
            parent._background_tasks.add(task)
            task.add_done_callback(lambda t: parent._background_tasks.discard(t))
        return task

    with patch("desktop.main_window.SessionManagerWidget", side_effect=lambda *a, **k: _SessionManagerStub()), \
         patch("desktop.main_window.GitPanelWidget", side_effect=lambda *a, **k: _GitPanelStub()), \
         patch("desktop.main_window.FilesPanelWidget", side_effect=lambda *a, **k: _FilesPanelStub()), \
         patch("desktop.main_window.RunOperationsPanelWidget", side_effect=lambda *a, **k: _RunsPanelStub()), \
         patch("desktop.main_window.SchedulerPanelWidget", side_effect=lambda *a, **k: _SchedulerPanelStub()), \
         patch("desktop.main_window.StatusPanelWidget", side_effect=lambda *a, **k: _StatusPanelStub()), \
         patch("desktop.main_window.ConfigEditorWidget", side_effect=lambda *a, **k: _ConfigEditorStub()), \
         patch("desktop.main_window.LogViewerWidget", side_effect=lambda *a, **k: QWidget()), \
         patch("desktop.main_window.ensure_async", side_effect=_ensure_async):
        window = MainWindow(facade, ui_state_service)
        qtbot.addWidget(window)

        window._on_session_selected(session_runtime_uid(session))

        assert window.mode_panel.isEnabled() is True
        assert window.mode_panel.mode_combo.findText("echo") >= 0

        # Выбор режима через UI.
        window.mode_panel._schedule_async = lambda _coro_factory: None
        window.mode_panel.mode_combo.setCurrentText("echo")
        assert session.modes.active_mode == "echo"

        # Отправляем сообщение: desktop staging'ит ввод и ждёт явного take-in-work.
        window._on_message_sent("hi")
        await asyncio.wait_for(window._active_run_task, timeout=1.0)

        assert entered.is_set() is False
        assert window.mode_menu.text.text() == InputDispatchService.take_in_work_prompt_text()
        assert window.chat_view.send_button.isEnabled() is True
        assert window.mode_panel.status_text.text() == "Idle"

        take_task = asyncio.create_task(
            facade.handle_mode_callback(session_runtime_uid(session), data="take_pending_input")
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert window.chat_view.send_button.isEnabled() is False
        assert window.mode_panel.status_text.text() == "Working"
        assert "MODE:hi" not in window.chat_view.history_browser.toPlainText()

        # Завершаем pipeline и ждём финального ответа.
        release.set()
        await asyncio.wait_for(take_task, timeout=2.0)
        await asyncio.sleep(0)

        assert "MODE:hi" in window.chat_view.history_browser.toPlainText()
        assert window.chat_view.send_button.isEnabled() is True
        assert window.mode_panel.status_text.text() in {"Completed", "Idle"}
