import asyncio
import json
import logging
from typing import Any, Dict, Optional

from modes.sdk.runtime.json_normalizer import loads_safe


class MCPBridge:
    def __init__(self, config, bot_app) -> None:
        self.config = config
        self.bot_app = bot_app
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        if not self.config.mcp.enabled:
            return
        if not str(getattr(self.config.mcp, "token", "") or "").strip():
            logging.getLogger(__name__).error("MCP bridge is enabled but mcp.token is empty; bridge will not start")
            return
        self._server = await asyncio.start_server(
            self._handle_client, host=self.config.mcp.host, port=self.config.mcp.port
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                payload = loads_safe(line.decode().strip(), strict_first=True)
            except Exception:
                await self._write(writer, {"ok": False, "error": "bad_json"})
                continue
            token = payload.get("token")
            if self.config.mcp.token and token != self.config.mcp.token:
                await self._write(writer, {"ok": False, "error": "unauthorized"})
                continue
            prompt = payload.get("prompt")
            session_id = payload.get("session_id")
            chat_id = payload.get("chat_id")
            if not prompt:
                await self._write(writer, {"ok": False, "error": "empty_prompt"})
                continue
            if chat_id is None:
                await self._write(writer, {"ok": False, "error": "chat_id_required"})
                continue
            try:
                chat_id = int(chat_id)
            except Exception:
                await self._write(writer, {"ok": False, "error": "invalid_chat_id"})
                continue
            try:
                output = await self.bot_app.run_prompt_raw(
                    prompt,
                    session_id=session_id,
                    chat_id=chat_id,
                    source="mcp_bridge",
                    task_bearing=True,
                )
                await self._write(writer, {"ok": True, "output": output})
            except Exception as e:
                logging.exception(f"tool failed {str(e)}")
                await self._write(writer, {"ok": False, "error": str(e)})
        writer.close()
        await writer.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        writer.write(data)
        await writer.drain()
