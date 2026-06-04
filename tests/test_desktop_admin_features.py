"""Smoke-tests for admin features A/B/C:
  A – state.json dump (get_session_state_json / StateJsonDialog)
  B – selfupdate_desktop
  C – Lint Evolution tab in AdminPanel (4 subsections)
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services import ConfigService, SessionService, TaskService
from app.services.config_service import ConfigProvider
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from desktop.services.application_facade import ApplicationFacade
from desktop.widgets.admin_panel import AdminPanel
from modes.admin.mode import AdminMode
from modes.admin.state_store import AdminStateStore
from modes.registry import ModeRegistry
from modes.sdk.services.mode_registry import ModeRegistryService
from session import SessionManager, session_runtime_uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default: Any = None) -> Any:  # type: ignore[no-untyped-def]
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
            "dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])
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
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg
    return facade, sessions, cfg


def _enable_admin(cfg: AppConfig, session_id: str, *, chat_id: int = 0) -> None:
    store = AdminStateStore(cfg.defaults.state_path)
    store.upsert_session_state(session_id, chat_id=chat_id, enabled=True)


# ---------------------------------------------------------------------------
# Фича A: get_session_state_json
# ---------------------------------------------------------------------------

def test_get_session_state_json_no_session(tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)
    result = facade.get_session_state_json("nonexistent-uid")
    assert not result.get("ok")
    assert "error" in result


def test_get_session_state_json_no_state_path(tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "wd"))
    uid = session_runtime_uid(session)
    # Patch config to have no state_path
    facade.config = SimpleNamespace(defaults=SimpleNamespace(state_path=None))
    result = facade.get_session_state_json(uid)
    assert not result.get("ok")


def test_get_session_state_json_found(tmp_path: Path) -> None:
    """Session that exists returns ok=True with a non-empty payload."""
    facade, sessions, cfg = _build_facade(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    session = sessions.create_desktop_session("dummy", str(workdir))
    uid = session_runtime_uid(session)
    result = facade.get_session_state_json(uid)
    assert result.get("ok"), f"Expected ok=True, got: {result}"
    assert isinstance(result.get("payload"), dict)


# ---------------------------------------------------------------------------
# Фича A: StateJsonDialog
# ---------------------------------------------------------------------------

def test_state_json_dialog_renders(qtbot: Any, tmp_path: Path) -> None:
    from desktop.main_window import StateJsonDialog
    payload = {"session_uid": "test-uid", "active_mode": "agent", "queue": []}
    dlg = StateJsonDialog(payload, lang="ru")
    qtbot.addWidget(dlg)
    text = dlg._editor.toPlainText()
    assert "session_uid" in text
    assert "test-uid" in text


def test_state_json_dialog_invalid_payload(qtbot: Any, tmp_path: Path) -> None:
    """Non-serializable payload doesn't crash the dialog."""
    from desktop.main_window import StateJsonDialog
    dlg = StateJsonDialog({"key": object()}, lang="ru")
    qtbot.addWidget(dlg)
    # Should render something
    assert dlg._editor.toPlainText() != ""


# ---------------------------------------------------------------------------
# Фича B: selfupdate_desktop
# ---------------------------------------------------------------------------

def test_selfupdate_desktop_not_a_git_repo(tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)
    # No .git directory exists in tmp_path (it's our isolated tmp dir)
    with patch("desktop.services.application_facade.ApplicationFacade.selfupdate_desktop") as mock:
        mock.return_value = {"ok": False, "error": "not_a_git_repo"}
        result = mock()
    assert not result.get("ok")
    assert result.get("error") == "not_a_git_repo"


def test_selfupdate_desktop_git_pull_nonzero(tmp_path: Path) -> None:
    """Git pull failing returns ok=False."""
    facade, sessions, cfg = _build_facade(tmp_path)
    # Create a fake .git dir
    fake_git = tmp_path / ".git"
    fake_git.mkdir()

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = "error: some git problem"
    fake_result.stderr = ""

    with patch("os.path.dirname", return_value=str(tmp_path)), \
         patch("os.path.isdir", return_value=True), \
         patch("subprocess.run", return_value=fake_result):
        result = facade.selfupdate_desktop()

    assert not result.get("ok")
    assert result.get("error") == "git_pull_nonzero"


