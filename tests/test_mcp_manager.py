"""Тесты MCPManager и ToolRegistry.close_mcp."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import MCPClientServerConfig
from modes.sdk.runtime.mcp.manager import MCPManager


# ---------------------------------------------------------------------------
# Вспомогательные фикстуры
# ---------------------------------------------------------------------------

def _make_config(servers=None):
    cfg = SimpleNamespace()
    cfg.mcp_clients = servers or []
    return cfg


def _make_client(name: str) -> MagicMock:
    client = MagicMock()
    client.stop = AsyncMock()
    client.name = name
    return client


# ---------------------------------------------------------------------------
# Тест 1: close_all останавливает всех клиентов
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_all_stops_all_clients():
    mgr = MCPManager(_make_config())
    c1 = _make_client("srv1")
    c2 = _make_client("srv2")
    mgr._clients = {"srv1": c1, "srv2": c2}

    await mgr.close_all()

    c1.stop.assert_called_once()
    c2.stop.assert_called_once()
    assert mgr._clients == {}


# ---------------------------------------------------------------------------
# Тест 2: после close_all ensure_started может запустить MCP заново
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_started_can_restart_after_close():
    server = MCPClientServerConfig(name="srv", cmd=["python", "-m", "fake_mcp"])
    mgr = MCPManager(_make_config([server]))
    await mgr.close_all()

    client = _make_client("srv")
    client.start = AsyncMock()

    with patch("modes.sdk.runtime.mcp.manager.StdioMCPClient", return_value=client) as client_cls:
        await mgr.ensure_started()

    client_cls.assert_called_once_with(
        name="srv",
        cmd=["python", "-m", "fake_mcp"],
        cwd=None,
        env=None,
        timeout_ms=30_000,
    )
    client.start.assert_awaited_once()
    assert mgr._clients == {"srv": client}


# ---------------------------------------------------------------------------
# Тест 3: close_all продолжает при ошибке stop одного клиента
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_all_continues_on_error():
    mgr = MCPManager(_make_config())
    c1 = _make_client("srv1")
    c1.stop = AsyncMock(side_effect=RuntimeError("ошибка stop"))
    c2 = _make_client("srv2")
    mgr._clients = {"srv1": c1, "srv2": c2}

    # Не должно выбрасывать исключение.
    await mgr.close_all()

    c1.stop.assert_called_once()
    c2.stop.assert_called_once()
    assert mgr._clients == {}


# ---------------------------------------------------------------------------
# Тест 4: registry.close_mcp вызывает close_all у MCPManager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_close_mcp_calls_manager():
    """ToolRegistry.close_mcp должен вызывать _mcp_manager.close_all()."""
    from modes.sdk.runtime.tooling.registry import ToolRegistry

    cfg = SimpleNamespace(mcp_clients=None)

    with patch("modes.sdk.runtime.tooling.registry.PluginLoader") as mock_loader_cls:
        mock_loader = MagicMock()
        mock_loader.load.return_value = []
        mock_loader_cls.return_value = mock_loader

        registry = ToolRegistry(cfg)

    close_all_mock = AsyncMock()
    registry._mcp_manager.close_all = close_all_mock

    await registry.close_mcp()

    close_all_mock.assert_called_once()
