import asyncio

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
    def __init__(self, stdout_chunks):
        self.pid = 777778
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream([])

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


OPENCODE_CMD = [
    "opencode",
    "run",
    "--format",
    "json",
    "--session",
    "{resume}",
    "{prompt}",
]

SESSION_ID = "ses_01d85ccdfffepbcJ8X7ycTGj7M"


def _session(tmp_path) -> Session:
    tool = ToolConfig(name="opencode", mode="headless", cmd=list(OPENCODE_CMD))
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"opencode": tool},
        defaults=DefaultsConfig(workdir=str(tmp_path)),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return Session(
        id="s1",
        tool=tool,
        workdir=str(tmp_path),
        idle_timeout_sec=10,
        config=cfg,
    )


def test_opencode_headless_returns_final_assistant_message(monkeypatch, tmp_path):
    async def _run() -> None:
        session = _session(tmp_path)
        commands: list[list[str]] = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            commands.append(list(args))
            return _FakeProc(
                stdout_chunks=[
                    b'{"type":"step_start","timestamp":1,"sessionID":"' + SESSION_ID.encode()
                    + b'","part":{"id":"prt_1","messageID":"msg_1","type":"step-start"}}\n',
                    b'{"type":"reasoning","timestamp":2,"sessionID":"' + SESSION_ID.encode()
                    + b'","part":{"id":"prt_2","messageID":"msg_1","type":"reasoning",'
                    b'"text":"internal thinking"}}\n',
                    b'{"type":"tool_use","timestamp":3,"sessionID":"' + SESSION_ID.encode()
                    + b'","part":{"id":"prt_3","messageID":"msg_1","type":"tool","tool":"bash",'
                    b'"callID":"call_1","state":{"status":"completed","input":{"command":"ls"},'
                    b'"output":"bot.py\\nsession.py","title":"ls"}}}\n',
                    b'{"type":"text","timestamp":4,"sessionID":"' + SESSION_ID.encode()
                    + b'","part":{"id":"prt_4","messageID":"msg_1","type":"text",'
                    b'"text":"Two Python files."}}\n',
                    b'{"type":"step_finish","timestamp":5,"sessionID":"' + SESSION_ID.encode()
                    + b'","part":{"id":"prt_5","messageID":"msg_1","type":"step-finish",'
                    b'"reason":"stop"}}\n',
                ]
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("what is here?")

        assert out == "Two Python files."
        # Без токена пара `--session {resume}` выпадает целиком: промпт не должен
        # уехать в значение флага.
        assert commands[0] == ["opencode", "run", "--format", "json", "what is here?"]
        # sessionID приходит в каждой строке потока, отдельный resume_regex не нужен.
        assert session.resume_token == SESSION_ID

        await session._run_headless("and now?")

        assert commands[1] == [
            "opencode",
            "run",
            "--format",
            "json",
            "--session",
            SESSION_ID,
            "and now?",
        ]

    asyncio.run(_run())
