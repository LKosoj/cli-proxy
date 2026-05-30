"""Тесты HttpMCPClient."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modes.sdk.runtime.mcp.http_client import HttpMCPClient, HttpMCPClientConfig


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_client(**kwargs) -> HttpMCPClient:
    cfg = HttpMCPClientConfig(name="test", url="http://localhost:8888/mcp", **kwargs)
    return HttpMCPClient(cfg)


def _make_response(data, *, status=200):
    """Создаёт мок httpx.Response."""
    import json as _json_inner
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json = lambda: _json_inner.loads(_json_inner.dumps(data))
    resp.content = _json_inner.dumps(data).encode()
    return resp


# ---------------------------------------------------------------------------
# Тест 1: атомарность _next_id при параллельных _request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_request_id_uniqueness():
    """Параллельные вызовы _request должны получать уникальные id."""
    client = _make_client()
    client._client = MagicMock()

    sent_ids = []

    async def _fake_post(url, *, json, headers):
        if "id" in json:
            sent_ids.append(json["id"])
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.content = b'{"result": {}}'
        resp.json = lambda: {"result": {}}
        return resp

    client._client.post = _fake_post

    await asyncio.gather(*[client._request(f"method_{i}", {}) for i in range(5)])

    assert len(sent_ids) == 5
    assert len(set(sent_ids)) == 5, f"id должны быть уникальными: {sent_ids}"


# ---------------------------------------------------------------------------
# Тест 2: _initialize при сетевой ошибке пробрасывает исключение
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_propagates_network_error():
    """_initialize должен пробрасывать IO/сетевые ошибки (не RuntimeError)."""
    import httpx

    client = _make_client()
    client._client = MagicMock()

    async def _raise_network(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    client._client.post = _raise_network

    with pytest.raises(httpx.ConnectError):
        await client._initialize()


# ---------------------------------------------------------------------------
# Тест 3: _initialize при JSON-RPC ошибке НЕ пробрасывает (RuntimeError)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_swallows_jsonrpc_error():
    """_initialize должен поглощать RuntimeError (JSON-RPC ошибку сервера)."""
    client = _make_client()
    client._client = MagicMock()

    async def _request_stub(method, params, *, notification=False):
        if not notification:
            raise RuntimeError("Method not found")

    # Патчим сам _request напрямую.
    client._request = _request_stub

    # Не должно пробрасывать.
    await client._initialize()


# ---------------------------------------------------------------------------
# Тест 4: stop() закрывает http client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_closes_client():
    client = _make_client()
    mock_client = AsyncMock()
    client._client = mock_client

    await client.stop()

    mock_client.aclose.assert_called_once()
    assert client._client is None
