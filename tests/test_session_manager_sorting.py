import os

from config import load_config
from session import SessionManager


def test_sessions_for_chat_sorted_by_workdir_dirname(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.workdir = str(tmp_path)
    cfg.defaults.state_path = str(tmp_path / "state.json")

    a_dir = tmp_path / "alpha_repo"
    m_dir = tmp_path / "MiddleRepo"
    z_dir = tmp_path / "zeta_repo"
    for path in (z_dir, a_dir, m_dir):
        path.mkdir(parents=True, exist_ok=True)

    manager = SessionManager(cfg)
    chat_id = 100
    s_z = manager.create(chat_id, "codex", str(z_dir))
    s_a = manager.create(chat_id, "codex", str(a_dir))
    s_m = manager.create(chat_id, "codex", str(m_dir))

    ordered_ids = list(manager.sessions_for_chat(chat_id).keys())
    assert ordered_ids == [s_a.id, s_m.id, s_z.id]
