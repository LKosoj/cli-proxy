import json
import os

from config import load_config
from session import SessionManager


def test_session_manager_persists_manager_quiet_mode(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    s = sm.create("codex", str(tmp_path))
    s.manager_quiet_mode = True
    sm._persist_sessions()

    sm2 = SessionManager(cfg)
    s2 = sm2.get(s.id)
    assert s2 is not None
    assert s2.manager_quiet_mode is True


def test_session_manager_restores_manager_quiet_mode_false_when_missing_key(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    s = sm.create("codex", str(tmp_path))
    s.manager_quiet_mode = True
    sm._persist_sessions()

    with open(cfg.defaults.state_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw["_sessions"][s.id].pop("manager_quiet_mode", None)
    with open(cfg.defaults.state_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    sm2 = SessionManager(cfg)
    s2 = sm2.get(s.id)
    assert s2 is not None
    assert s2.manager_quiet_mode is False
