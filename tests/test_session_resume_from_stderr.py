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
    def __init__(self, stdout_chunks, stderr_chunks):
        self.pid = 424242
        self.returncode = None
        self.stdin = None
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream(stderr_chunks)

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = 0
        return 0


def test_headless_resume_token_detected_from_stderr(monkeypatch, tmp_path):
    async def _run() -> None:
        tool = ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
            headless_cmd=["codex", "exec", "{prompt}"],
            resume_cmd=["codex", "exec", "resume", "{resume}", "{prompt}"],
            resume_regex=r"\"thread_id\"\s*:\s*\"([^\"]+)\"",
            separate_stderr=True,
        )
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={"codex": tool},
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
                stdout_chunks=[b"result\n"],
                stderr_chunks=[b'{"thread_id":"019c353d-5d3d-7441-9178-da0630800212"}\n'],
            )

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        out = await session._run_headless("hello")
        assert "result" in out
        assert session.resume_token == "019c353d-5d3d-7441-9178-da0630800212"

    asyncio.run(_run())
