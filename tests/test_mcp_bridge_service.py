import asyncio
import json
from types import SimpleNamespace

from app.services.mcp_bridge_service import MCPBridge


class _Reader:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _Writer:
    def __init__(self) -> None:
        self.data: list[bytes] = []
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data.append(bytes(chunk))

    async def drain(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


def _decode_lines(writer: _Writer) -> list[dict]:
    out: list[dict] = []
    for chunk in writer.data:
        for line in chunk.decode().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_mcp_bridge_does_not_start_without_token_when_enabled(monkeypatch):
    async def _run() -> None:
        calls = {"start_server": 0}

        async def _start_server(*_args, **_kwargs):
            calls["start_server"] += 1
            return object()

        monkeypatch.setattr(asyncio, "start_server", _start_server)
        bridge = MCPBridge(
            config=SimpleNamespace(mcp=SimpleNamespace(enabled=True, host="127.0.0.1", port=8765, token=None)),
            bot_app=SimpleNamespace(),
        )
        await bridge.start()
        assert calls["start_server"] == 0

    asyncio.run(_run())


def test_mcp_bridge_requires_chat_id():
    async def _run() -> None:
        calls = {"run_prompt_raw": 0}

        async def _run_prompt_raw(*_args, **_kwargs):
            calls["run_prompt_raw"] += 1
            return "ok"

        bridge = MCPBridge(
            config=SimpleNamespace(mcp=SimpleNamespace(enabled=True, host="127.0.0.1", port=8765, token="tkn")),
            bot_app=SimpleNamespace(run_prompt_raw=_run_prompt_raw),
        )

        req = json.dumps({"token": "tkn", "prompt": "hello", "session_id": "s1"}).encode() + b"\n"
        reader = _Reader([req])
        writer = _Writer()
        await bridge._handle_client(reader, writer)

        payloads = _decode_lines(writer)
        assert payloads[-1]["ok"] is False
        assert payloads[-1]["error"] == "chat_id_required"
        assert calls["run_prompt_raw"] == 0

    asyncio.run(_run())


def test_mcp_bridge_passes_chat_id_into_run_prompt_raw():
    async def _run() -> None:
        calls = {"chat_id": None, "session_id": None, "source": None, "task_bearing": None}

        async def _run_prompt_raw(prompt: str, session_id=None, chat_id=None, source=None, task_bearing=None, **_kwargs):
            assert prompt == "hello"
            calls["session_id"] = session_id
            calls["chat_id"] = chat_id
            calls["source"] = source
            calls["task_bearing"] = task_bearing
            return "ok"

        bridge = MCPBridge(
            config=SimpleNamespace(mcp=SimpleNamespace(enabled=True, host="127.0.0.1", port=8765, token="tkn")),
            bot_app=SimpleNamespace(run_prompt_raw=_run_prompt_raw),
        )

        req = json.dumps({"token": "tkn", "prompt": "hello", "session_id": "s1", "chat_id": 11}).encode() + b"\n"
        reader = _Reader([req])
        writer = _Writer()
        await bridge._handle_client(reader, writer)

        payloads = _decode_lines(writer)
        assert payloads[-1]["ok"] is True
        assert payloads[-1]["output"] == "ok"
        assert calls["session_id"] == "s1"
        assert calls["chat_id"] == 11
        assert calls["source"] == "mcp_bridge"
        assert calls["task_bearing"] is True

    asyncio.run(_run())
