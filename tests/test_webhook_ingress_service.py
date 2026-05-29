from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp import ClientSession

from app.events.bus import SystemEventBus, WebhookReceivedEvent
from app.services.shared_http_ingress import SharedHttpIngress
from app.services.webhook_delivery_repository import WebhookDeliveryRepository
from app.services.webhook_ingress_service import WebhookIngressService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig, WebhooksConfig


def _build_config(tmp_path, *, intent: str, secret_token: str = "secret-token", max_payload_bytes: int = 4096) -> AppConfig:
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
        miniapp=MiniAppConfig(enabled=False),
        webhooks=WebhooksConfig(
            enabled=True,
            path="/webhooks/telegram",
            secret_token=secret_token,
            max_payload_bytes=max_payload_bytes,
        ),
    )


def _json_payload_bytes(total_size: int) -> bytes:
    prefix = b'{"message":"'
    suffix = b'"}'
    min_size = len(prefix) + len(suffix)
    if total_size < min_size:
        raise ValueError("total_size is too small for a json object payload")
    return prefix + (b"a" * (total_size - min_size)) + suffix


def test_webhook_ingress_shared_http_has_no_legacy_for_bot_app_bridge() -> None:
    assert not hasattr(SharedHttpIngress, "for_bot_app")


async def _stream_payload(body: bytes, *, chunk_size: int = 11):
    for offset in range(0, len(body), chunk_size):
        yield body[offset:offset + chunk_size]


def test_webhooks_are_available_when_miniapp_is_disabled_and_publish_event(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="webhook_only")
        bus = SystemEventBus()
        events: list[WebhookReceivedEvent] = []

        def _capture(event: WebhookReceivedEvent) -> None:
            events.append(event)

        bus.subscribe(WebhookReceivedEvent, _capture)
        app = SimpleNamespace(
            config=cfg,
            system_event_bus=bus,
            shared_http_ingress=SharedHttpIngress(host="127.0.0.1", port=0),
            webhook_delivery_repository=WebhookDeliveryRepository(cfg.defaults.state_path),
        )
        service = WebhookIngressService(app)
        await service.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            health = await client.get(f"http://127.0.0.1:{port}/health")
            assert health.status == 200
            assert await health.json() == {
                "ok": True,
                "status": "ok",
                "miniapp_enabled": False,
                "webhooks_enabled": True,
            }
            response = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={"update_id": 101, "message": "hi"},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-1",
                },
            )
            assert response.status == 202
            body = await response.json()
            assert body == {"ok": True, "provider": "telegram", "duplicate": False}

        assert len(events) == 1
        assert events[0].source == "telegram"
        assert events[0].path == "/webhooks/telegram"
        assert events[0].payload == {"update_id": 101, "message": "hi"}

        await service.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_webhook_ingress_verifies_secret_and_sequential_runs_do_not_leak_state(tmp_path) -> None:
    async def _exercise(cfg: AppConfig, *, expected_delivery: str, wrong_secret: bool = False) -> tuple[int, list[WebhookReceivedEvent]]:
        bus = SystemEventBus()
        events: list[WebhookReceivedEvent] = []

        def _capture(event: WebhookReceivedEvent) -> None:
            events.append(event)

        bus.subscribe(WebhookReceivedEvent, _capture)
        app = SimpleNamespace(
            config=cfg,
            system_event_bus=bus,
            shared_http_ingress=SharedHttpIngress(host="127.0.0.1", port=0),
            webhook_delivery_repository=WebhookDeliveryRepository(cfg.defaults.state_path),
        )
        service = WebhookIngressService(app)
        await service.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        header_secret = "wrong-secret" if wrong_secret else str(cfg.webhooks.secret_token or "")
        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={"delivery": expected_delivery},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": header_secret,
                    "X-Webhook-Delivery-Id": expected_delivery,
                },
            )
            status = int(response.status)

        await service.stop()
        await app.shared_http_ingress.stop()
        return status, events

    cfg_a = _build_config(tmp_path, intent="webhook_seq_a", secret_token="secret-a")
    cfg_b = _build_config(tmp_path, intent="webhook_seq_b", secret_token="secret-b")

    first_status, first_events = asyncio.run(_exercise(cfg_a, expected_delivery="delivery-a", wrong_secret=True))
    second_status, second_events = asyncio.run(_exercise(cfg_b, expected_delivery="delivery-b", wrong_secret=False))

    assert first_status == 401
    assert first_events == []
    assert second_status == 202
    assert len(second_events) == 1
    assert second_events[0].payload == {"delivery": "delivery-b"}


