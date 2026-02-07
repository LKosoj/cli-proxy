import os

from config import load_config
from session import SessionManager


def test_session_manager_persists_manager_enabled(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    s = sm.create("codex", str(tmp_path))
    s.manager_enabled = True
    s.agent_enabled = False
    sm._persist_sessions()

    sm2 = SessionManager(cfg)
    s2 = sm2.get(s.id)
    assert s2 is not None
    assert s2.manager_enabled is True

