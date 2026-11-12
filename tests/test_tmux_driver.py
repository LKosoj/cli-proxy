import asyncio
import os
import stat
import subprocess

import pytest

from app.services.cli_backends.tmux_driver import (
    TmuxCommandResult,
    TmuxDriver,
    TmuxDriverError,
    wrap_user_command,
    write_prompt_temp,
)


@pytest.mark.asyncio
async def test_tmux_driver_run_returns_subprocess_result(monkeypatch) -> None:
    calls = {}

    class _Process:
        pid = 123
        returncode = 0

        def communicate(self, timeout=None):
            calls["timeout"] = timeout
            return b"pane output", b""

    def _popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(TmuxDriver, "tmux_available", staticmethod(lambda: True))
    monkeypatch.setattr("app.services.cli_backends.tmux_driver.subprocess.Popen", _popen)

    result = await TmuxDriver(timeout_sec=1.5).run("capture-pane", "-p")

    assert calls == {
        "argv": ["tmux", "capture-pane", "-p"],
        "kwargs": {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
        },
        "timeout": 1.5,
    }
    assert result == TmuxCommandResult(returncode=0, stdout="pane output", stderr="")


@pytest.mark.asyncio
async def test_tmux_driver_run_reaps_process_after_timeout(monkeypatch) -> None:
    calls = []

    class _Process:
        pid = 456
        returncode = -9

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd=["tmux", "capture-pane"], timeout=timeout)
            return b"", b"killed"

    monkeypatch.setattr(TmuxDriver, "tmux_available", staticmethod(lambda: True))
    monkeypatch.setattr("app.services.cli_backends.tmux_driver.subprocess.Popen", lambda *_args, **_kwargs: _Process())
    monkeypatch.setattr(
        "app.services.cli_backends.tmux_driver.os.killpg",
        lambda pid, sig: calls.append(("killpg", pid, sig)),
    )

    with pytest.raises(TmuxDriverError, match="tmux command timed out: capture-pane"):
        await TmuxDriver(timeout_sec=0.25).run("capture-pane")

    assert calls == [
        ("communicate", 0.25),
        ("killpg", 456, 9),
        ("communicate", None),
    ]


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


def test_write_prompt_temp_neutralizes_embedded_esc(tmp_path) -> None:
    # Вставленный ESC (например, из скопированного в промпт лога с "\x1b[201~")
    # закрыл бы bracketed-paste рамку tmux раньше времени, и хвост промпта CLI
    # получил бы как нажатия клавиш вместо текста.
    path = write_prompt_temp(str(tmp_path), "лог:\x1b[201~хвост")

    written = open(path, encoding="utf-8").read()
    assert "\x1b" not in written
    assert written == "лог:␛[201~хвост"


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
        (("paste-buffer", "-p", "-d", "-b", "cli-proxy-req", "-t", "pane:0.0"), {}),
        (("delete-buffer", "-b", "cli-proxy-req"), {"check": False}),
    ]


@pytest.mark.asyncio
async def test_tmux_driver_pastes_with_bracket_markers(monkeypatch) -> None:
    # Без скобочных маркеров CLI считает вставкой всё, что пришло следом, и Enter
    # попадает в текст переводом строки: промпт остаётся висеть в поле ввода.
    calls = []

    async def _run(self, *args, **kwargs):
        calls.append(args)
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(TmuxDriver, "run", _run)

    await TmuxDriver().paste_buffer("pane:0.0")

    assert calls == [("paste-buffer", "-p", "-t", "pane:0.0")]


@pytest.mark.asyncio
async def test_tmux_driver_new_session_without_env_matches_previous_argv(monkeypatch) -> None:
    # Обратная совместимость: без env итоговый argv не должен отличаться от того,
    # что было до появления параметра env (иначе ломаем уже существующих вызывающих).
    calls = []

    async def _run(self, *args, **kwargs):
        calls.append(args)
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(TmuxDriver, "run", _run)

    await TmuxDriver().new_session("sess", workdir="/tmp/work", command=["claude"])

    assert calls == [
        ("new-session", "-d", "-x", "200", "-y", "50", "-s", "sess", "-c", "/tmp/work", "claude"),
    ]


