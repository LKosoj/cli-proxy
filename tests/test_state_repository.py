import concurrent.futures
from unittest.mock import MagicMock

import pytest

from sqlalchemy import text

from app.services.state_repository import get_state_repository


def test_state_repository_sessions_roundtrip_and_lookup(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    by_chat = {
        "1": {
            "sessions": {
                "s1": {
                    "active_cli": "codex",
                    "workdir": "/repo",
                    "resume_tokens": {"codex": "r1"},
                    "summary": "sum",
                    "updated_at": 123.0,
                    "name": "n1",
                }
            },
            "active_session_id": "s1",
            "counter": 1,
        }
    }
    repo.save_sessions_by_chat(by_chat)

    loaded = repo.load_sessions_by_chat()
    assert "active_session_id" not in loaded["1"]
    assert loaded["1"]["counter"] == 1

    state = repo.load_state(chat_id=1)
    assert "s1" in state
    assert state["s1"].resume_token == "r1"
    assert state["s1"].tool == "codex"

    found = repo.get_state(tool="codex", workdir="/repo", session_id="s1", chat_id=1)
    assert found is not None
    assert found.session_id == "s1"
    assert found.name == "n1"


def test_state_repository_namespace_update_preserves_sessions(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    repo.save_sessions_by_chat(
        {
            "1": {
                "sessions": {
                    "s1": {
                        "active_cli": "codex",
                        "workdir": "/repo",
                        "resume_tokens": {"codex": "r1"},
                    }
                },
                "counter": 1,
            }
        }
    )

    repo.update_namespace(
        "_manager_resume_pending",
        lambda bucket: {**bucket, "s1": {"prompt": "go", "created_at": 1.0}},
    )

    by_chat = repo.load_sessions_by_chat()
    assert "1" in by_chat
    assert "s1" in by_chat["1"]["sessions"]
    assert repo.read_namespace("_manager_resume_pending")["s1"]["prompt"] == "go"


def test_state_repository_does_not_persist_legacy_active_session_meta(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    repo.save_sessions_by_chat(
        {
            "1": {
                "sessions": {
                    "s1": {
                        "active_cli": "codex",
                        "workdir": "/repo",
                    }
                },
                "active_session_id": "s1",
                "counter": 3,
            }
        }
    )

    with repo._engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT key, value
                FROM chat_meta
                WHERE chat_id=:chat_id
                ORDER BY key
                """
            ),
            {"chat_id": "1"},
        ).mappings().all()

    assert [str(row.get("key") or "") for row in rows] == ["counter"]


def test_state_repository_namespace_supports_scalar_values(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    repo.write_namespace("_agent_project_pending_by_chat", {"1": "session_1"})
    assert repo.read_namespace("_agent_project_pending_by_chat")["1"] == "session_1"

    repo.update_namespace(
        "_agent_project_pending_by_chat",
        lambda bucket: {**bucket, "2": "session_2"},
    )
    pending = repo.read_namespace("_agent_project_pending_by_chat")
    assert pending["1"] == "session_1"
    assert pending["2"] == "session_2"


def test_state_repository_pending_commands_crud(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    payload = {
        "cmd_id": "cmd_1",
        "session_id": "s1",
        "chat_id": 10,
        "command": "echo hello",
        "cwd": "/tmp",
        "reason": "Dangerous",
        "created_at": 100.0,
    }

    repo.set_pending_command("cmd_1", payload)
    loaded = repo.load_pending_commands()
    assert loaded["cmd_1"]["command"] == "echo hello"

    popped = repo.pop_pending_command("cmd_1")
    assert popped is not None
    assert popped["session_id"] == "s1"
    assert repo.load_pending_commands() == {}


def test_state_repository_atomic_session_field_update(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    repo.save_sessions_by_chat(
        {
            "1": {
                "sessions": {
                    "s1": {
                        "active_cli": "codex",
                        "workdir": "/repo",
                        "resume_tokens": {"codex": "r1"},
                        "manager_quiet_mode": False,
                    }
                },
                "counter": 1,
            }
        }
    )

    updated = repo.update_session_fields(
        chat_id=1,
        session_id="s1",
        updates={"manager_quiet_mode": True, "advanced_orchestrator_enabled": True},
    )
    assert updated["manager_quiet_mode"] is True
    assert updated["advanced_orchestrator_enabled"] is True

    assert not hasattr(repo, "set_chat_active_session")
    repo.set_chat_counter(chat_id=1, counter=9)
    loaded = repo.load_sessions_by_chat()
    assert "active_session_id" not in loaded["1"]
    assert int(loaded["1"]["counter"]) == 9


def test_state_repository_concurrent_session_field_updates(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    repo.save_sessions_by_chat(
        {
            "1": {
                "sessions": {
                    "s1": {
                        "active_cli": "codex",
                        "workdir": "/repo",
                        "resume_tokens": {"codex": "r1"},
                    }
                },
                "counter": 1,
            }
        }
    )

    def _worker(i: int) -> None:
        repo.update_session_fields(chat_id=1, session_id="s1", updates={f"k{i}": i})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_worker, range(40)))

    payload = repo.load_sessions_by_chat()["1"]["sessions"]["s1"]
    for i in range(40):
        assert payload[f"k{i}"] == i


def test_state_repository_load_state_supports_nested_cli_payload(tmp_path):
    repo = get_state_repository(str(tmp_path / "state.json"))
    repo.save_sessions_by_chat(
        {
            "1": {
                "sessions": {
                    "s1": {
                        "workdir": "/repo",
                        "cli": {
                            "active_cli": "codex",
                            "resume_tokens": {"codex": "r1"},
                        },
                    }
                },
                "counter": 1,
            }
        }
    )

    state = repo.load_state(chat_id=1)
    assert "s1" in state
    assert state["s1"].tool == "codex"
    assert state["s1"].resume_token == "r1"


def test_get_state_repository_rejects_non_pathlike_state_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(TypeError, match=r"state_path must be str or os\.PathLike"):
        get_state_repository(MagicMock().config.defaults.state_path)

    assert list(tmp_path.iterdir()) == []
