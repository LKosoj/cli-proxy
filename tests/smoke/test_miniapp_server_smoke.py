from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from bot import BotApp

from tests.smoke._smoke_support import build_config


def test_miniapp_server_smoke_mounts_and_serves_index(tmp_path) -> None:
    cfg = build_config(tmp_path, intent="miniapp_entrypoint", miniapp_enabled=True)
    app = BotApp(cfg)

    async def _run() -> None:
        await app.miniapp_server.start()
        assert app.miniapp_server._started is True

        server = TestServer(app.shared_http_ingress._root)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.get("/cli-proxy/")
            body = await response.text()
            assert response.status == 200
            assert "statusRunDoctor" in body
        finally:
            await client.close()
            await server.close()

    try:
        asyncio.run(_run())
    finally:
        app.shutdown_html_process_pool()
