from __future__ import annotations

import asyncio
from pathlib import Path

from app.services import ConfigService, SessionService, TaskService, ThemeService
from app.services.config_service import ConfigProvider
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from desktop.widgets.admin_panel import AdminPanel
from modes.admin.mode import AdminMode
from modes.admin.state_store import AdminStateStore
from modes.registry import ModeRegistry
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager, session_runtime_uid


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


def _build_facade(tmp_path: Path) -> tuple[ApplicationFacade, SessionService, AppConfig]:
    cfg = _build_config(tmp_path)
    mode_registry = ModeRegistry()
    mode_registry.register(AdminMode())
    mode_registry_service = ModeRegistryService(mode_registry)

    task_service = TaskService()
    sessions = SessionService(SessionManager(cfg), task_service)
    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=sessions,
        task_service=task_service,
        theme_service=ThemeService(),
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    return facade, sessions, cfg


def _enable_admin_for_session(cfg: AppConfig, session_id: str, *, chat_id: int = 0) -> None:
    store = AdminStateStore(cfg.defaults.state_path)
    store.upsert_session_state(session_id, chat_id=chat_id, enabled=True)


def test_admin_panel_renders_admin_status_payload_for_selected_session(qtbot, tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)

    session_a = sessions.create_desktop_session("dummy", str(tmp_path / "session-a"))
    session_b = sessions.create_desktop_session("dummy", str(tmp_path / "session-b"))
    pass

    _enable_admin_for_session(cfg, session_a.id)
    _enable_admin_for_session(cfg, session_b.id)

    session_a.admin_runtime_status = {
        "pipeline_status": "running",
        "analyzer_status": "completed",
        "analyzer_message": "http_502 -> notify_admin (medium)",
        "executor_status": "completed",
        "executor_message": "Notification sent to admin channel.",
        "status_updated_at": 1.0,
    }
    session_b.admin_runtime_status = {
        "pipeline_status": "idle",
        "analyzer_status": "idle",
        "analyzer_message": "Waiting for the next admin pipeline iteration.",
        "executor_status": "idle",
        "executor_message": "No execution requested yet.",
        "status_updated_at": 2.0,
    }

    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)

    assert [panel.admin_tabs.tabText(i) for i in range(panel.admin_tabs.count())] == [
        "Обзор",
        "Операции",
        "Мониторинг",
        "Config",
        "Chat",
        "Autonomy",
        "Scheduler",
    ]
    assert panel.active_session_uid == session_runtime_uid(session_a)
    assert panel.session_selector.itemText(0) == f"{session_a.id} | {session_a.name}"
    assert panel.session_selector.itemText(1) == f"{session_b.id} | {session_b.name}"
    assert panel.state_stack.currentWidget() == panel.enabled_page
    assert panel.pipeline_status_value_label.text() == "running"
    assert panel.analyzer_status_value_label.text() == "completed"
    assert panel.analyzer_detail_value_label.text() == "http_502 -> notify_admin (medium)"
    assert panel.executor_status_value_label.text() == "completed"
    assert panel.executor_detail_value_label.text() == "Notification sent to admin channel."

    index = panel.session_selector.findData(session_runtime_uid(session_b))
    assert index >= 0
    panel.session_selector.setCurrentIndex(index)

    assert panel.active_session_uid == session_runtime_uid(session_b)
    assert panel.state_stack.currentWidget() == panel.enabled_page
    assert panel.pipeline_status_value_label.text() == "idle"
    assert panel.analyzer_status_value_label.text() == "idle"
    assert panel.executor_status_value_label.text() == "idle"
    assert "Mode tasks: idle" in panel.runtime_flags_value_label.text()