def test_webhook_ingress_sets_correlation_id_and_dry_run_on_event(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="webhook_corr")
        bus = SystemEventBus()
        events: list[WebhookReceivedEvent] = []

        bus.subscribe(WebhookReceivedEvent, events.append)
        app = SimpleNamespace(
            config=cfg,
            system_event_bus=bus,
            shared_http_ingress=SharedHttpIngress(host="127.0.0.1", port=0),
            webhook_delivery_repository=WebhookDeliveryRepository(cfg.defaults.state_path),
        )
        service = WebhookIngressService(app)
        await service.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={"mode_id": "capture", "dry_run": True},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-corr",
                    "X-Correlation-Id": "corr-telegram-1",
                },
            )
            assert response.status == 202

        assert len(events) == 1
        assert events[0].correlation_id == "corr-telegram-1"
        assert events[0].dry_run is True
        assert events[0].source == "telegram"

        await service.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_webhook_ingress_rejects_stale_path_after_runtime_config_change(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="webhook_path_switch")
        bus = SystemEventBus()
        app = SimpleNamespace(
            config=cfg,
            system_event_bus=bus,
            shared_http_ingress=SharedHttpIngress(host="127.0.0.1", port=0),
            webhook_delivery_repository=WebhookDeliveryRepository(cfg.defaults.state_path),
        )
        service = WebhookIngressService(app)
        await service.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        async with ClientSession() as client:
            before = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={"delivery": "before-switch"},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-before-switch",
                },
            )
            assert before.status == 202

            app.config.webhooks.path = "/webhooks/new-path"

            stale = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                json={"delivery": "stale-path"},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-stale-path",
                },
            )
            assert stale.status == 404

            fresh = await client.post(
                f"http://127.0.0.1:{port}/webhooks/new-path",
                json={"delivery": "fresh-path"},
                headers={
                    "X-Telegram-Bot-Api-Secret-Token": "secret-token",
                    "X-Webhook-Delivery-Id": "delivery-fresh-path",
                },
            )
            assert fresh.status == 404

        await service.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())


def test_max_payload_bytes_enforcement(tmp_path) -> None:
    async def _run() -> None:
        limit = 128
        cfg = _build_config(tmp_path, intent="webhook_payload_limit", max_payload_bytes=limit)
        bus = SystemEventBus()
        events: list[WebhookReceivedEvent] = []

        bus.subscribe(WebhookReceivedEvent, events.append)
        app = SimpleNamespace(
            config=cfg,
            system_event_bus=bus,
            shared_http_ingress=SharedHttpIngress(host="127.0.0.1", port=0),
            webhook_delivery_repository=WebhookDeliveryRepository(cfg.defaults.state_path),
        )
        service = WebhookIngressService(app)
        await service.start()
        await app.shared_http_ingress.start()
        port = app.shared_http_ingress.bound_port

        payload_small = _json_payload_bytes(limit - 1)
        payload_exact = _json_payload_bytes(limit)
        payload_large = _json_payload_bytes(limit + 1)
        base_headers = {
            "X-Telegram-Bot-Api-Secret-Token": "secret-token",
            "Content-Type": "application/json",
        }

        async with ClientSession() as client:
            small = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                data=payload_small,
                headers={**base_headers, "X-Webhook-Delivery-Id": "delivery-small"},
            )
            assert small.status == 202

            exact = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                data=payload_exact,
                headers={**base_headers, "X-Webhook-Delivery-Id": "delivery-exact"},
            )
            assert exact.status == 202

            too_large_with_length = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                data=payload_large,
                headers={**base_headers, "X-Webhook-Delivery-Id": "delivery-large"},
            )
            assert too_large_with_length.status == 413
            assert too_large_with_length.request_info.headers.get("Content-Length") == str(len(payload_large))

            too_large_chunked = await client.post(
                f"http://127.0.0.1:{port}/webhooks/telegram",
                data=_stream_payload(payload_large),
                chunked=True,
                headers={**base_headers, "X-Webhook-Delivery-Id": "delivery-chunked"},
            )
            assert too_large_chunked.status == 413
            assert too_large_chunked.request_info.headers.get("Transfer-Encoding") == "chunked"
            assert "Content-Length" not in too_large_chunked.request_info.headers

        assert [event.payload["message"] for event in events] == [
            payload_small.decode("utf-8")[12:-2],
            payload_exact.decode("utf-8")[12:-2],
        ]

        await service.stop()
        await app.shared_http_ingress.stop()

    asyncio.run(_run())
