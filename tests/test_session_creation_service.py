from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.project_registry import ProjectRegistry
from app.services.session_creation_service import SessionCreationService
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ThreadModeConfig, ToolConfig
from session import SessionManager


def _build_config(tmp_path, *, intent: str) -> AppConfig:
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
        miniapp=MiniAppConfig(),
        thread_mode=ThreadModeConfig(enabled=False),
    )


def _build_bot_app(tmp_path, *, intent: str, thread_manager=None, project_registry=None):
    cfg = _build_config(tmp_path, intent=intent)
    manager = SessionManager(cfg)
    bot_app = SimpleNamespace(
        config=cfg,
        manager=manager,
        project_registry=project_registry or ProjectRegistry(cfg.defaults.state_path),
        session_thread_manager=thread_manager,
        _is_tool_available=lambda _tool: True,
        _expected_tools=lambda: "dummy",
        is_within_root=lambda path, root: True,
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        ui_state=ChatUiState(),
    )
    return bot_app


@pytest.mark.asyncio
async def test_session_creation_service_create_session_registers_project_idempotently(tmp_path) -> None:
    project_path = tmp_path / "project-idempotent"
    project_path.mkdir()
    bot_app = _build_bot_app(tmp_path, intent="idempotent")
    service = SessionCreationService(bot_app)

    first, first_error = await service.create_session(101, "dummy", str(project_path), register_project=True)
    second, second_error = await service.create_session(101, "dummy", str(project_path), register_project=True)

    assert first_error is None
    assert second_error is None
    assert first is not None
    assert second is not None
    assert first.id != second.id

    records = bot_app.project_registry.list_projects(owner_id=101)
    assert len(records) == 1
    assert records[0].path == str(project_path.resolve())


@pytest.mark.asyncio
async def test_session_creation_service_create_from_pending_tool_registers_project_via_authoritative_flow(tmp_path) -> None:
    project_path = tmp_path / "project-pending"
    project_path.mkdir()
    bot_app = _build_bot_app(tmp_path, intent="pending")
    service = SessionCreationService(bot_app)
    ui_key = TelegramUiKey.from_parts(101, 77)
    bot_app.ui_state.pending_new_tool[ui_key] = "dummy"
    bot_app.ui_state.dirs_mode[ui_key] = "new_session"

    session, error = await service.create_from_pending_tool(
        101,
        str(project_path),
        root=str(tmp_path),
        clear_dirs_mode=True,
        message_thread_id=77,
        ui_chat_id=101,
    )

    assert error is None
    assert session is not None
    assert bot_app.ui_state.pending_new_tool == {}
    assert bot_app.ui_state.dirs_mode == {}
    records = bot_app.project_registry.list_projects(owner_id=101)
    assert len(records) == 1
    assert records[0].path == str(project_path.resolve())


class _FailingThreadManager:
    def __init__(self) -> None:
        self.cleanup_calls: list[dict[str, object]] = []

    @staticmethod
    def is_enabled() -> bool:
        return True

    async def ensure_topic_for_session(self, **_kwargs) -> None:
        raise RuntimeError("topic bind failed")

    async def cleanup_closed_session(self, **kwargs) -> None:
        self.cleanup_calls.append(dict(kwargs))


@pytest.mark.asyncio
async def test_session_creation_service_topic_failure_rolls_back_session_without_registering_project(tmp_path) -> None:
    project_path = tmp_path / "project-topic-failure"
    project_path.mkdir()
    thread_manager = _FailingThreadManager()
    bot_app = _build_bot_app(tmp_path, intent="topic_failure", thread_manager=thread_manager)
    service = SessionCreationService(bot_app)
    ui_key = TelegramUiKey.from_parts(101, None)
    bot_app.ui_state.pending_new_tool[ui_key] = "dummy"

    session, error = await service.create_from_pending_tool(
        101,
        str(project_path),
        root=str(tmp_path),
        bot=object(),
        ui_chat_id=101,
    )

    assert session is None
    assert error == (
        "Не удалось создать topic для сессии. "
        "Проверьте, что у бота есть право управлять темами форума."
    )
    assert bot_app.manager.sessions_for_chat(101) == {}
    assert bot_app.project_registry.list_projects(owner_id=101) == []
    assert len(thread_manager.cleanup_calls) == 1


class _BrokenProjectRegistry:
    def register_project(self, **_kwargs):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_session_creation_service_registration_error_rolls_back_created_session(tmp_path) -> None:
    project_path = tmp_path / "project-registry-failure"
    project_path.mkdir()
    bot_app = _build_bot_app(
        tmp_path,
        intent="registry_failure",
        project_registry=_BrokenProjectRegistry(),
    )
    service = SessionCreationService(bot_app)

    session, error = await service.create_session(
        101,
        "dummy",
        str(project_path),
        root=str(tmp_path),
        register_project=True,
    )

    assert session is None
    assert error == "Не удалось зарегистрировать проект."
    assert bot_app.manager.sessions_for_chat(101) == {}


@pytest.mark.asyncio
async def test_session_creation_service_applies_and_persists_default_mode(tmp_path) -> None:
    project_path = tmp_path / "project-default-mode"
    project_path.mkdir()
    bot_app = _build_bot_app(tmp_path, intent="default_mode")

    def _apply_default_mode(session, *, chat_id=None):
        _ = chat_id
        session.modes.active_mode = "agent"
        return "agent"

    bot_app.access_policy_service = SimpleNamespace(apply_default_mode_for_session=_apply_default_mode)
    service = SessionCreationService(bot_app)

    session, error = await service.create_session(101, "dummy", str(project_path))

    assert error is None
    assert session is not None
    assert session.modes.active_mode == "agent"
    restored = bot_app.manager.get(101, session.id)
    assert restored is not None
    assert restored.modes.active_mode == "agent"
