from __future__ import annotations

import json
import sqlite3

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager
from app.services.state_repository import JsonStateRepository, get_state_repository


def _build_config(tmp_path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_state_repository_rejects_legacy_sessions_without_scope_columns(tmp_path) -> None:
    state_path = tmp_path / "legacy_state.json"
    db_path = JsonStateRepository._derive_db_path(str(state_path))

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                chat_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, session_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat_meta (
                chat_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(chat_id, key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE namespaces (
                namespace TEXT NOT NULL,
                item_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(namespace, item_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE repository_meta (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions(chat_id, session_id, payload, updated_at) VALUES (?, ?, ?, ?)",
            (
                "11",
                "s1",
                json.dumps({"active_cli": "dummy", "workdir": "/repo"}, separators=(",", ":")),
                0.0,
            ),
        )
        conn.execute(
            "INSERT INTO sessions(chat_id, session_id, payload, updated_at) VALUES (?, ?, ?, ?)",
            (
                "11",
                "s2",
                json.dumps(
                    {"active_cli": "dummy", "workdir": "/repo-thread", "message_thread_id": 77},
                    separators=(",", ":"),
                ),
                0.0,
            ),
        )

    with pytest.raises(RuntimeError, match=r"sessions table is missing required columns"):
        get_state_repository(str(state_path))


def test_session_manager_roundtrip_persists_conversation_scope_fields(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="thread_scope")
    workdir = tmp_path / "thread-session"
    workdir.mkdir()

    manager = SessionManager(cfg)
    session = manager.create(1, "dummy", str(workdir), message_thread_id=42)

    assert session.conversation_scope is not None
    assert session.conversation_scope.message_thread_id == 42
    assert session.conversation_scope.session_uid == "thread:1:42"
    assert session.conversation_scope.session_surface == "thread"

    payload = manager._state_repo.load_sessions_by_chat()["1"]["sessions"][session.id]
    assert payload["message_thread_id"] == 42
    assert payload["session_uid"] == "thread:1:42"
    assert payload["session_surface"] == "thread"

    restored = SessionManager(cfg)
    restored_session = restored.get(1, session.id)
    assert restored_session is not None
    assert restored_session.conversation_scope is not None
    assert restored_session.conversation_scope.message_thread_id == 42
    assert restored_session.conversation_scope.session_uid == "thread:1:42"


def test_session_manager_persist_session_rewrites_legacy_flat_payload_to_nested_only(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="flat_cleanup")
    state_path = cfg.defaults.state_path
    db_path = JsonStateRepository._derive_db_path(str(state_path))

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                chat_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                session_uid TEXT NOT NULL DEFAULT '',
                session_surface TEXT NOT NULL DEFAULT 'chat',
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, session_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat_meta (
                chat_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(chat_id, key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE namespaces (
                namespace TEXT NOT NULL,
                item_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(namespace, item_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE repository_meta (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions(chat_id, session_id, payload, session_uid, session_surface, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "1",
                "s1",
                json.dumps(
                    {
                        "workdir": str(tmp_path / "legacy-workdir"),
                        "active_cli": "dummy",
                        "resume_tokens": {"dummy": "resume-1"},
                        "active_mode": "manager",
                        "manager_quiet_mode": True,
                        "advanced_orchestrator_enabled": True,
                        "orchestrator_pending_input": {"text": "next"},
                    },
                    separators=(",", ":"),
                ),
                "chat:1",
                "chat",
                0.0,
            ),
        )

    manager = SessionManager(cfg)
    restored = manager.get(1, "s1")
    assert restored is not None
    assert restored.resume_tokens == {"dummy": "resume-1"}
    assert restored.modes.active_mode == "manager"
    assert restored.modes.manager_quiet_mode is True
    assert restored.orchestrator.enabled is True
    assert restored.orchestrator.pending_input == {"text": "next"}

    assert manager.persist_session(1, "s1") is True

    payload = manager._state_repo.load_sessions_by_chat()["1"]["sessions"]["s1"]
    assert payload["cli"]["active_cli"] == "dummy"
    assert payload["cli"]["resume_tokens"] == {"dummy": "resume-1"}
    assert payload["modes"]["active_mode"] == "manager"
    assert payload["modes"]["manager_quiet_mode"] is True
    assert payload["orchestrator"]["enabled"] is True
    assert payload["orchestrator"]["pending_input"] == {"text": "next"}
    for legacy_key in (
        "active_cli",
        "resume_tokens",
        "active_mode",
        "cli_work_type",
        "analyst_template_id",
        "manager_quiet_mode",
        "advanced_orchestrator_enabled",
        "agent_memory",
        "git_busy",
        "git_conflict",
        "git_conflict_files",
        "git_conflict_kind",
        "orchestrator_pending_input",
        "orchestrator_last_mode_output",
        "orchestrator_last_mode_id",
    ):
        assert legacy_key not in payload


def test_session_manager_get_by_uid_rejects_legacy_chat_session_tokens(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="uid_cleanup")
    workdir = tmp_path / "uid-session"
    workdir.mkdir()

    manager = SessionManager(cfg)
    session = manager.create(1, "dummy", str(workdir))
    legacy_session_token = str(session.id)
    legacy_scoped_token = f"1:{legacy_session_token}"

    assert session.conversation_scope is not None
    assert manager.get_by_uid(session.conversation_scope.session_uid) is session
    assert manager.get_by_uid(legacy_session_token) is None
    assert manager.get_by_uid(legacy_scoped_token) is None


def test_conversation_scope_state_is_isolated_between_sequential_state_paths(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="scope_a")
    cfg_b = _build_config(tmp_path, intent="scope_b")
    workdir_a = tmp_path / "scope-a"
    workdir_b = tmp_path / "scope-b"
    workdir_a.mkdir()
    workdir_b.mkdir()

    manager_a = SessionManager(cfg_a)
    session_a = manager_a.create(1, "dummy", str(workdir_a), message_thread_id=55)
    assert session_a.conversation_scope is not None
    assert session_a.conversation_scope.session_uid == "thread:1:55"

    manager_b = SessionManager(cfg_b)
    assert manager_b.sessions_for_chat(1) == {}
    session_b = manager_b.create(1, "dummy", str(workdir_b))
    assert session_b.conversation_scope is not None
    assert session_b.conversation_scope.message_thread_id is None
    assert session_b.conversation_scope.session_uid == "chat:1"
