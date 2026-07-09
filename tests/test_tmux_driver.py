import os
import stat

import pytest

from app.services.cli_backends.tmux_driver import TmuxCommandResult, TmuxDriver, wrap_user_command, write_prompt_temp


def test_wrap_user_command_uses_su_without_losing_argv_boundaries() -> None:
    wrapped = wrap_user_command(["tmux", "send-keys", "-t", "pane", "hello world"], user="claude-bot")

    assert wrapped[:4] == ["su", "-", "claude-bot", "-c"]
    assert "tmux send-keys -t pane 'hello world'" == wrapped[4]


def test_write_prompt_temp_uses_user_only_permissions(tmp_path) -> None:
    path = write_prompt_temp(str(tmp_path), "secret prompt")

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR
    assert os.path.dirname(path) == str(tmp_path)
    assert open(path, encoding="utf-8").read() == "secret prompt"


def test_write_prompt_temp_chowns_for_su_user(tmp_path, monkeypatch) -> None:
    calls = []

    class _User:
        pw_uid = 123
        pw_gid = 456

    monkeypatch.setattr("app.services.cli_backends.tmux_driver.pwd.getpwnam", lambda user: _User())
    monkeypatch.setattr("app.services.cli_backends.tmux_driver.os.chown", lambda path, uid, gid: calls.append((path, uid, gid)))

    path = write_prompt_temp(str(tmp_path), "shared prompt", owner_user="claude-bot")

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR
    assert calls == [(path, 123, 456)]


@pytest.mark.asyncio
async def test_tmux_driver_uses_named_buffer_commands(monkeypatch) -> None:
    calls = []

    async def _run(self, *args, **kwargs):
        calls.append((args, kwargs))
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(TmuxDriver, "run", _run)
    driver = TmuxDriver()

    await driver.load_buffer("/tmp/prompt.txt", buffer_name="cli-proxy-req")
    await driver.paste_buffer("pane:0.0", buffer_name="cli-proxy-req", delete=True)
    await driver.delete_buffer(buffer_name="cli-proxy-req")

    assert calls == [
        (("load-buffer", "-b", "cli-proxy-req", "/tmp/prompt.txt"), {}),
        (("paste-buffer", "-d", "-b", "cli-proxy-req", "-t", "pane:0.0"), {}),
        (("delete-buffer", "-b", "cli-proxy-req"), {"check": False}),
    ]
