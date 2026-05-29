import os

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import SessionManager
from utils import legacy_sandbox_session_dir, sandbox_session_dir


def _cfg(tmp_path, *, a_enabled: bool = True, b_enabled: bool = True):
    # Use the same executable ("bash") for both tools so availability checks pass.
    tools = {
        "a": ToolConfig(name="a", mode="headless", cmd=["bash", "-lc", "cat"], enabled=a_enabled),
        "b": ToolConfig(name="b", mode="headless", cmd=["bash", "-lc", "cat"], enabled=b_enabled),
    }
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_cli="a",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_session_switch_cli_preserves_tokens(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    s = mgr.create(1, "a", str(tmp_path))

    assert s.tool.name == "a"
    assert s.active_cli == "a"
    assert s.resume_token is None

    s.resume_token = "tok-a"
    assert s.resume_tokens["a"] == "tok-a"

    s.set_active_cli("b")
    assert s.tool.name == "b"
    assert s.active_cli == "b"
    assert s.resume_token is None

    s.resume_token = "tok-b"
    assert s.resume_tokens["b"] == "tok-b"

    s.set_active_cli("a")
    assert s.tool.name == "a"
    assert s.resume_token == "tok-a"


def test_persist_and_restore_multi_cli_tokens(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    s = mgr.create(1, "a", str(tmp_path))
    s.resume_token = "tok-a"
    s.set_active_cli("b")
    s.resume_token = "tok-b"
    mgr._persist_sessions()

    mgr2 = SessionManager(cfg)
    # Replaced deprecated get_single_session_for_chat with sessions_for_chat check
    sessions = list(mgr2.sessions_for_chat(1).values())
    assert len(sessions) == 1
    s2 = sessions[0]
    assert s2 is not None
    # Restores last active_cli.
    assert s2.active_cli == "b"
    assert s2.tool.name == "b"
    assert s2.resume_tokens.get("a") == "tok-a"
    assert s2.resume_tokens.get("b") == "tok-b"
    assert s2.resume_token == "tok-b"


def test_restore_switches_from_disabled_cli_and_keeps_notice(tmp_path):
    cfg = _cfg(tmp_path, a_enabled=True, b_enabled=True)
    mgr = SessionManager(cfg)
    mgr.create(1, "a", str(tmp_path))
    mgr._persist_sessions()

    restored_cfg = _cfg(tmp_path, a_enabled=False, b_enabled=True)
    restored = SessionManager(restored_cfg)
    sessions = list(restored.sessions_for_chat(1).values())

    assert len(sessions) == 1
    restored_session = sessions[0]
    assert restored_session.active_cli == "b"
    assert restored_session.tool.name == "b"
    assert restored_session.cli.pending_switch_notice == {"from": "a", "to": "b"}


def test_session_reset_clears_all_tokens(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    s = mgr.create(1, "a", str(tmp_path))
    s.resume_token = "tok-a"
    s.set_active_cli("b")
    s.resume_token = "tok-b"

    s.reset_all_resume_tokens()
    assert s.resume_token is None
    assert s.resume_tokens.get("a") is None
    assert s.resume_tokens.get("b") is None


def test_create_clears_stale_sandbox_session_dir(tmp_path):
    cfg = _cfg(tmp_path)
    stale_dir = sandbox_session_dir(str(tmp_path), "1_s1")
    os.makedirs(stale_dir, exist_ok=True)
    with open(os.path.join(stale_dir, "stale.txt"), "w", encoding="utf-8") as f:
        f.write("stale")

    mgr = SessionManager(cfg)
    s = mgr.create(1, "a", str(tmp_path))
    assert s.id == "s1"
    assert s.scoped_key == "1_s1"
    assert not os.path.exists(stale_dir)


def test_create_cleans_legacy_raw_sandbox_dir_without_touching_other_scoped_dir(tmp_path):
    cfg = _cfg(tmp_path)
    legacy_dir = legacy_sandbox_session_dir(str(tmp_path), "s1")
    unrelated_dir = sandbox_session_dir(str(tmp_path), "2_s1")
    os.makedirs(legacy_dir, exist_ok=True)
    os.makedirs(unrelated_dir, exist_ok=True)

    mgr = SessionManager(cfg)
    session = mgr.create(1, "a", str(tmp_path))

    assert session.scoped_key == "1_s1"
    assert not os.path.exists(legacy_dir)
    assert os.path.exists(unrelated_dir)


def test_close_clears_sandbox_session_dir(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    s = mgr.create(1, "a", str(tmp_path))

    session_dir = sandbox_session_dir(str(tmp_path), s.scoped_key)
    os.makedirs(session_dir, exist_ok=True)
    with open(os.path.join(session_dir, "payload.txt"), "w", encoding="utf-8") as f:
        f.write("data")
    assert os.path.isdir(session_dir)

    assert mgr.close(1, s.id) is True
    assert not os.path.exists(session_dir)


def test_restore_cleans_legacy_raw_sandbox_dir_once(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    session = mgr.create(1, "a", str(tmp_path))
    legacy_dir = legacy_sandbox_session_dir(str(tmp_path), session.id)
    os.makedirs(legacy_dir, exist_ok=True)
    with open(os.path.join(legacy_dir, "legacy.txt"), "w", encoding="utf-8") as f:
        f.write("legacy")

    restored = SessionManager(cfg)

    assert restored.get(1, session.id) is not None
    assert not os.path.exists(legacy_dir)


def test_session_scoped_key_is_unique_per_chat_and_stable_after_restore(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)

    session_chat_one = mgr.create(1, "a", str(tmp_path))
    session_chat_two = mgr.create(2, "a", str(tmp_path))

    assert session_chat_one.id == "s1"
    assert session_chat_two.id == "s1"
    assert session_chat_one.scoped_key == "1_s1"
    assert session_chat_two.scoped_key == "2_s1"
    assert session_chat_one.scoped_key != session_chat_two.scoped_key

    by_chat = mgr._state_repo.load_sessions_by_chat()
    assert by_chat["1"]["sessions"]["s1"]["scoped_key"] == "1_s1"
    assert by_chat["2"]["sessions"]["s1"]["scoped_key"] == "2_s1"

    restored = SessionManager(cfg)
    restored_one = restored.get(1, "s1")
    restored_two = restored.get(2, "s1")

    assert restored_one is not None
    assert restored_two is not None
    assert restored_one.scoped_key == "1_s1"
    assert restored_two.scoped_key == "2_s1"


def test_close_clears_only_matching_scoped_sandbox_dir(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    session_chat_one = mgr.create(1, "a", str(tmp_path))
    session_chat_two = mgr.create(2, "a", str(tmp_path))

    dir_one = sandbox_session_dir(str(tmp_path), session_chat_one.scoped_key)
    dir_two = sandbox_session_dir(str(tmp_path), session_chat_two.scoped_key)
    os.makedirs(dir_one, exist_ok=True)
    os.makedirs(dir_two, exist_ok=True)

    assert mgr.close(1, session_chat_one.id) is True
    assert not os.path.exists(dir_one)
    assert os.path.exists(dir_two)


def test_session_nested_state_payload_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg)
    s = mgr.create(1, "a", str(tmp_path))
    s.resume_token = "tok-a"
    s.cli_work_type = "analytics"
    s.auto_commands_ran = True
    s.git_busy = True
    s.git_conflict = True
    s.git_conflict_files = ["file_a.py"]
    s.git_conflict_kind = "merge"
    s.modes.active_mode = "manager"
    s.analyst_template_id = "audit"
    s.manager_quiet_mode = True
    s.agent_memory = {"k": "v"}
    s.orchestrator.enabled = True
    s.orchestrator.pending_input = {"text": "next"}
    s.orchestrator.last_mode_output = "report"
    s.orchestrator.last_mode_id = "manager"
    mgr._persist_sessions()

    by_chat = mgr._state_repo.load_sessions_by_chat()
    payload = by_chat["1"]["sessions"][s.id]
    assert payload["scoped_key"] == s.scoped_key
    assert payload["cli"]["active_cli"] == "a"
    assert payload["cli"]["resume_tokens"]["a"] == "tok-a"
    assert payload["cli"]["cli_work_type"] == "analytics"
    assert payload["cli"]["auto_commands_ran"] is True
    assert payload["git"]["busy"] is True
    assert payload["git"]["conflict"] is True
    assert payload["git"]["conflict_files"] == ["file_a.py"]
    assert payload["git"]["conflict_kind"] == "merge"
    assert payload["modes"]["active_mode"] == "manager"
    assert payload["modes"]["analyst_template_id"] == "audit"
    assert payload["modes"]["manager_quiet_mode"] is True
    assert payload["modes"]["agent_memory"] == {"k": "v"}
    assert payload["orchestrator"]["enabled"] is True
    assert payload["orchestrator"]["pending_input"] == {"text": "next"}
    assert payload["orchestrator"]["last_mode_output"] == "report"
    assert payload["orchestrator"]["last_mode_id"] == "manager"

    mgr2 = SessionManager(cfg)
    # Replaced deprecated get_single_session_for_chat with sessions_for_chat check
    sessions = list(mgr2.sessions_for_chat(1).values())
    assert len(sessions) == 1
    s2 = sessions[0]
    assert s2 is not None
    assert s2.scoped_key == s.scoped_key
    assert s2.active_cli == "a"
    assert s2.resume_tokens["a"] == "tok-a"
    assert s2.cli_work_type == "analytics"
    assert s2.auto_commands_ran is True
    assert s2.git.busy is True
    assert s2.git.conflict is True
    assert s2.git.conflict_files == ["file_a.py"]
    assert s2.git.conflict_kind == "merge"
    assert s2.modes.active_mode == "manager"
    assert s2.analyst_template_id == "audit"
    assert s2.manager_quiet_mode is True
    assert s2.agent_memory == {"k": "v"}
    assert s2.orchestrator.enabled is True
    assert s2.orchestrator.pending_input == {"text": "next"}
    assert s2.orchestrator.last_mode_output == "report"
    assert s2.orchestrator.last_mode_id == "manager"
