import asyncio
import os
from types import SimpleNamespace

import pytest

from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
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


def test_session_service_mode_persists_and_facade_proxies(tmp_path) -> None:
    async def _run():
        cfg = _build_config(tmp_path)
        os.makedirs(os.path.dirname(cfg.defaults.state_path) or ".", exist_ok=True)
        config_service = ConfigService(_InMemoryConfigProvider(cfg))
        task_service = TaskService()
        manager = SessionManager(cfg)
        sessions = SessionService(manager, task_service)
        mode_registry_service = SimpleNamespace(
            list_modes=lambda: [("agent", "Agent")],
            registry=object(),
        )
        facade = ApplicationFacade(
            config_service=config_service,
            session_service=sessions,
            task_service=task_service,
            git_service=None,
            mode_registry_service=mode_registry_service,
        )
        facade.config = cfg

        s = sessions.create_session(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(s)
        assert s.modes.active_mode is None

        assert sessions.set_mode(1, s.id, "agent") is True
        assert sessions.get_session(1, s.id).modes.active_mode == "agent"

        assert facade.get_session_mode(session_uid) == "agent"
        assert facade.set_session_mode(session_uid, None) is True
        assert facade.get_session_mode(session_uid) is None

        # Проверяем, что persist сработал: новый SessionManager подхватывает active_mode=None.
        manager2 = SessionManager(cfg)
        s2 = manager2.get(1, s.id)
        assert s2 is not None
        assert s2.modes.active_mode is None

        # Снова включаем mode и проверяем, что он восстанавливается из файла.
        assert facade.set_session_mode(session_uid, "analyst") is True
        manager3 = SessionManager(cfg)
        s3 = manager3.get(1, s.id)
        assert s3 is not None
        assert s3.modes.active_mode == "analyst"

        with pytest.raises(TypeError):
            facade.get_session_mode(1, s.id)

        with pytest.raises(TypeError):
            facade.set_session_mode(1, s.id, "agent")

        with pytest.raises(TypeError):
            facade.set_session_mode_via_callback(1, s.id, "agent")

        # list_modes доступен и возвращает список строк (не проверяем конкретные значения).
        assert all(isinstance(x, str) for x in facade.list_modes())

    asyncio.run(_run())


def test_facade_reset_session_restores_single_allowed_mode_default(tmp_path) -> None:
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="t",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[],
                user_workdirs={1: [str(tmp_path)]},
                user_modes={1: ["agent"]},
            ),
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
            path=str(tmp_path / "config-user.yaml"),
            miniapp=MiniAppConfig(),
        )
        os.makedirs(os.path.dirname(cfg.defaults.state_path) or ".", exist_ok=True)
        config_service = ConfigService(_InMemoryConfigProvider(cfg))
        task_service = TaskService()
        manager = SessionManager(cfg)
        sessions = SessionService(manager, task_service)
        mode_registry_service = SimpleNamespace(
            list_modes=lambda: [("agent", "Agent")],
            registry=object(),
        )
        facade = ApplicationFacade(
            config_service=config_service,
            session_service=sessions,
            task_service=task_service,
            git_service=None,
            mode_registry_service=mode_registry_service,
        )
        facade.config = cfg

        s = sessions.create_session(1, "dummy", str(tmp_path))
        session_uid = session_runtime_uid(s)
        s.resume_token = "token"
        s.queue.append({"text": "queued", "dest": {"kind": "telegram", "chat_id": 1}})
        s.modes.active_mode = "webmaster"

        assert facade.reset_session(session_uid) is True
        assert s.resume_token is None
        assert list(s.queue) == []
        assert s.modes.active_mode == "agent"

    asyncio.run(_run())


def test_desktop_session_overview_hides_admin_controls_for_simple_user(tmp_path) -> None:
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="t",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[],
                user_workdirs={1: [str(tmp_path)]},
                user_modes={1: ["agent"]},
            ),
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
            path=str(tmp_path / "config-user.yaml"),
            miniapp=MiniAppConfig(),
        )
        os.makedirs(os.path.dirname(cfg.defaults.state_path) or ".", exist_ok=True)
        config_service = ConfigService(_InMemoryConfigProvider(cfg))
        task_service = TaskService()
        manager = SessionManager(cfg)
        sessions = SessionService(manager, task_service)
        mode_registry_service = SimpleNamespace(
            list_modes=lambda: [("agent", "Agent"), ("webmaster", "Webmaster")],
            registry=object(),
        )
        facade = ApplicationFacade(
            config_service=config_service,
            session_service=sessions,
            task_service=task_service,
            git_service=None,
            mode_registry_service=mode_registry_service,
        )
        facade.config = cfg

        session = sessions.create_session(1, "dummy", str(tmp_path))
        session.modes.active_mode = "agent"
        text, rows = facade._build_desktop_session_overview(session_runtime_uid(session))
        callbacks = [item["data"] for row in rows for item in row]

        assert text.startswith("Сессия ")
        assert not any(token.startswith("sess_mode:") for token in callbacks)
        assert not any(token.startswith("sess_orch_toggle:") for token in callbacks)
        assert callbacks == ["sess_close_menu"]

    asyncio.run(_run())


def test_desktop_session_overview_keeps_mode_entry_for_direct_cli_user(tmp_path) -> None:
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(
                token="t",
                whitelist_chat_ids=[1],
                admlist_chat_ids=[],
                user_workdirs={1: [str(tmp_path)]},
                user_modes={1: ["agent", "direct_cli"]},
            ),
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
            path=str(tmp_path / "config-direct-cli.yaml"),
            miniapp=MiniAppConfig(),
        )
        os.makedirs(os.path.dirname(cfg.defaults.state_path) or ".", exist_ok=True)
        config_service = ConfigService(_InMemoryConfigProvider(cfg))
        task_service = TaskService()
        manager = SessionManager(cfg)
        sessions = SessionService(manager, task_service)
        mode_registry_service = SimpleNamespace(
            list_modes=lambda: [("agent", "Agent"), ("webmaster", "Webmaster")],
            registry=object(),
        )
        facade = ApplicationFacade(
            config_service=config_service,
            session_service=sessions,
            task_service=task_service,
            git_service=None,
            mode_registry_service=mode_registry_service,
        )
        facade.config = cfg

        session = sessions.create_session(1, "dummy", str(tmp_path))
        text, rows = facade._build_desktop_session_overview(session_runtime_uid(session))
        callbacks = [item["data"] for row in rows for item in row]

        assert text.startswith("Сессия ")
        assert "sess_mode:agent" in callbacks
        assert "sess_mode:webmaster" not in callbacks
        assert not any(token.startswith("sess_orch_toggle:") for token in callbacks)

    asyncio.run(_run())
