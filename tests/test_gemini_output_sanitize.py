import asyncio

from app.services.session_tick_history_store import load_session_ticks
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 555555
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream(stderr_chunks)

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0

    async def communicate(self):
        out = bytearray()
        err = bytearray()
        while True:
            chunk = await self.stdout.read(4096)
            if not chunk:
                break
            out.extend(chunk)
        while True:
            chunk = await self.stderr.read(4096)
            if not chunk:
                break
            err.extend(chunk)
        self.returncode = 0
        return bytes(out), bytes(err)


class _DelayedStream:
    def __init__(self, chunks, delay: float):
        self._chunks = list(chunks)
        self._delay = delay

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(self._delay)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _DelayedProc(_FakeProc):
    def __init__(self, stdout_chunks, stderr_chunks, *, wait_delay: float, read_delay: float):
        super().__init__(stdout_chunks=stdout_chunks, stderr_chunks=stderr_chunks)
        self.stdout = _DelayedStream(stdout_chunks, read_delay)
        self.stderr = _DelayedStream(stderr_chunks, read_delay)
        self._wait_delay = wait_delay

    async def wait(self) -> int:
        await asyncio.sleep(self._wait_delay)
        self.returncode = 0
        return 0


def test_gemini_headless_uses_stdout_only_for_user(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*_args, **_kwargs):
            return _FakeProc(
                stdout_chunks=[b"Actual answer from stdout\n"],
                stderr_chunks=[
                    b"YOLO mode is enabled. All tool calls will be automatically approved.\n",
                    b"Loaded cached credentials.\n",
                    b"Hook registry initialized with 0 hook entries\n",
                ],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "Actual answer from stdout\n"

    asyncio.run(_run())


def test_gemini_resume_is_recovered_via_list_sessions_not_stderr(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(workdir=str(tmp_path)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(tmp_path),
            idle_timeout_sec=10,
            config=cfg,
        )

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            cmd = list(args)
            if cmd[:2] == ["gemini", "--list-sessions"]:
                return _FakeProc(
                    stdout_chunks=[b"  1. ? (Just now) [token-from-list-sessions]\n"],
                    stderr_chunks=[],
                )
            return _FakeProc(
                stdout_chunks=[b"answer\n"],
                stderr_chunks=[b"To continue run: gemini --resume token-from-stderr-1 -p \"...\"\n"],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert out == "answer\n"
        assert session.resume_token == "token-from-list-sessions"

    asyncio.run(_run())


def test_gemini_stdout_stream_restores_ticks_for_status_and_miniapp(monkeypatch, tmp_path):
    async def _run() -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        tool = ToolConfig(
            name="gemini",
            mode="headless",
            cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            headless_cmd=["gemini", "--approval-mode", "yolo", "--resume", "latest", "-p", "{prompt}"],
            separate_stderr=False,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"gemini": tool},
            defaults=DefaultsConfig(workdir=str(workdir)),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        session = Session(
            id="s1",
            tool=tool,
            workdir=str(workdir),
            idle_timeout_sec=10,
            config=cfg,
            chat_id=777,
        )
        captured_calls = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            captured_calls.append(list(args))
            return _DelayedProc(
                stdout_chunks=[
                    b'{"type":"init","session_id":"ba568ec1-3d9d-424d-86cc-55644c4124d7","model":"auto-gemini-3"}\n',
                    b'{"type":"tool_use","tool_name":"read_file","tool_id":"tool-1","parameters":{"file_path":"README.md"}}\n',
                    b'{"type":"tool_result","tool_id":"tool-1","status":"success","output":"Read lines 1-338 of 544 from README.md"}\n',
                    (
                        b'{"type":"tool_use","tool_name":"run_shell_command","tool_id":"tool-2",'
                        b'"parameters":{"command":"printf \\"tick-1\\\\ntick-2\\\\ntick-3\\""}}\n'
                    ),
                    (
                        b'{"type":"tool_result","tool_id":"tool-2","status":"success",'
                        b'"output":"tick-1\\ntick-2\\ntick-3"}\n'
                    ),
                    b'{"type":"message","role":"assistant","content":"Final answer from stdout","delta":false}\n',
                    b'{"type":"result","status":"success"}\n',
                ],
                stderr_chunks=[
                    b"YOLO mode is enabled. All tool calls will be automatically approved.\n",
                    b"Loaded cached credentials.\n",
                ],
                wait_delay=0.10,
                read_delay=0.10,
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")

        assert out == "Final answer from stdout"
        assert len(captured_calls) == 1

        ticks = [str(item.get("value")) for item in load_session_ticks(session)]
        assert any("read_file: README.md" in item for item in ticks)
        assert any("Read lines 1-338 of 544 from README.md" in item for item in ticks)
        assert any('run_shell_command: printf "tick-1\\ntick-2\\ntick-3"' in item for item in ticks)
        assert any("tick-1 tick-2 tick-3" in item for item in ticks)
        assert all("YOLO mode" not in item for item in ticks)
        assert all("Loaded cached credentials" not in item for item in ticks)
        assert session.resume_token == "ba568ec1-3d9d-424d-86cc-55644c4124d7"

    asyncio.run(_run())
