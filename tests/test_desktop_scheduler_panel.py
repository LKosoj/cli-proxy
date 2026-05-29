from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services import ConfigService, SessionService, TaskService, ThemeService
from app.services.actor_identity import DESKTOP_ACTOR_ID
from app.services.config_service import ConfigProvider
from app.services.project_registry import ProjectOwnershipError, ProjectRegistry
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from desktop.widgets.scheduler_panel import SchedulerPanelWidget
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
    mode_registry = MagicMock()
    mode_registry.registry = object()
    mode_registry.list_modes.return_value = [("agent", "Agent"), ("manager", "Manager")]
    facade = ApplicationFacade(
        config_service=config_service,
        session_service=session_service,
        task_service=task_service,
        theme_service=ThemeService(),
        mode_registry_service=mode_registry,
    )
    facade.config = cfg
    return facade, session_service, cfg


def test_application_facade_scheduler_jobs_enforce_project_ownership(tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)
    alpha_dir = tmp_path / "workdir" / "alpha"
    beta_dir = tmp_path / "workdir" / "beta"
    foreign_dir = tmp_path / "workdir" / "foreign"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    beta_dir.mkdir(parents=True, exist_ok=True)
    foreign_dir.mkdir(parents=True, exist_ok=True)

    session_alpha = sessions.create_desktop_session("dummy", str(alpha_dir))
    session_alpha.name = "Alpha"
    session_beta = sessions.create_desktop_session("dummy", str(beta_dir))
    session_beta.name = "Beta"

    alpha_slug = facade.resolve_scheduler_project_slug(session_alpha.id)
    beta_slug = facade.resolve_scheduler_project_slug(session_beta.id)
    assert alpha_slug is not None
    assert beta_slug is not None

    alpha_target = facade.list_scheduler_notification_targets(project_slug=str(alpha_slug))[0]["session_uid"]
    beta_target = facade.list_scheduler_notification_targets(project_slug=str(beta_slug))[0]["session_uid"]

    created = facade.create_scheduler_job(
        project_slug=str(alpha_slug),
        cron="*/10 * * * *",
        target_mode="agent",
        notification_target_session_uid=str(alpha_target),
        job_name="alpha digest",
    )
    assert created["project_slug"] == str(alpha_slug)
    assert created["owner_id"] == DESKTOP_ACTOR_ID
    assert created["last_status"] == "idle"
    assert created["last_error"] == ""
    assert created["run_count"] == 0
    assert [job["job_id"] for job in facade.list_scheduler_jobs(project_slug=str(alpha_slug))] == [
        created["job_id"]
    ]
    fetched = facade.get_scheduler_job(project_slug=str(alpha_slug), job_id=created["job_id"])
    assert fetched["job_id"] == created["job_id"]
    assert fetched["owner_id"] == DESKTOP_ACTOR_ID

    paused = facade.pause_scheduler_job(project_slug=str(alpha_slug), job_id=created["job_id"])
    assert paused["enabled"] is False
    assert paused["last_status"] == "paused"
    resumed = facade.resume_scheduler_job(project_slug=str(alpha_slug), job_id=created["job_id"])
    assert resumed["enabled"] is True

    with pytest.raises(ProjectOwnershipError):
        facade.create_scheduler_job(
            project_slug=str(alpha_slug),
            cron="*/15 * * * *",
            target_mode="agent",
            notification_target_session_uid=str(beta_target),
            job_name="wrong target",
        )

    foreign_record = ProjectRegistry(cfg.defaults.state_path).register_project(
        path=str(foreign_dir),
        owner_id=999,
        slug="foreign",
        name="Foreign",
    )
    with pytest.raises(ProjectOwnershipError):
        facade.list_scheduler_jobs(project_slug=foreign_record.slug)


def test_application_facade_scheduler_notification_targets_use_topic_title(tmp_path: Path) -> None:
    facade, sessions, _cfg = _build_facade(tmp_path)
    alpha_dir = tmp_path / "workdir" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)

    session_alpha = sessions.create_desktop_session("dummy", str(alpha_dir))
    session_alpha.name = "Alpha Session"

    alpha_slug = facade.resolve_scheduler_project_slug(session_alpha.id)
    assert alpha_slug is not None

    targets = facade.list_scheduler_notification_targets(project_slug=str(alpha_slug))
    assert len(targets) == 1
    assert targets[0]["label"] == f"{session_alpha.id} | {session_alpha.name}"