@pytest.mark.asyncio
async def test_tmux_driver_new_session_passes_env_before_workdir(monkeypatch) -> None:
    # -e должен идти до -c: native-хуки внутри панели наследуют его только если
    # переменная попала в окружение сессии до старта первого процесса.
    calls = []

    async def _run(self, *args, **kwargs):
        calls.append(args)
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(TmuxDriver, "run", _run)

    await TmuxDriver().new_session(
        "sess",
        workdir="/tmp/work",
        command=["claude"],
        env={"CLI_PROXY_SESSION_UID": "chat:1:s1", "CLI_PROXY_PANE_ID": "sess"},
    )

    assert calls == [
        (
            "new-session", "-d", "-x", "200", "-y", "50", "-s", "sess",
            "-e", "CLI_PROXY_SESSION_UID=chat:1:s1",
            "-e", "CLI_PROXY_PANE_ID=sess",
            "-c", "/tmp/work", "claude",
        ),
    ]


@pytest.mark.asyncio
async def test_tmux_driver_kill_session_treats_missing_session_as_absent(monkeypatch) -> None:
    async def _run(self, *args, **kwargs):
        return TmuxCommandResult(returncode=1, stdout="", stderr="can't find session: missing")

    monkeypatch.setattr(TmuxDriver, "run", _run)

    assert await TmuxDriver().kill_session("missing") is False


@pytest.mark.asyncio
async def test_tmux_driver_kill_session_raises_for_real_error(monkeypatch) -> None:
    async def _run(self, *args, **kwargs):
        return TmuxCommandResult(returncode=1, stdout="", stderr="permission denied")

    monkeypatch.setattr(TmuxDriver, "run", _run)

    with pytest.raises(TmuxDriverError, match="permission denied"):
        await TmuxDriver().kill_session("blocked")


@pytest.mark.asyncio
async def test_tmux_driver_types_literal_characters_separately(monkeypatch):
    calls = []

    async def _run(self, *args, **kwargs):
        calls.append(args)
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(TmuxDriver, "run", _run)
    await TmuxDriver().type_text("pane:0.0", "Я -Enter")

    assert calls == [("send-keys", "-t", "pane:0.0", "-l", "--", char) for char in "Я -Enter"]


@pytest.mark.asyncio
@pytest.mark.skipif(not TmuxDriver.tmux_available(), reason="tmux is required for the isolated pipe test")
async def test_pipe_pane_reconnects_without_disabling_existing_stream(tmp_path, monkeypatch):
    socket = str(tmp_path / "tmux.sock")
    driver = TmuxDriver()
    monkeypatch.setattr(driver, "command", lambda *args: ["tmux", "-S", socket, *args])
    target = "pipe-test:0.0"
    log = tmp_path / "folder ' with spaces" / "pane.log"
    try:
        await driver.run("new-session", "-d", "-s", "pipe-test", "sh")
        await driver.pipe_pane(target, str(log))
        first = await driver.run("display-message", "-p", "-t", target, "#{pane_pipe}")
        assert first.stdout.strip() == "1"
        await driver.pipe_pane(target, str(log))
        second = await driver.run("display-message", "-p", "-t", target, "#{pane_pipe}")
        assert second.stdout.strip() == "1"
        await driver.run("pipe-pane", "-t", target)
        await driver.pipe_pane(target, str(log))
        restored = await driver.run("display-message", "-p", "-t", target, "#{pane_pipe}")
        assert restored.stdout.strip() == "1"
        await driver.send_keys(target, "printf 'pipe-marker\\n'", "Enter")
        for _ in range(100):
            if log.exists() and "pipe-marker" in log.read_text():
                break
            await asyncio.sleep(0.01)
        assert "pipe-marker" in log.read_text()
    finally:
        await driver.run("kill-server", check=False)
