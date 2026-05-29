import os
import sys

from app.services.state_repository import get_state_repository
from config import load_config
from session import SessionManager


def test_persist_sessions_does_not_require_import_system_during_shutdown(tmp_path, monkeypatch):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    created = sm.create(1, "codex", str(tmp_path))
    created.state_summary = "ok"

    monkeypatch.setattr(sys, "meta_path", None)

    # Must not fail with ImportError("sys.meta_path is None, Python is likely shutting down").
    sm._persist_sessions()

    repo = get_state_repository(cfg.defaults.state_path)
    by_chat = repo.load_sessions_by_chat()
    assert "1" in by_chat
    sessions = (by_chat.get("1") or {}).get("sessions") or {}
    assert created.id in sessions
