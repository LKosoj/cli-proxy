from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional


def _json_dumps(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def encode_content_length_message(obj: Any) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8")
    return header + payload


class JsonRpcStream:
    """
    Supports 2 common framings:
    - LSP-style: Content-Length: N + \\r\\n\\r\\n + JSON bytes
    - NDJSON: one JSON message per line
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def write(self, obj: Any, *, prefer_content_length: bool = True) -> None:
        data = encode_content_length_message(obj) if prefer_content_length else _json_dumps(obj)
        self._writer.write(data)
        await self._writer.drain()

    async def read(self) -> Optional[Dict[str, Any]]:
        line = await self._reader.readline()
        if not line:
            return None

        # Content-Length framing.
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except Exception:
                logging.exception("tool failed bad Content-Length header")
                return None

            # Consume headers until blank line.
            while True:
                hdr = await self._reader.readline()
                if not hdr:
                    return None
                if hdr in (b"\r\n", b"\n", b""):
                    break

            payload = await self._reader.readexactly(length)
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception:
                logging.exception("tool failed bad JSON payload")
                return None
            return obj if isinstance(obj, dict) else None

        # NDJSON framing.
        try:
            obj = json.loads(line.decode("utf-8").strip())
        except Exception:
            # Some servers may output logs to stdout; ignore non-JSON lines.
            return None
        return obj if isinstance(obj, dict) else None

