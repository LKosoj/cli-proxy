from app.services.state_repository import get_state_repository


def test_state_is_scoped_by_session_id(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    path = tmp_path / "state.json"
    repo = get_state_repository(str(path))
    chat_id = 123
    repo.save_sessions_by_chat(
        {
            str(chat_id): {
                "sessions": {
                    "s1": {
                        "active_cli": "codex",
                        "workdir": "/p",
                        "resume_tokens": {"codex": "r1"},
                        "summary": "a",
                        "updated_at": 1,
                        "name": "n1",
                    },
                    "s2": {
                        "active_cli": "codex",
                        "workdir": "/p",
                        "resume_tokens": {"codex": "r2"},
                        "summary": "b",
                        "updated_at": 2,
                        "name": "n2",
                    },
                }
            }
        }
    )

    data = repo.load_state(chat_id=chat_id)
    assert set(data.keys()) == {"s1", "s2"}

    st1 = repo.get_state(tool="codex", workdir="/p", session_id="s1", chat_id=chat_id)
    assert st1 is not None
    assert st1.resume_token == "r1"

    # Tool/workdir lookup is ambiguous when multiple sessions share them.
    st_amb = repo.get_state(tool="codex", workdir="/p", chat_id=chat_id)
    assert st_amb is None


def test_repository_writes_active_cli_schema(tmp_path):
    path = tmp_path / "state.json"
    repo = get_state_repository(str(path))
    chat_id = 77
    repo.update_session_fields(
        chat_id=chat_id,
        session_id="s1",
        updates={
            "active_cli": "codex",
            "workdir": "/repo",
            "resume_tokens": {"codex": "r1"},
            "summary": "sum",
            "updated_at": 123.0,
            "name": "n1",
        },
    )
    by_chat = repo.load_sessions_by_chat()
    s1 = by_chat[str(chat_id)]["sessions"]["s1"]
    assert s1.get("active_cli") == "codex"
    assert s1.get("resume_tokens", {}).get("codex") == "r1"
    assert "tool" not in s1
    assert "resume_token" not in s1
