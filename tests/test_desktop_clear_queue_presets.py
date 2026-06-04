"""Tests for clear_session_queue and get_presets on ApplicationFacade (Фичи A и B)."""
from __future__ import annotations

from collections import deque
from pathlib import Path

from app.services import ConfigService, SessionService, TaskService
from app.services.config_service import ConfigProvider
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    PresetConfig,
    TelegramConfig,
    ToolConfig,
)
from desktop.services.application_facade import ApplicationFacade
from session import SessionManager, session_runtime_uid


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


def _build_facade(tmp_path: Path, *, presets: list[PresetConfig] | None = None) -> tuple[ApplicationFacade, object]:
    """Создаёт facade + одну сессию; не запускает event-loop."""
    workdir = tmp_path / "workdir"
    runtime = tmp_path / "runtime"
    logs = tmp_path / "logs"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[], admlist_chat_ids=[]),
        tools={
            "dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=list(presets or []),
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=False),
    )
    task_service = TaskService()
    session_service = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=session_service,
        task_service=task_service,
    )
    # Attach config so get_presets() can read it (mimics facade.start() behaviour)
    facade.config = cfg
    proj_dir = workdir / "project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    session = session_service.create_desktop_session("dummy", str(proj_dir))
    return facade, session


# ---------------------------------------------------------------------------
# Фича A: clear_session_queue
# ---------------------------------------------------------------------------

class TestClearSessionQueue:
    def test_clear_nonempty_queue_returns_true(self, tmp_path: Path) -> None:
        facade, session = _build_facade(tmp_path)
        # Populate queue
        session.queue = deque(["task1", "task2", "task3"])
        session_uid = session_runtime_uid(session)

        ok = facade.clear_session_queue(session_uid)

        assert ok is True
        assert len(session.queue) == 0

    def test_clear_empty_queue_returns_true(self, tmp_path: Path) -> None:
        facade, session = _build_facade(tmp_path)
        session.queue = deque()
        session_uid = session_runtime_uid(session)

        ok = facade.clear_session_queue(session_uid)

        assert ok is True
        assert len(session.queue) == 0

    def test_clear_queue_unknown_session_returns_false(self, tmp_path: Path) -> None:
        facade, _session = _build_facade(tmp_path)

        ok = facade.clear_session_queue("nonexistent-uid")

        assert ok is False

    def test_clear_queue_emits_session_updated_notification(self, tmp_path: Path) -> None:
        facade, session = _build_facade(tmp_path)
        session.queue = deque(["task1"])
        session_uid = session_runtime_uid(session)
        notifications: list[str] = []
        facade.subscribe(lambda n: notifications.append(n.event))

        facade.clear_session_queue(session_uid)

        assert "ui:session_updated" in notifications


# ---------------------------------------------------------------------------
# Фича B: get_presets
# ---------------------------------------------------------------------------

class TestGetPresets:
    def test_returns_empty_dict_when_no_presets(self, tmp_path: Path) -> None:
        facade, _session = _build_facade(tmp_path, presets=[])

        result = facade.get_presets()

        assert result == {}

    def test_returns_name_to_prompt_mapping(self, tmp_path: Path) -> None:
        presets = [
            PresetConfig(name="tests", prompt="Run tests and report."),
            PresetConfig(name="lint", prompt="Run linter and report."),
        ]
        facade, _session = _build_facade(tmp_path, presets=presets)

        result = facade.get_presets()

        assert result == {
            "tests": "Run tests and report.",
            "lint": "Run linter and report.",
        }

    def test_returns_empty_dict_when_config_not_set(self, tmp_path: Path) -> None:
        facade, _session = _build_facade(tmp_path)
        # Simulate facade before start() attaches config
        facade.config = None

        result = facade.get_presets()

        assert result == {}
