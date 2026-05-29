import os

import pytest

from config import load_config
from session import SessionManager


@pytest.mark.parametrize(
    ("active_mode", "cli_work_type", "executor_profile"),
    [
        ("analyst", None, None),
        ("manager", "analytics", "analyst"),
    ],
)
def test_session_manager_persists_active_mode(tmp_path, active_mode, cli_work_type, executor_profile):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)

    sm = SessionManager(cfg)
    s = sm.create(1, "codex", str(tmp_path))
    s.modes.active_mode = active_mode
    if cli_work_type is not None:
        s.cli_work_type = cli_work_type
    if executor_profile is not None:
        s.executor_profile = executor_profile
    sm._persist_sessions()

    sm2 = SessionManager(cfg)
    s2 = sm2.get(1, s.id)
    assert s2 is not None
    assert s2.modes.active_mode == active_mode
    if cli_work_type is not None:
        assert s2.cli_work_type == cli_work_type
    if executor_profile is not None:
        assert s2.executor_profile == executor_profile
