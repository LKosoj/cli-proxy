from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import ConfigService, SessionService, TaskService, ThemeService
from app.services.actor_identity import DESKTOP_ACTOR_ID
from app.services.config_service import ConfigProvider
from app.services.project_registry import ProjectOwnershipError, ProjectRegistry
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import AppNotification, ApplicationFacade
from desktop.services.desktop_identity_provider import DesktopIdentityProvider
from desktop.widgets.session_manager import SessionManagerWidget
from session import SessionManager


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig):
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


def _build_config(tmp_path: Path) -> AppConfig:
    workdir = tmp_path / "workdir"
    runtime_dir = tmp_path / "runtime"
    logs_dir = tmp_path / "logs"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
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
            workdir=str(workdir),
            state_path=str(runtime_dir / "state.json"),
            toolhelp_path=str(runtime_dir / "toolhelp.json"),
            log_path=str(logs_dir / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def _build_facade(tmp_path: Path) -> tuple[ApplicationFacade, SessionService, AppConfig]:
    cfg = _build_config(tmp_path)
    config_service = ConfigService(_InMemoryConfigProvider(cfg))
    task_service = TaskService()
    session_service = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        theme_service=ThemeService(),
    )
    facade.config = cfg
    return facade, session_service, cfg


def test_desktop_identity_provider_contract_and_project_scope_isolation(tmp_path: Path) -> None:
    facade_a, sessions_a, cfg_a = _build_facade(tmp_path / "a")
    alpha = tmp_path / "a" / "workdir" / "alpha"
    beta = tmp_path / "a" / "workdir" / "beta"
    alpha.mkdir(parents=True, exist_ok=True)
    beta.mkdir(parents=True, exist_ok=True)

    session_alpha = sessions_a.create_desktop_session("dummy", str(alpha))
    session_alpha.name = "Alpha Session"
    session_beta = sessions_a.create_desktop_session("dummy", str(beta))
    session_beta.name = "Beta Session"

    provider_a = DesktopIdentityProvider(
        project_registry=ProjectRegistry(cfg_a.defaults.state_path),
        session_service=sessions_a,
    )

    assert provider_a.owner_id == DESKTOP_ACTOR_ID

    owned = provider_a.list_owned_projects()
    owned_slugs = {item.slug for item in owned}
    alpha_slug = provider_a.resolve_project_slug(session_alpha.id)
    beta_slug = provider_a.resolve_project_slug(session_beta.id)
    assert alpha_slug in owned_slugs
    assert beta_slug in owned_slugs
    assert alpha_slug != beta_slug

    alpha_targets = provider_a.list_notification_targets(str(alpha_slug))
    beta_targets = provider_a.list_notification_targets(str(beta_slug))
    assert [item.session_id for item in alpha_targets] == [session_alpha.id]
    assert [item.session_id for item in beta_targets] == [session_beta.id]
    assert alpha_targets[0].session_uid == f"desktop:{session_alpha.id}"
    assert provider_a.require_notification_target(str(alpha_slug), session_alpha.id).session_uid == f"desktop:{session_alpha.id}"
    with pytest.raises(ProjectOwnershipError):
        provider_a.require_notification_target(str(alpha_slug), session_beta.id)

    facade_b, sessions_b, cfg_b = _build_facade(tmp_path / "b")
    provider_b = DesktopIdentityProvider(
        project_registry=ProjectRegistry(cfg_b.defaults.state_path),
        session_service=sessions_b,
    )
    assert provider_b.list_owned_projects() == []
    assert facade_a.config is not None
    assert facade_b.config is not None


def test_desktop_identity_provider_rejects_foreign_owned_project(tmp_path: Path) -> None:
    _facade, sessions, cfg = _build_facade(tmp_path)
    foreign = tmp_path / "workdir" / "foreign"
    foreign.mkdir(parents=True, exist_ok=True)

    registry = ProjectRegistry(cfg.defaults.state_path)
    foreign_record = registry.register_project(
        path=str(foreign),
        owner_id=999,
        slug="foreign",
        name="Foreign",
    )

    sessions.create_desktop_session("dummy", str(foreign))

    provider = DesktopIdentityProvider(
        project_registry=registry,
        session_service=sessions,
    )

    with pytest.raises(ProjectOwnershipError):
        provider.require_owned_project(foreign_record.slug)


def test_desktop_sessions_restore_with_canonical_session_uid(tmp_path: Path) -> None:
    cfg = _build_config(tmp_path)
    original_manager = SessionManager(cfg)
    original_service = SessionService(original_manager, TaskService())

    alpha = tmp_path / "workdir" / "alpha"
    alpha.mkdir(parents=True, exist_ok=True)
    session = original_service.create_desktop_session("dummy", str(alpha))
    session.name = "Alpha Session"
    original_manager._persist_sessions()
    session_uid = getattr(getattr(session, "conversation_scope", None), "session_uid", "")

    restored_manager = SessionManager(cfg)
    restored = restored_manager.get_by_uid(session_uid)

    assert restored is not None
    assert restored.id == session.id
    assert restored.chat_id == 0
    assert getattr(restored.conversation_scope, "session_surface", "") == "desktop"
    assert getattr(restored.conversation_scope, "session_uid", "") == session_uid
    assert [item.id for item in SessionService(restored_manager, TaskService()).list_desktop_sessions()] == [session.id]


def test_session_manager_transfer_offer_reads_notification_payload(monkeypatch) -> None:
    from desktop.widgets import session_manager as session_manager_mod

    calls: list[tuple[str, str]] = []

    def _confirm_session_transfer(session_uid: str, source_cli: str) -> bool:
        calls.append((session_uid, source_cli))
        return True

    fake_self = SimpleNamespace(
        facade=SimpleNamespace(confirm_session_transfer=_confirm_session_transfer, ui_language="ru")
    )

    monkeypatch.setattr(
        session_manager_mod.QMessageBox,
        "question",
        lambda *_args, **_kwargs: session_manager_mod.QMessageBox.StandardButton.Yes,
    )

    SessionManagerWidget._handle_transfer_offer(
        fake_self,
        AppNotification(
            event="ui:session_transfer_offer",
            payload={"session_uid": "desktop:s1", "source_cli": "codex", "target_cli": "qwen"},
        ),
    )

    assert calls == [("desktop:s1", "codex")]
