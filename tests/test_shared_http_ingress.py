from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp import ClientSession

from app.services.shared_http_ingress import SharedHttpIngress
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig, load_config
from miniapp.server import MiniAppServer


def _build_config(tmp_path, *, intent: str, base_path: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(enabled=True, base_path=base_path),
    )


def _fake_miniapp_bot(cfg: AppConfig) -> SimpleNamespace:
    return SimpleNamespace(
        config=cfg,
        container=SimpleNamespace(config_service=SimpleNamespace()),
    )


def test_miniapp_server_uses_shared_http_ingress_and_serves_ui(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="shared_ingress", base_path="/cli-proxy")
        app = _fake_miniapp_bot(cfg)
        app.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
        server = MiniAppServer(app)

        await server.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            redirect = await client.get(
                f"http://127.0.0.1:{port}/cli-proxy",
                allow_redirects=False,
            )
            assert redirect.status == 302
            assert redirect.headers["Location"] == "/cli-proxy/"

            index = await client.get(f"http://127.0.0.1:{port}/cli-proxy/")
            assert index.status == 200
            assert index.headers["Content-Type"].startswith("text/html")
        await server.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_shared_http_ingress_does_not_leak_routes_between_sequential_starts(tmp_path) -> None:
    async def _run() -> None:
        cfg_a = _build_config(tmp_path, intent="ingress_a", base_path="/mini-a")
        app_a = _fake_miniapp_bot(cfg_a)
        app_a.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
        server_a = MiniAppServer(app_a)
        await server_a.start()
        await app_a.shared_http_ingress.start()
        port_a = app_a.shared_http_ingress.bound_port

        async with ClientSession() as client:
            ok_a = await client.get(f"http://127.0.0.1:{port_a}/mini-a/")
            assert ok_a.status == 200
        await server_a.stop()
        await app_a.shared_http_ingress.stop()

        cfg_b = _build_config(tmp_path, intent="ingress_b", base_path="/mini-b")
        app_b = _fake_miniapp_bot(cfg_b)
        app_b.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
        server_b = MiniAppServer(app_b)
        await server_b.start()
        await app_b.shared_http_ingress.start()
        port_b = app_b.shared_http_ingress.bound_port

        async with ClientSession() as client:
            missing_old = await client.get(
                f"http://127.0.0.1:{port_b}/mini-a/",
                allow_redirects=False,
            )
            ok_b = await client.get(f"http://127.0.0.1:{port_b}/mini-b/")
            assert missing_old.status == 404
            assert ok_b.status == 200
        await server_b.stop()
        await app_b.shared_http_ingress.stop()

    asyncio.run(_run())


def test_miniapp_server_hides_mounted_surface_after_runtime_disable(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="shared_ingress_disable", base_path="/cli-proxy")
        app = _fake_miniapp_bot(cfg)
        app.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
        server = MiniAppServer(app)

        await server.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            before = await client.get(f"http://127.0.0.1:{port}/cli-proxy/")
            assert before.status == 200

            app.config.miniapp.enabled = False

            after = await client.get(
                f"http://127.0.0.1:{port}/cli-proxy/",
                allow_redirects=False,
            )
            assert after.status == 404

        await server.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_miniapp_server_keeps_mounted_base_path_until_restart(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="shared_ingress_base_path_reload", base_path="/old")
        app = _fake_miniapp_bot(cfg)
        app.shared_http_ingress = SharedHttpIngress(host="127.0.0.1", port=0)
        server = MiniAppServer(app)

        await server.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            before = await client.get(f"http://127.0.0.1:{port}/old/")
            assert before.status == 200

            app.config.miniapp.base_path = "/new"

            old_after = await client.get(f"http://127.0.0.1:{port}/old/")
            new_after = await client.get(
                f"http://127.0.0.1:{port}/new/",
                allow_redirects=False,
            )
            assert old_after.status == 200
            assert new_after.status == 404

        await server.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_shared_http_ingress_from_config_reads_miniapp_bind_settings(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
telegram:
  token: "t"
  whitelist_chat_ids: [1]
  admlist_chat_ids: [1]
tools:
  dummy:
    mode: headless
    cmd: ["bash", "-lc", "cat"]
defaults:
  workdir: "."
miniapp:
  enabled: true
  bind_host: "0.0.0.0"
  bind_port: 8099
  base_path: "/cli-proxy"
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(str(path))

    ingress = SharedHttpIngress.from_config(cfg)

    assert ingress.host == "0.0.0.0"
    assert ingress.port == 8099
