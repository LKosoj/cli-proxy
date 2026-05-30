"""Тесты жизненного цикла StdioMCPClient."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modes.sdk.runtime.mcp.stdio_client import StdioMCPClient


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры
# ---------------------------------------------------------------------------

class _FakeStream:
    """Имитирует JsonRpcStream."""

    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self._written = []

    async def read(self):
        if self._messages:
            return self._messages.pop(0)
        # EOF
        return None

    def at_eof(self) -> bool:
        return not self._messages

    async def write(self, msg):
        self._written.append(msg)


class _FakeProc:
    def __init__(self, *, wait_delay=0.0):
        self.stdin = MagicMock()
        self.stdin.close = MagicMock()
        self.stdout = MagicMock()
        self.stderr = AsyncMock()
        self.stderr.read = AsyncMock(return_value=b"")
        self._wait_delay = wait_delay
        self._returncode = 0

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        return self._returncode


# ---------------------------------------------------------------------------
# Тест 1: EOF освобождает pending futures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eof_cancels_pending_futures():
    """Когда reader_loop получает EOF, pending futures должны получить исключение."""
    client = StdioMCPClient(name="test", cmd=["true"], timeout_ms=1000)

    stream = _FakeStream(messages=[])  # сразу EOF

    client._stream = stream
    client._proc = _FakeProc()

    # Создаём pending future вручную.
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    client._pending[42] = fut

    # Запускаем reader_loop — он должен получить EOF и вызвать _cancel_pending.
    await client._reader_loop()

    assert fut.done(), "future должен быть завершён после EOF"
    assert isinstance(fut.exception(), Exception), "future должен содержать исключение"


# ---------------------------------------------------------------------------
# Тест 2: stop() отменяет pending futures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_cancels_pending():
    """stop() должен отменить все pending futures через _cancel_pending."""
    client = StdioMCPClient(name="test", cmd=["true"], timeout_ms=1000)

    proc = _FakeProc()
    client._proc = proc
    client._stream = _FakeStream()

    # Добавляем pending future.
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    client._pending[1] = fut

    await client.stop()

    assert fut.done(), "future должен быть завершён после stop()"
    assert isinstance(fut.exception(), RuntimeError)
    assert "остановлен" in str(fut.exception())


# ---------------------------------------------------------------------------
# Тест 3: stop() отправляет SIGKILL при таймауте
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_kills_on_timeout():
    """Если proc.wait() зависает, stop() должен вызвать proc.kill()."""
    client = StdioMCPClient(name="slow", cmd=["sleep", "100"], timeout_ms=1000)

    # Процесс зависает на wait() дольше 5 секунд.
    proc = _FakeProc(wait_delay=100.0)
    kill_called = []
    original_kill = proc.kill
    proc.kill = lambda: kill_called.append(True) or original_kill()
    client._proc = proc
    client._stream = _FakeStream()

    # Ускоряем таймаут через патч wait_for.
    original_wait_for = asyncio.wait_for
    call_count = [0]

    async def _fast_wait_for(coro, timeout):
        call_count[0] += 1
        # Первый вызов — слив stderr, второй — proc.wait (5s → TimeoutError), третий — post-kill wait.
        if call_count[0] == 2:
            # Имитируем таймаут для proc.wait().
            coro.close()
            raise asyncio.TimeoutError
        if call_count[0] == 3:
            # После kill — возвращаем сразу.
            coro.close()
            return 0
        return await original_wait_for(coro, timeout)

    with patch("modes.sdk.runtime.mcp.stdio_client.asyncio.wait_for", side_effect=_fast_wait_for):
        await client.stop()

    assert kill_called, "proc.kill() должен быть вызван при таймауте"


# ---------------------------------------------------------------------------
# Тест 4: атомарность req_id — параллельные _request дают уникальные id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_id_uniqueness():
    """Параллельные вызовы _request должны получать уникальные req_id."""
    client = StdioMCPClient(name="test", cmd=["true"], timeout_ms=2000)

    sent_ids = []

    class _ResolvingStream:
        """Стрим, который немедленно резолвит future после записи."""

        async def read(self):
            return None

        async def write(self, msg):
            if "id" in msg:
                sent_ids.append(msg["id"])
                # Немедленно резолвим соответствующий future.
                req_id = msg["id"]
                fut = client._pending.get(req_id)
                if fut and not fut.done():
                    fut.set_result({})

    client._stream = _ResolvingStream()
    client._proc = _FakeProc()

    await asyncio.gather(*[client._request(f"method_{i}", {}) for i in range(5)])

    assert len(sent_ids) == 5, f"Ожидалось 5 id, получено {len(sent_ids)}"
    assert len(set(sent_ids)) == 5, f"Все id должны быть уникальными: {sent_ids}"


# ---------------------------------------------------------------------------
# Тест 5: _reader_loop при IncompleteReadError устанавливает dead_exc
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_json_line_does_not_kill_reader():
    """read()==None при at_eof()==False (не-JSON строка / лог сервера в stdout) не
    должен обрывать соединение: reader_loop пропускает строку и продолжает читать."""
    client = StdioMCPClient(name="test", cmd=["true"], timeout_ms=1000)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    client._pending[5] = fut

    class _NoisyStream:
        def __init__(self):
            # None = не-JSON строка (поток жив), затем валидный ответ, затем EOF.
            self._script = [None, {"id": 5, "result": {"ok": True}}]

        async def read(self):
            if self._script:
                return self._script.pop(0)
            return None  # EOF

        def at_eof(self) -> bool:
            return not self._script

        async def write(self, msg):
            pass

    client._stream = _NoisyStream()
    client._proc = _FakeProc()

    await client._reader_loop()

    assert fut.done(), "ответ должен дойти, несмотря на предшествующую не-JSON строку"
    assert fut.result() == {"ok": True}


@pytest.mark.asyncio
async def test_reader_loop_incomplete_read_cancels_pending():
    """IncompleteReadError в reader_loop должен отменить pending futures."""
    client = StdioMCPClient(name="test", cmd=["true"], timeout_ms=1000)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    client._pending[7] = fut

    class _ErrorStream:
        async def read(self):
            raise asyncio.IncompleteReadError(b"", 10)

        async def write(self, msg):
            pass

    client._stream = _ErrorStream()
    client._proc = _FakeProc()

    await client._reader_loop()

    assert fut.done()
    assert isinstance(fut.exception(), asyncio.IncompleteReadError)