def test_admin_panel_refreshes_runtime_status_after_facade_notification(qtbot, tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)

    session = sessions.create_desktop_session("dummy", str(tmp_path / "session-live"))
    pass
    _enable_admin_for_session(cfg, session.id)

    session.admin_runtime_status = {
        "pipeline_status": "running",
        "analyzer_status": "running",
        "analyzer_message": "Analyzer inspects the latest monitor snapshot.",
        "executor_status": "idle",
        "executor_message": "Waiting for analyzer decision.",
        "status_updated_at": 10.0,
    }

    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)

    assert panel.analyzer_status_value_label.text() == "running"
    assert panel.executor_status_value_label.text() == "idle"

    session.admin_runtime_status = {
        "pipeline_status": "running",
        "analyzer_status": "completed",
        "analyzer_message": "restart_nginx (high)",
        "executor_status": "failed",
        "executor_message": "Restart command failed with exit code 1.",
        "status_updated_at": 11.0,
    }

    facade.notify("task:completed", session_uid=session_runtime_uid(session))

    qtbot.waitUntil(lambda: panel.executor_status_value_label.text() == "failed")
    assert panel.analyzer_status_value_label.text() == "completed"
    assert panel.executor_detail_value_label.text() == "Restart command failed with exit code 1."


def test_admin_panel_scheduler_panel_tracks_selected_session_scope(qtbot, tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)

    alpha_dir = tmp_path / "project-alpha"
    beta_dir = tmp_path / "project-beta"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    beta_dir.mkdir(parents=True, exist_ok=True)

    session_a = sessions.create_desktop_session("dummy", str(alpha_dir))
    session_a.name = "Alpha"
    session_b = sessions.create_desktop_session("dummy", str(beta_dir))
    session_b.name = "Beta"

    _enable_admin_for_session(cfg, session_a.id)
    _enable_admin_for_session(cfg, session_b.id)

    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)

    alpha_slug = facade.resolve_scheduler_project_slug(session_runtime_uid(session_a))
    beta_slug = facade.resolve_scheduler_project_slug(session_runtime_uid(session_b))
    assert alpha_slug is not None
    assert beta_slug is not None
    assert panel.scheduler_panel.current_project_slug() == str(alpha_slug)

    index = panel.session_selector.findData(session_runtime_uid(session_b))
    assert index >= 0
    panel.session_selector.setCurrentIndex(index)

    assert panel.active_session_uid == session_runtime_uid(session_b)
    assert panel.scheduler_panel.current_project_slug() == str(beta_slug)


def test_desktop_admin_actions_keep_status_active_after_rescan(tmp_path: Path) -> None:
    async def _run() -> None:
        facade, sessions, _cfg = _build_facade(tmp_path)
        workdir = tmp_path / "session-rescan"
        workdir.mkdir()
        session = sessions.create_desktop_session("dummy", str(workdir))
        session_uid = session_runtime_uid(session)

        enabled = await facade.run_admin_session_action(session_uid, action="enable")
        assert enabled is True
        payload = facade.get_admin_status_payload(session_uid) or {}
        assert payload.get("active") is True

        rescanned = await facade.run_admin_session_action(session_uid, action="rescan")
        assert rescanned is True
        payload = facade.get_admin_status_payload(session_uid) or {}
        assert payload.get("active") is True

    asyncio.run(_run())


