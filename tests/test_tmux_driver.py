import os
import stat

import pytest

from app.services.cli_backends.tmux_driver import (
    TmuxCommandResult,
    TmuxDriver,
    TmuxDriverError,
    wrap_user_command,
    write_prompt_temp,
)


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
