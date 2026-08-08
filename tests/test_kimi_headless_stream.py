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
        self.pid = 777777
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream([])

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


KIMI_CMD = [
    "kimi",
    "--print",
    "--output-format",
    "stream-json",
    "--continue",
    "--prompt",
    "{prompt}",
    "--resume",
    "{resume}",
]


def _session(tmp_path) -> Session:
    tool = ToolConfig(name="kimi", mode="headless", cmd=list(KIMI_CMD))
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"kimi": tool},
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


def test_kimi_headless_returns_final_assistant_message(monkeypatch, tmp_path):
    async def _run() -> None:
        session = _session(tmp_path)
        commands: list[list[str]] = []

        async def _fake_create_subprocess_exec(*args, **_kwargs):
            commands.append(list(args))
            return _FakeProc(
                stdout_chunks=[
                    b'{"role":"assistant","content":"Checking the tree.","tool_calls":'
                    b'[{"type":"function","id":"tc_1","function":{"name":"Shell",'
                    b'"arguments":"{\\"command\\":\\"ls\\"}"}}]}\n',
                    b'{"role":"tool","tool_call_id":"tc_1","content":"bot.py\\nsession.py"}\n',
                    b'{"role":"assistant","content":"Two Python files."}\n',
                ]
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("what is here?")

        assert out == "Two Python files."
        # Промпт уходит аргументом --prompt, а fresh-запуск продолжает последнюю сессию каталога.
        assert commands[0][:6] == ["kimi", "--print", "--output-format", "stream-json", "--continue", "--prompt"]
        assert "what is here?" in commands[0]
        assert "--resume" not in commands[0]

    asyncio.run(_run())
