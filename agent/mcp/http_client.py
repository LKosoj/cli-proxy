from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from agent.mcp.stdio_client import MCPToolInfo


@dataclass
class HttpMCPClientConfig:
    name: str
    url: str
    timeout_ms: int = 30_000
    headers: Optional[Dict[str, str]] = None
    protocol_version: str = "2024-11-05"


class HttpMCPClient:
    """
    MCP client over HTTP POST with JSON-RPC 2.0 request/response.

    The gateway at 127.0.0.1:8888 used by MCP tool servers expects:
    - POST JSON-RPC messages to the MCP endpoint
    - Accept: application/json
    """

    def __init__(self, cfg: HttpMCPClientConfig) -> None:
        self.cfg = cfg
        self._client: Optional[httpx.AsyncClient] = None
        self._next_id = 1

    async def start(self) -> None:
        if self._client:
            return
        self._client = httpx.AsyncClient(timeout=self.cfg.timeout_ms / 1000)
        await self._initialize()

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def list_tools(self) -> List[MCPToolInfo]:
        resp = await self._request("tools/list", {})
        tools = (resp or {}).get("tools") or []
        out: List[MCPToolInfo] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            desc = str(t.get("description") or "").strip()
            schema = t.get("inputSchema") or {}
            if not isinstance(schema, dict):
                schema = {}
            out.append(MCPToolInfo(name=name, description=desc, input_schema=schema))
        return out

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}}) or {}

    async def _initialize(self) -> None:
        # Not all servers enforce initialize, but it is harmless if supported.
        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": self.cfg.protocol_version,
                    "clientInfo": {"name": "cli-proxy", "version": "0.1"},
                    "capabilities": {"tools": {}},
                },
            )
            await self._notify("notifications/initialized", {})
        except Exception as e:
            logging.exception(f"tool failed MCP http initialize failed for '{self.cfg.name}': {str(e)}")

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._request(method, params, notification=True)

    async def _request(
        self, method: str, params: Dict[str, Any], *, notification: bool = False
    ) -> Optional[Dict[str, Any]]:
        if not self._client:
            raise RuntimeError("HTTP MCP client not started")
        req_id = None if notification else self._next_id
        if req_id is not None:
            self._next_id += 1

        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if req_id is not None:
            msg["id"] = req_id

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.cfg.headers:
            headers.update({str(k): str(v) for k, v in self.cfg.headers.items()})

        r = await self._client.post(self.cfg.url, json=msg, headers=headers)
        r.raise_for_status()

        # Some gateways return empty responses for notifications.
        if notification:
            return None

        if not r.content:
            return {}

        data = r.json()
        if not isinstance(data, dict):
            return {}
        if "error" in data:
            raise RuntimeError(str(data.get("error")))
        result = data.get("result")
        return result if isinstance(result, dict) else {}