def test_admin_panel_renders_pending_skill_installs_and_triggers_actions(qtbot, tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "session-skill-approvals"))
    _enable_admin_for_session(cfg, session.id)

    base_payload = {
        "mode": "admin",
        "session_uid": session_runtime_uid(session),
        "session_id": session.id,
        "active": True,
        "busy": False,
        "run_lock_locked": False,
        "tick_active": False,
        "mode_tasks_running": False,
        "pipeline_status": "idle",
        "monitor_status": "idle",
        "analyzer_status": "idle",
        "analyzer_message": "",
        "executor_status": "idle",
        "executor_message": "",
        "notifier_status": "idle",
        "notifier_message": "",
        "scan_status": "ready",
        "scan_error": None,
        "initialized_at": None,
        "last_scan_at": None,
        "pinned_cli": {},
        "pinned_executor_profile": None,
        "component_readiness": {"monitor": True, "analyzer": True, "executor": True, "notifier": True},
        "last_monitor_snapshot": {"server_id": "mb_test", "status": "ok"},
        "last_analyzer_decision": {"action": "notify_admin", "confidence": "medium"},
        "last_action": {"action": "restart_nginx", "status": "done"},
        "pending_ask_user": {"count": 1, "active": True, "current": {"question": "Перезапустить nginx?"}},
        "pending_approvals": {"count": 2, "active": True},
        "pending_skill_installs": {
            "count": 2,
            "active": True,
            "items": [
                {
                    "approval_id": "approval-1",
                    "skill_id": "playwright-cli-local",
                    "mode_id": "agent",
                    "phase": "execute",
                    "source": "ref:owner-repo-skill",
                },
                {
                    "approval_id": "approval-2",
                    "skill_id": "xlsx-local",
                    "mode_id": "agent",
                    "phase": "execute",
                    "source": "ref:owner-repo-skill",
                },
            ],
        },
        "mute_state": {"muted_until_ts": None, "muted": False},
        "recent_incidents": [{"incident_id": "inc-1", "payload": {"decision": {"diagnosis": "http_502"}}}],
        "recent_admin_actions": [{"action_id": "act-1", "payload": {"decision": {"action": "restart_nginx"}}}],
        "approved_overrides": [{"action": "restart_nginx", "ttl": 3600}],
    }

    action_log: list[tuple[str, str, str]] = []

    def _get_status(_session_uid: str) -> dict:
        return dict(base_payload)

    async def _approve(session_uid: str, *, approval_id: str) -> dict:
        action_log.append(("approve", session_uid, approval_id))
        base_payload["pending_skill_installs"] = {
            "count": 1,
            "active": True,
            "items": [dict(base_payload["pending_skill_installs"]["items"][1])],
        }
        return {
            "status": "ok",
            "approval_id": approval_id,
            "skill_id": "playwright-cli-local",
            "message": "Skill `playwright-cli-local` установлен локально после approve.",
            "manifest_path": str(tmp_path / "session-skill-approvals" / ".cli-proxy" / "skills" / "playwright-cli-local" / "SKILL.md"),
        }

    async def _reject(session_uid: str, *, approval_id: str) -> dict:
        action_log.append(("reject", session_uid, approval_id))
        base_payload["pending_skill_installs"] = {
            "count": 0,
            "active": False,
            "items": [],
        }
        return {
            "status": "ok",
            "approval_id": approval_id,
            "skill_id": "xlsx-local",
            "message": "Pending установка skill `xlsx-local` отклонена.",
            "manifest_path": None,
        }

    facade.get_admin_status_payload = _get_status  # type: ignore[method-assign]
    facade.approve_pending_skill_install = _approve  # type: ignore[method-assign]
    facade.reject_pending_skill_install = _reject  # type: ignore[method-assign]

    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)

    assert panel.pending_value_label.text() == "ask_user 1 | approvals 2 | active"
    assert panel.skill_installs_value_label.text() == "2 pending | 2 listed | active"
    assert panel.mute_state_value_label.text() == "off"
    assert panel.recent_incidents_value_label.text() == "1 | inc-1 | http_502"
    assert panel.recent_actions_value_label.text() == "1 | act-1 | restart_nginx"
    assert panel.approved_overrides_value_label.text() == "1 | restart_nginx"
    assert "{" not in panel.recent_incidents_value_label.text()
    assert "{" not in panel.recent_actions_value_label.text()
    assert panel.skill_approval_selector.count() == 3
    assert panel.skill_approval_selector.currentData() == "approval-1"
    assert panel.skill_approve_button.isEnabled() is True
    assert panel.skill_reject_button.isEnabled() is True

    panel.skill_approve_button.click()
    qtbot.waitUntil(lambda: ("approve", session_runtime_uid(session), "approval-1") in action_log)
    qtbot.waitUntil(lambda: "установлен локально" in panel.skill_action_result_label.text())

    assert panel.skill_installs_value_label.text() == "1 pending | 1 listed | active"
    assert panel.skill_approval_selector.currentData() == "approval-2"

    panel.skill_reject_button.click()
    qtbot.waitUntil(lambda: ("reject", session_runtime_uid(session), "approval-2") in action_log)
    qtbot.waitUntil(lambda: "отклонена" in panel.skill_action_result_label.text())

    assert panel.skill_installs_value_label.text() == "0 pending"
    assert panel.skill_approval_selector.currentData() in ("", None)
    assert panel.skill_approve_button.isEnabled() is False
    assert panel.skill_reject_button.isEnabled() is False
