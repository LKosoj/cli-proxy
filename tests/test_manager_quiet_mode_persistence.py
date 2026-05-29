import os

from app.services.state_repository import get_state_repository
from config import load_config
from session import SessionManager


def test_session_manager_persists_manager_quiet_mode(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    s = sm.create(1, "codex", str(tmp_path))
    s.manager_quiet_mode = True
    sm._persist_sessions()

    sm2 = SessionManager(cfg)
    s2 = sm2.get(1, s.id)
    assert s2 is not None
    assert s2.manager_quiet_mode is True


def test_session_manager_restores_manager_quiet_mode_from_nested_state_when_flat_key_missing(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    s = sm.create(1, "codex", str(tmp_path))
    s.manager_quiet_mode = True
    sm._persist_sessions()

    repo = get_state_repository(cfg.defaults.state_path)
    by_chat = repo.load_sessions_by_chat()
    by_chat["1"]["sessions"][s.id].pop("manager_quiet_mode", None)
    repo.save_sessions_by_chat(by_chat)

    sm2 = SessionManager(cfg)
    s2 = sm2.get(1, s.id)
    assert s2 is not None
    assert s2.manager_quiet_mode is True