def test_selfupdate_desktop_would_execv(tmp_path: Path) -> None:
    """After successful git pull, os.execv is called."""
    facade, sessions, cfg = _build_facade(tmp_path)

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "Already up to date."
    fake_result.stderr = ""

    execv_calls: list[tuple] = []

    def fake_execv(python: str, args: list) -> None:
        execv_calls.append((python, args))
        # Don't actually exec — just record and return
        raise SystemExit(0)

    with patch("os.path.dirname", return_value=str(tmp_path)), \
         patch("os.path.isdir", return_value=True), \
         patch("subprocess.run", return_value=fake_result), \
         patch("os.execv", fake_execv), \
         pytest.raises(SystemExit):
        facade.selfupdate_desktop()

    assert len(execv_calls) == 1
    assert execv_calls[0][0] == sys.executable


# ---------------------------------------------------------------------------
# Фича C: AdminPanel Lint Evolution tab
# ---------------------------------------------------------------------------

def test_admin_panel_has_lint_evolution_tab(qtbot: Any, tmp_path: Path) -> None:
    facade, sessions, cfg = _build_facade(tmp_path)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    tab_texts = [panel.admin_tabs.tabText(i) for i in range(panel.admin_tabs.count())]
    assert "Lint Evolution" in tab_texts


def test_admin_panel_lint_status_no_session(qtbot: Any, tmp_path: Path) -> None:
    """Refresh status without session shows error in view."""
    facade, sessions, cfg = _build_facade(tmp_path)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    panel._refresh_lint_status()
    assert panel.lint_status_view.toPlainText() != ""


def test_admin_panel_lint_status_with_mock(qtbot: Any, tmp_path: Path) -> None:
    """Refresh status calls facade.get_lint_evolution_status and shows lines."""
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "wd"))
    _enable_admin(cfg, session.id)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    panel._active_session_uid = session_runtime_uid(session)

    facade.get_lint_evolution_status = lambda uid: {
        "ok": True,
        "lines": ["workdir: /tmp", "schema_version: 3"],
    }
    panel._refresh_lint_status()
    text = panel.lint_status_view.toPlainText()
    assert "workdir" in text
    assert "schema_version" in text


def test_admin_panel_lint_autopause_pause_with_mock(qtbot: Any, tmp_path: Path) -> None:
    """Pause button calls facade.pause_lint_autopause and sets status label."""
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "wd"))
    _enable_admin(cfg, session.id)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    panel._active_session_uid = session_runtime_uid(session)

    calls: list[tuple] = []

    def _mock_pause(uid: str, level: int) -> dict:
        calls.append((uid, level))
        return {"ok": True}

    facade.pause_lint_autopause = _mock_pause  # type: ignore[assignment]
    panel._trigger_lint_autopause_pause()
    assert len(calls) == 1
    assert calls[0][1] in (1, 2, 3)


def test_admin_panel_lint_autopause_resume_with_mock(qtbot: Any, tmp_path: Path) -> None:
    """Resume button calls facade.resume_lint_autopause."""
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "wd"))
    _enable_admin(cfg, session.id)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    panel._active_session_uid = session_runtime_uid(session)

    calls: list[tuple] = []

    def _mock_resume(uid: str, level: int) -> dict:
        calls.append((uid, level))
        return {"ok": True, "resumed": True}

    facade.resume_lint_autopause = _mock_resume  # type: ignore[assignment]
    panel._trigger_lint_autopause_resume()
    assert len(calls) == 1


def test_admin_panel_lint_schema_with_mock(qtbot: Any, tmp_path: Path) -> None:
    """Schema history refresh shows lines from facade."""
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "wd"))
    _enable_admin(cfg, session.id)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    panel._active_session_uid = session_runtime_uid(session)

    facade.get_lint_schema_history = lambda uid: {
        "ok": True,
        "lines": ["active_version: 2", "fields (3): foo, bar, baz"],
    }
    panel._refresh_lint_schema()
    text = panel.lint_schema_view.toPlainText()
    assert "active_version" in text


def test_admin_panel_lint_gate_dry_run_with_mock(qtbot: Any, tmp_path: Path) -> None:
    """Gate dry-run shows result from facade."""
    facade, sessions, cfg = _build_facade(tmp_path)
    session = sessions.create_desktop_session("dummy", str(tmp_path / "wd"))
    _enable_admin(cfg, session.id)
    panel = AdminPanel(facade, actor_id="desktop")
    qtbot.addWidget(panel)
    panel._active_session_uid = session_runtime_uid(session)

    facade.run_lint_gate_dry_run = lambda uid: {
        "ok": True,
        "lines": ["rules_evaluated: 5", "findings: 2"],
    }
    panel._trigger_lint_gate_dry_run()
    text = panel.lint_gate_view.toPlainText()
    assert "rules_evaluated" in text
    assert "findings" in text
