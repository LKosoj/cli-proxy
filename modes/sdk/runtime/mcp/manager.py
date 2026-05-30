from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Tuple, Union

from config import AppConfig, MCPClientServerConfig
from modes.sdk.runtime.mcp.http_client import HttpMCPClient, HttpMCPClientConfig
from modes.sdk.runtime.mcp.stdio_client import MCPToolInfo, StdioMCPClient

logger = logging.getLogger(__name__)


def _sanitize_tool_name(name: str) -> str:
    # OpenAI function tool names are restrictive; keep to [a-zA-Z0-9_-].
    out = []
    for ch in name:
        if ch.isalnum() or ch in ("_", "-"):
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_") or "tool"


class MCPManager:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._clients: Dict[str, Union[StdioMCPClient, HttpMCPClient]] = {}
        self._tools_cache: Dict[str, List[MCPToolInfo]] = {}
        self._init_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False

    def _shared_root(self) -> str:
        sandbox_root = os.getenv("AGENT_SANDBOX_ROOT")
        if sandbox_root:
            return os.path.join(sandbox_root, "_shared")
        return os.path.join(os.getcwd(), "_sandbox", "_shared")

    def _cache_path(self) -> str:
        return os.path.join(self._shared_root(), "mcp_tools_cache.json")

    def configured_servers(self) -> List[MCPClientServerConfig]:
        return list(self._config.mcp_clients or [])

    def load_cached_tools(self) -> List[Tuple[str, MCPToolInfo]]:
        path = self._cache_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.exception("mcp: не удалось прочитать кэш инструментов %s", path)
            return []
        if not isinstance(data, dict):
            return []
        out: List[Tuple[str, MCPToolInfo]] = []
        for server_name, tools in data.items():
            if not isinstance(server_name, str) or not isinstance(tools, list):
                continue
            for t in tools:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name") or "").strip()
                if not name:
                    continue
                desc = str(t.get("description") or "").strip()
                schema = t.get("input_schema") if isinstance(t.get("input_schema"), dict) else {}
                out.append((server_name, MCPToolInfo(name=name, description=desc, input_schema=schema)))
        return out

    def save_cached_tools(self, tools: List[Tuple[str, MCPToolInfo]]) -> None:
        root = self._shared_root()
        os.makedirs(root, exist_ok=True)
        payload: Dict[str, List[Dict[str, Any]]] = {}
        for server_name, t in tools:
            payload.setdefault(server_name, []).append(
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            )
        path = self._cache_path()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    async def close_all(self) -> None:
        """Остановить все MCP-клиенты и сбросить состояние."""
        async with self._close_lock:
            # Захватываем _init_lock, чтобы не разойтись с ensure_started(): иначе клиент,
            # создаваемый в момент close_all(), мог бы попасть в уже очищенный _clients и утечь.
            async with self._init_lock:
                self._closing = True
                clients = self._clients.copy()
                self._clients.clear()
                self._tools_cache.clear()
            try:
                # stop() может быть медленным (терминирование процессов) — выполняем вне лока.
                for name, client in clients.items():
                    try:
                        await client.stop()
                    except Exception:
                        logger.exception("mcp '%s': ошибка при stop в close_all", name)
            finally:
                async with self._init_lock:
                    self._closing = False

    async def ensure_started(self) -> None:
        async with self._init_lock:
            if self._closing:
                return
            for server in self.configured_servers():
                if not server.enabled:
                    continue
                if server.name in self._clients:
                    continue
                try:
                    transport = (server.transport or "stdio").lower().strip()
                    if transport in ("stdio",):
                        client = StdioMCPClient(
                            name=server.name,
                            cmd=server.cmd,
                            cwd=server.cwd,
                            env=server.env,
                            timeout_ms=server.timeout_ms,
                        )
                        await client.start()
                        self._clients[server.name] = client
                    elif transport in ("http", "http_stream", "httpstream", "httpstreaming", "http_streaming"):
                        if not server.url:
                            raise ValueError(f"MCP http server '{server.name}' missing url")
                        client = HttpMCPClient(
                            HttpMCPClientConfig(
                                name=server.name,
                                url=server.url,
                                timeout_ms=server.timeout_ms,
                                headers=server.headers,
                            )
                        )
                        await client.start()
                        self._clients[server.name] = client
                    else:
                        logger.error("mcp '%s': unsupported transport=%r, skipping", server.name, server.transport)
                        continue
                except Exception:
                    logger.exception("mcp '%s': ошибка запуска MCP-клиента", server.name)
                    continue

    async def list_all_tools(self) -> List[Tuple[str, MCPToolInfo]]:
        await self.ensure_started()
        out: List[Tuple[str, MCPToolInfo]] = []
        # Снапшот: close_all() может очистить _clients во время await client.list_tools(),
        # что вызвало бы "dictionary changed size during iteration".
        for server_name, client in list(self._clients.items()):
            try:
                tools = await client.list_tools()
                self._tools_cache[server_name] = tools
                for t in tools:
                    out.append((server_name, t))
            except Exception:
                logger.exception("mcp '%s': ошибка tools/list", server_name)
        return out

    async def call(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        await self.ensure_started()
        client = self._clients.get(server_name)
        if not client:
            raise RuntimeError(f"MCP server not started: {server_name}")
        return await client.call_tool(tool_name, arguments)

    def build_registry_name(self, server_name: str, tool_name: str) -> str:
        s1 = _sanitize_tool_name(server_name)
        s2 = _sanitize_tool_name(tool_name)
        # OpenAI limit is 64 chars; keep a reasonable bound.
        base = f"mcp_{s1}_{s2}"
        return base[:64]
