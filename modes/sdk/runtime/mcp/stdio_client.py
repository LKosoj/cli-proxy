from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from modes.sdk.runtime.mcp.jsonrpc import JsonRpcStream

logger = logging.getLogger(__name__)


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

        # Если handshake падает, обязаны прибрать процесс и reader_task, иначе они утекут:
        # вызывающий (MCPManager.ensure_started) не добавит клиента в _clients и close_all() его не остановит.
        try:
            await self._initialize()
        except Exception:
            await self.stop()
            raise

    def _cancel_pending(self, exc: Exception) -> None:
        """Завершить все ожидающие futures с исключением."""
        pending = self._pending.copy()
        self._pending.clear()
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        self._stream = None

        # Отменяем reader_task с ожиданием завершения.
        reader_task = self._reader_task
        self._reader_task = None
        if reader_task is not None:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                logger.debug("mcp '%s': reader task завершён при stop", self.name)

        # Отменяем все pending futures.
        self._cancel_pending(RuntimeError(f"MCP сервер '{self.name}' остановлен"))

        if proc is None:
            return

        # Закрываем stdin.
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            logger.debug("mcp '%s': ошибка закрытия stdin при stop", self.name)

        # Посылаем SIGTERM.
        try:
            proc.terminate()
        except Exception:
            logger.debug("mcp '%s': ошибка terminate при stop", self.name)

        # Сливаем stderr (best-effort).
        try:
            await asyncio.wait_for(proc.stderr.read(), 2.0)
        except Exception:
            logger.debug("mcp '%s': ошибка слива stderr при stop", self.name)

        # Ждём завершения процесса.
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except asyncio.TimeoutError:
            logger.warning("mcp '%s': процесс не завершился за 5с, отправляем SIGKILL", self.name)
            try:
                proc.kill()
            except Exception:
                logger.debug("mcp '%s': ошибка kill при stop", self.name)
            try:
                await asyncio.wait_for(proc.wait(), 3.0)
            except Exception:
                logger.debug("mcp '%s': процесс не завершился после SIGKILL", self.name)

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
        except RuntimeError as e:
            # JSON-RPC ошибка сервера — сервер может всё равно работать для tools/list.
            logger.debug("mcp '%s': JSON-RPC ошибка при initialize: %s", self.name, e)
        except Exception:
            logger.exception("mcp '%s': IO/сетевая ошибка при initialize", self.name)
            raise

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
            # stop() обнуляет _stream перед _cancel_pending; если он успел отработать,
            # пока мы ждали _lock, не регистрируем «осиротевший» fut, который никто
            # уже не отменит — иначе wait_for ниже зависнет до таймаута.
            if self._stream is None:
                raise RuntimeError("MCP stream not started")
            req_id = self._next_id
            self._next_id += 1
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = fut

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        await stream.write(msg)

        try:
            return await asyncio.wait_for(fut, timeout=self.timeout_ms / 1000)
        finally:
            self._pending.pop(req_id, None)

    async def _reader_loop(self) -> None:
        assert self._stream is not None
        dead_exc: Optional[Exception] = None
        while True:
            try:
                msg = await self._stream.read()
            except asyncio.CancelledError:
                return
            except asyncio.IncompleteReadError as e:
                dead_exc = e
                break
            except Exception as e:
                logger.exception("mcp '%s': ошибка в reader loop", self.name)
                dead_exc = e
                break
            if not msg:
                stream = self._stream
                if stream is None or stream.at_eof():
                    # Реальный EOF (или поток уже закрыт через stop()).
                    dead_exc = RuntimeError(f"MCP сервер '{self.name}': EOF")
                    break
                # Не-JSON строка / лог сервера в stdout / битый кадр — пропускаем,
                # соединение живо, продолжаем читать.
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

        # После выхода из цикла — уведомляем ожидающих об обрыве соединения.
        if dead_exc is not None:
            self._cancel_pending(dead_exc)
        self._stream = None
