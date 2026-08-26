"""Атрибуция native-хуков CLI: uid сессии бота уходит в окружение процесса.

Без него хук отдаёт только внутренний id самого CLI, и событие невозможно
связать с сессией бота. tmux-бэкенд закрыт отдельно (`-e` у new-session),
здесь проверяются оставшиеся два пути запуска: headless и interactive.
"""

import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session, session_runtime_uid


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    def __init__(self, stdout_chunks):
        self.pid = 424242
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream([])

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


def _session(tmp_path, *, name: str) -> Session:
    tool = ToolConfig(name=name, mode="headless", cmd=[name, "-p", "{prompt}"])
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={name: tool},
        defaults=DefaultsConfig(workdir=str(tmp_path)),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return Session(id="s1", tool=tool, workdir=str(tmp_path), idle_timeout_sec=10, config=cfg)


def test_headless_spawn_exports_session_uid(monkeypatch, tmp_path):
    async def _run() -> None:
        session = _session(tmp_path, name="kimi")
        captured: list[dict] = []

        async def _fake_exec(*_args, **kwargs):
            captured.append(dict(kwargs.get("env") or {}))
            return _FakeProc([b"done\n"])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await session._run_headless("hi")

        assert captured
        assert captured[0]["CLI_PROXY_SESSION_UID"] == session_runtime_uid(session)
        assert captured[0]["CLI_PROXY_SESSION_UID"] == "desktop:s1"

    asyncio.run(_run())


def test_headless_claude_su_command_carries_session_uid(monkeypatch, tmp_path):
    # Claude запускается через `su -`, а login shell сбрасывает окружение,
    # поэтому переменная должна стоять внутри самой команды.
    async def _run() -> None:
        session = _session(tmp_path, name="claude")
        commands: list[list[str]] = []

        async def _fake_exec(*args, **_kwargs):
            commands.append(list(args))
            return _FakeProc([b"done\n"])

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await session._run_headless("hi")

        assert commands and commands[0][0] == "su"
        full_cmd = commands[0][4]
        assert "CLI_PROXY_SESSION_UID=desktop:s1" in full_cmd
        # Переменная ставится тем же `env`, что снимает маркеры вложенности.
        assert full_cmd.index("CLI_PROXY_SESSION_UID=") > full_cmd.index("env -u CLAUDECODE")

    asyncio.run(_run())


def test_interactive_spawn_exports_session_uid(monkeypatch, tmp_path):
    import session as session_module

    session = _session(tmp_path, name="kimi")
    session.tool.interactive_cmd = ["kimi"]
    captured: dict = {}

    class _FakeChild:
        def __init__(self, *args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})
            captured["args"] = list(args)

        def isalive(self) -> bool:
            return True

    monkeypatch.setattr(session_module.pexpect, "spawn", _FakeChild)
    session._ensure_child()

    assert captured["env"]["CLI_PROXY_SESSION_UID"] == session_runtime_uid(session)
    assert captured["env"]["CLI_PROXY_SESSION_UID"] == "desktop:s1"


def test_interactive_claude_su_command_carries_session_uid(monkeypatch, tmp_path):
    # Interactive-путь для claude тоже идёт через `su -`. Это не мёртвая ветка:
    # на неё бот переключается, когда headless-запуск упал, то есть баг здесь
    # проявился бы именно при восстановлении после сбоя.
    import session as session_module

    session = _session(tmp_path, name="claude")
    session.tool.interactive_cmd = ["claude"]
    captured: dict = {}

    class _FakeChild:
        def __init__(self, *args, **kwargs):
            captured["args"] = list(args)

        def isalive(self) -> bool:
            return True

    monkeypatch.setattr(session_module.pexpect, "spawn", _FakeChild)
    session._ensure_child()

    assert captured["args"][0] == "su"
    full_cmd = captured["args"][1][-1]
    # Присваивание идёт после cd и непосредственно перед командой CLI.
    assert full_cmd.endswith("&& env CLI_PROXY_SESSION_UID=desktop:s1 claude")


def test_headless_drops_inherited_session_uid_when_unresolvable(monkeypatch, tmp_path):
    # Бот, запущенный внутри чужой сессии, уже имеет переменную в окружении.
    # Если для текущей сессии uid не резолвится, наследовать чужой нельзя.
    async def _run() -> None:
        session = _session(tmp_path, name="kimi")
        session.id = ""
        captured: list[dict] = []

        async def _fake_exec(*_args, **kwargs):
            captured.append(dict(kwargs.get("env") or {}))
            return _FakeProc([b"done\n"])

        monkeypatch.setenv("CLI_PROXY_SESSION_UID", "desktop:stale")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        await session._run_headless("hi")

        assert captured
        assert session_runtime_uid(session) == ""
        assert "CLI_PROXY_SESSION_UID" not in captured[0]

    asyncio.run(_run())
