from unittest.mock import MagicMock

from agent.tooling import helpers
from app.services.state_repository import get_state_repository


def test_pending_command_persists_across_memory_reset(tmp_path):
    state_path = tmp_path / "state.json"
    repo = get_state_repository(str(state_path))
    repo.replace_pending_commands({})
    helpers.configure_pending_commands_store(str(state_path))

    cmd_id = helpers._store_pending_command(
        session_id="s1",
        chat_id=123,
        command="echo hello",
        cwd=str(tmp_path),
        reason="Dangerous",
    )
    pending = repo.load_pending_commands()
    assert cmd_id in pending

    # Simulate process restart: in-memory queue is gone, storage must restore it.
    helpers._PENDING_COMMANDS.clear()
    helpers.configure_pending_commands_store(str(state_path))

    restored = helpers.pop_pending_command(cmd_id)
    assert restored is not None
    assert restored.command == "echo hello"
    assert restored.session_id == "s1"
    assert restored.chat_id == 123

    pending_after = repo.load_pending_commands()
    assert cmd_id not in pending_after


def test_configure_pending_commands_store_ignores_non_pathlike_state_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    helpers.configure_pending_commands_store(MagicMock().config.defaults.state_path)

    assert helpers._PENDING_STORE_PATH is None
    assert helpers._PENDING_STORE_REPO is None
    assert list(tmp_path.iterdir()) == []