@pytest.mark.asyncio
async def test_scheduler_panel_widget_manages_jobs_via_facade(qtbot, tmp_path: Path) -> None:
    facade, sessions, _cfg = _build_facade(tmp_path)
    alpha_dir = tmp_path / "workdir" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    session_alpha = sessions.create_desktop_session("dummy", str(alpha_dir))
    session_alpha.name = "Alpha Session"

    alpha_slug = facade.resolve_scheduler_project_slug(session_alpha.id)
    assert alpha_slug is not None

    panel = SchedulerPanelWidget(facade)
    qtbot.addWidget(panel)
    panel.set_context_session(session_alpha.id)

    assert panel.current_project_slug() == str(alpha_slug)
    assert panel.session_selector.count() == 1
    assert panel.session_selector.itemText(0) == f"{session_alpha.id} | {session_alpha.name}"
    assert panel.project_selector.currentData() == str(alpha_slug)

    mode_index = panel.mode_selector.findData("agent")
    assert mode_index >= 0
    panel.mode_selector.setCurrentIndex(mode_index)
    panel.job_name_input.setText("Digest")
    panel.cron_input.setText("*/5 * * * *")
    panel.enabled_checkbox.setChecked(True)
    panel.save_button.click()

    qtbot.waitUntil(lambda: panel.jobs_list.count() == 1)
    created_jobs = facade.list_scheduler_jobs(project_slug=str(alpha_slug))
    assert len(created_jobs) == 1
    assert created_jobs[0]["job_name"] == "Digest"
    assert created_jobs[0]["owner_id"] == DESKTOP_ACTOR_ID
    assert created_jobs[0]["last_status"] == "idle"

    panel.jobs_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: panel._selected_job_id is not None)
    panel.cron_input.setText("0 * * * *")
    panel.save_button.click()

    updated_jobs = facade.list_scheduler_jobs(project_slug=str(alpha_slug))
    assert updated_jobs[0]["cron"] == "0 * * * *"

    panel.pause_button.click()
    qtbot.waitUntil(lambda: facade.list_scheduler_jobs(project_slug=str(alpha_slug))[0]["enabled"] is False)
    paused_jobs = facade.list_scheduler_jobs(project_slug=str(alpha_slug))
    assert paused_jobs[0]["last_status"] == "paused"

    panel.resume_button.click()
    qtbot.waitUntil(lambda: facade.list_scheduler_jobs(project_slug=str(alpha_slug))[0]["enabled"] is True)

    panel.delete_button.click()
    qtbot.waitUntil(lambda: panel.jobs_list.count() == 0)
    assert facade.list_scheduler_jobs(project_slug=str(alpha_slug)) == []


@pytest.mark.asyncio
async def test_scheduler_panel_widget_payload_roundtrip_uses_object_json(qtbot, tmp_path: Path) -> None:
    facade, sessions, _cfg = _build_facade(tmp_path)
    alpha_dir = tmp_path / "workdir" / "alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    session_alpha = sessions.create_desktop_session("dummy", str(alpha_dir))
    session_alpha.name = "Alpha Session"

    alpha_slug = facade.resolve_scheduler_project_slug(session_alpha.id)
    assert alpha_slug is not None

    panel = SchedulerPanelWidget(facade)
    qtbot.addWidget(panel)
    panel.set_context_session(session_alpha.id)

    mode_index = panel.mode_selector.findData("agent")
    assert mode_index >= 0
    panel.mode_selector.setCurrentIndex(mode_index)
    panel.job_name_input.setText("Digest Payload")
    panel.cron_input.setText("*/5 * * * *")
    panel.payload_input.setPlainText('{"intent":"digest","nested":{"step":1}}')
    panel.save_button.click()

    qtbot.waitUntil(lambda: panel.jobs_list.count() == 1)
    created_jobs = facade.list_scheduler_jobs(project_slug=str(alpha_slug))
    assert created_jobs[0]["payload"] == {
        "intent": "digest",
        "nested": {"step": 1},
        "project_slug": str(alpha_slug),
    }

    panel.jobs_list.setCurrentRow(0)
    qtbot.waitUntil(lambda: panel._selected_job_id is not None)
    assert '"project_slug": "{0}"'.format(str(alpha_slug)) in panel.payload_input.toPlainText()
