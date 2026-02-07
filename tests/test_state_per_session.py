
from state import load_state, get_state


def test_state_is_scoped_by_session_id(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    path = tmp_path / "state.json"
    path.write_text(
        "\n".join(
            [
                "{",
                '  "_sessions": {',
                '    "s1": {"tool":"codex","workdir":"/p","resume_token":"r1","summary":"a","updated_at": 1, "name":"n1"},',
                '    "s2": {"tool":"codex","workdir":"/p","resume_token":"r2","summary":"b","updated_at": 2, "name":"n2"}',
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    data = load_state(str(path))
    assert set(data.keys()) == {"s1", "s2"}

    st1 = get_state(str(path), "codex", "/p", session_id="s1")
    assert st1 is not None
    assert st1.resume_token == "r1"

    # Tool/workdir lookup is ambiguous when multiple sessions share them.
    st_amb = get_state(str(path), "codex", "/p")
    assert st_amb is None
