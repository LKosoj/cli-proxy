from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.mcp.jsonrpc import JsonRpcStream


@dataclass
class MCPToolInfo:
    name: str
    description: str
    input_schema: Dict[str, Any]


def _now_ms() -> int:
    return int(time.time() * 1000)


class StdioMCPClient:
    def __init__(
        self,
        *,
        name: str,
        cmd: List[str],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_ms: int = 30_000,
        protocol_version: str = "2024-11-05",
    ) -> None:
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env or {}
        self.timeout_ms = timeout_ms
        self.protocol_version = protocol_version

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stream: Optional[JsonRpcStream] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._proc:
            return
        if not self.cmd:
            raise ValueError(f"MCP server '{self.name}' cmd is empty")

        env = os.environ.copy()
        env.update({k: str(v) for k, v in (self.env or {}).items()})

        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
        )
        assert self._proc.stdin and self._proc.stdout
        self._stream = JsonRpcStream(self._proc.stdout, self._proc.stdin)
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"mcp:{self.name}:reader")

        await self._initialize()

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

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
        resp = await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        return resp or {}

    async def _initialize(self) -> None:
        # MCP handshake: initialize request + initialized notification.
        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "clientInfo": {"name": "cli-proxy", "version": "0.1"},
                    "capabilities": {"tools": {}},
                },
            )
            await self._notify("notifications/initialized", {})
        except Exception as e:
            # If the server doesn't implement initialize strictly, it may still work for tools/list.
            logging.exception(f"tool failed MCP initialize failed for '{self.name}': {str(e)}")

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        stream = self._stream
        if not stream:
            raise RuntimeError("MCP stream not started")
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        await stream.write(msg)

    async def _request(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stream = self._stream
        if not stream:
            raise RuntimeError("MCP stream not started")

        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        await stream.write(msg)

        try:
            return await asyncio.wait_for(fut, timeout=self.timeout_ms / 1000)
        finally:
            self._pending.pop(req_id, None)

    async def _reader_loop(self) -> None:
        assert self._stream is not None
        while True:
            try:
                msg = await self._stream.read()
            except asyncio.IncompleteReadError:
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logging.exception(f"tool failed MCP reader loop error '{self.name}': {str(e)}")
                return
            if not msg:
                continue

            # JSON-RPC response
            if "id" in msg:
                try:
                    req_id = int(msg.get("id"))
                except Exception:
                    continue
                fut = self._pending.get(req_id)
                if not fut or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg.get("error"))))
                else:
                    result = msg.get("result")
                    fut.set_result(result if isinstance(result, dict) else {})
