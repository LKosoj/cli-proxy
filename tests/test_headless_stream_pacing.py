import asyncio
from pathlib import Path

import pytest

import session as session_module
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import Session


class _BurstStream:
    """Отдаёт готовые данные без единой точки ожидания — как StreamReader с непустым буфером."""

    def __init__(self, chunks: list[bytes], on_eof=None) -> None:
        self._chunks = list(chunks)
        self._on_eof = on_eof

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._on_eof is not None:
            self._on_eof()
            self._on_eof = None
        return b""


class _BurstProc:
    def __init__(self, chunks: list[bytes]) -> None:
        self.pid = 474747
        self.returncode = None
        self.stdin = None
        self.exit_ready = asyncio.Event()
        self.stdout = _BurstStream(chunks, on_eof=self._mark_exited)
        self.stderr = _BurstStream([])

    def _mark_exited(self) -> None:
        self.returncode = 0
        self.exit_ready.set()

    async def wait(self) -> int:
        await self.exit_ready.wait()
        return int(self.returncode)

    def terminate(self) -> None:
        self._mark_exited()

    def kill(self) -> None:
        self._mark_exited()


def _build_session(tmp_path, *, archive: bool = False) -> Session:
    tool = ToolConfig(
        name="claude",
        mode="headless",
        cmd=["claude", "-p", "{prompt}"],
        headless_cmd=["claude", "-p", "{prompt}"],
    )
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
        tools={"claude": tool},
        defaults=DefaultsConfig(workdir=str(tmp_path), cli_json_stream_archive_enabled=archive),
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


@pytest.mark.asyncio
async def test_headless_drain_yields_to_event_loop_between_chunks(tmp_path, monkeypatch) -> None:
    chunk_count = 60
    chunks = [b'{"type":"assistant","text":"chunk %d"}\n' % i for i in range(chunk_count)]
    proc = _BurstProc(chunks)

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(session_module.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(session_module, "_HEADLESS_WAIT_POLL_SEC", 0.01)

    session = _build_session(tmp_path)
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(_ticker())
    try:
        await session._run_headless("hello")
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)

    # Без уступки управления внутри цикла чтения сторонняя задача не получила бы
    # процессорное время, пока весь поток не будет вычитан.
    assert ticks >= chunk_count // 2
    assert session.headless_forced_stop is None


@pytest.mark.asyncio
async def test_headless_archive_path_is_set_when_output_has_no_trailing_newline(
    tmp_path, monkeypatch
) -> None:
    # Единственная строка без завершающего "\n" разбирается только в EOF-ветке,
    # поэтому именно там архив обязан оказаться на диске до чтения путей.
    proc = _BurstProc([b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'])

    async def _fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(session_module.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(session_module, "_HEADLESS_WAIT_POLL_SEC", 0.01)

    session = _build_session(tmp_path, archive=True)
    await session._run_headless("hello")

    assert session.last_cli_raw_stream_path
    raw_file = Path(session.last_cli_raw_stream_path)
    assert raw_file.is_file()
    assert '"type":"assistant"' in raw_file.read_text(encoding="utf-8")
