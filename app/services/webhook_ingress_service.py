from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from aiohttp import web

from app.events.bus import WebhookReceivedEvent
from app.services.shared_http_ingress import SharedHttpIngress
from app.services.webhook_delivery_repository import WebhookDeliveryRepository
from modes.sdk.runtime.json_normalizer import loads_safe


logger = logging.getLogger("miniapp")


class WebhookIngressService:
    HEALTH_PATH = "/health"
    EVENT_NAME = WebhookReceivedEvent.EVENT_NAME
    BODY_READ_CHUNK_SIZE = 64 * 1024
    DELIVERY_ID_HEADERS = (
        "X-Webhook-Delivery-Id",
        "X-GitHub-Delivery",
        "X-Request-Id",
        "X-Delivery-Id",
    )
    CORRELATION_ID_HEADERS = (
        "X-Correlation-Id",
        "X-Webhook-Correlation-Id",
        "X-Request-Id",
    )
    SECRET_HEADERS = (
        "X-Telegram-Bot-Api-Secret-Token",
        "X-Webhook-Secret-Token",
    )

    def __init__(self, bot_app: Any) -> None:
        self.bot_app = bot_app
        self._started = False

    def _shared_ingress(self) -> SharedHttpIngress:
        ingress = getattr(self.bot_app, "shared_http_ingress", None)
        if ingress is None:
            ingress = SharedHttpIngress.from_config(getattr(self.bot_app, "config", None))
            setattr(self.bot_app, "shared_http_ingress", ingress)
        return ingress

    def _delivery_repo(self) -> WebhookDeliveryRepository:
        repo = getattr(self.bot_app, "webhook_delivery_repository", None)
        if repo is None:
            repo = WebhookDeliveryRepository(getattr(self.bot_app.config.defaults, "state_path", "state.json"))
            setattr(self.bot_app, "webhook_delivery_repository", repo)
        return repo

    def _verify_secret(self, request: web.Request) -> None:
        cfg = getattr(self.bot_app.config, "webhooks", None)
        expected = str(getattr(cfg, "secret_token", "") or "").strip()
        if not expected:
            return
        provided = ""
        for header_name in self.SECRET_HEADERS:
            token = str(request.headers.get(header_name, "") or "").strip()
            if token:
                provided = token
                break
        if provided != expected:
            raise web.HTTPUnauthorized(reason="invalid webhook secret")

    def _max_payload_bytes(self) -> int:
        cfg = getattr(self.bot_app.config, "webhooks", None)
        limit = int(getattr(cfg, "max_payload_bytes", 1048576) or 1048576)
        return max(1, limit)

    def _configured_path(self) -> str:
        cfg = getattr(self.bot_app.config, "webhooks", None)
        raw_path = str(getattr(cfg, "path", "/webhooks/telegram") or "/webhooks/telegram")
        return SharedHttpIngress.normalize_base_path(raw_path)

    @staticmethod
    def _payload_too_large(*, max_size: int, actual_size: int) -> web.HTTPRequestEntityTooLarge:
        return web.HTTPRequestEntityTooLarge(max_size=max_size, actual_size=actual_size)

    @classmethod
    def _extract_delivery_id(cls, request: web.Request) -> str:
        for header_name in cls.DELIVERY_ID_HEADERS:
            token = str(request.headers.get(header_name, "") or "").strip()
            if token:
                return token
        return ""

    @classmethod
    def _extract_correlation_id(cls, request: web.Request, *, delivery_id: str = "") -> str:
        for header_name in cls.CORRELATION_ID_HEADERS:
            token = str(request.headers.get(header_name, "") or "").strip()
            if token:
                return token
        delivery_token = str(delivery_id or "").strip()
        if delivery_token:
            return delivery_token
        return uuid.uuid4().hex

    @staticmethod
    def _extract_dry_run(payload: dict[str, Any]) -> bool:
        launch = payload.get("launch")
        launch_payload = dict(launch) if isinstance(launch, dict) else {}
        if "dry_run" in launch_payload:
            return bool(launch_payload.get("dry_run"))
        return bool(payload.get("dry_run", False))

    @classmethod
    async def _read_body_with_limit(cls, request: web.Request, *, max_payload_bytes: int) -> bytes:
        content_length = request.content_length
        if content_length is not None and int(content_length) > max_payload_bytes:
            raise cls._payload_too_large(max_size=max_payload_bytes, actual_size=int(content_length))

        chunks: list[bytes] = []
        total_size = 0
        chunk_size = min(cls.BODY_READ_CHUNK_SIZE, max_payload_bytes)
        async for chunk in request.content.iter_chunked(chunk_size):
            if not chunk:
                continue
            total_size += len(chunk)
            if total_size > max_payload_bytes:
                raise cls._payload_too_large(max_size=max_payload_bytes, actual_size=total_size)
            chunks.append(chunk)
        return b"".join(chunks)

    @classmethod
    async def _parse_payload(cls, request: web.Request, *, max_payload_bytes: int) -> dict[str, Any]:
        body = await cls._read_body_with_limit(request, max_payload_bytes=max_payload_bytes)
        if not body:
            return {}
        try:
            parsed = loads_safe(body.decode("utf-8"), strict_first=True)
        except Exception as exc:
            raise web.HTTPBadRequest(reason="invalid json body") from exc
        if not isinstance(parsed, dict):
            raise web.HTTPBadRequest(reason="webhook payload must be a json object")
        return dict(parsed)

    async def _publish_event(
        self,
        provider: str,
        request: web.Request,
        payload: dict[str, Any],
        *,
        correlation_id: str,
        dry_run: bool,
    ) -> None:
        bus = getattr(self.bot_app, "system_event_bus", None)
        if bus is None or not hasattr(bus, "publish"):
            return
        await bus.publish(
            WebhookReceivedEvent(
                source=str(provider or ""),
                path=str(request.path or ""),
                method=str(request.method or ""),
                correlation_id=str(correlation_id or ""),
                dry_run=bool(dry_run),
                headers={str(key): str(value) for key, value in request.headers.items()},
                payload=dict(payload),
            )
        )

    async def _handle(self, request: web.Request, *, provider: Optional[str] = None) -> web.Response:
        cfg = getattr(self.bot_app.config, "webhooks", None)
        if not cfg or not bool(getattr(cfg, "enabled", False)):
            raise web.HTTPNotFound()
        if SharedHttpIngress.normalize_base_path(str(request.path or "")) != self._configured_path():
            raise web.HTTPNotFound()
        resolved_provider = str(provider or "").strip() or "telegram"
        delivery_id = self._extract_delivery_id(request)
        correlation_id = self._extract_correlation_id(request, delivery_id=delivery_id)
        self._verify_secret(request)
        payload = await self._parse_payload(request, max_payload_bytes=self._max_payload_bytes())
        dry_run = self._extract_dry_run(payload)
        if delivery_id:
            claimed = self._delivery_repo().claim_delivery(
                source=resolved_provider,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not claimed:
                logger.info(
                    "webhook ingress duplicate provider=%s correlation_id=%s path=%s dry_run=%s",
                    resolved_provider,
                    correlation_id,
                    request.path,
                    dry_run,
                )
                return web.json_response({"ok": True, "duplicate": True, "provider": resolved_provider}, status=202)
        await self._publish_event(
            resolved_provider,
            request,
            payload,
            correlation_id=correlation_id,
            dry_run=dry_run,
        )
        logger.info(
            "webhook ingress accepted provider=%s correlation_id=%s path=%s dry_run=%s",
            resolved_provider,
            correlation_id,
            request.path,
            dry_run,
        )
        return web.json_response({"ok": True, "provider": resolved_provider, "duplicate": False}, status=202)

    async def _handle_telegram(self, request: web.Request) -> web.Response:
        return await self._handle(request, provider="telegram")

    async def _handle_health(self, _request: web.Request) -> web.Response:
        miniapp_cfg = getattr(self.bot_app.config, "miniapp", None)
        webhooks_cfg = getattr(self.bot_app.config, "webhooks", None)
        return web.json_response(
            {
                "ok": True,
                "status": "ok",
                "miniapp_enabled": bool(getattr(miniapp_cfg, "enabled", False)),
                "webhooks_enabled": bool(getattr(webhooks_cfg, "enabled", False)),
            }
        )

    async def start(self) -> None:
        cfg = getattr(self.bot_app.config, "webhooks", None)
        if not cfg or not bool(getattr(cfg, "enabled", False)):
            logger.info("webhooks disabled")
            return
        if self._started:
            return

        ingress = self._shared_ingress()
        ingress.add_route("GET", self.HEALTH_PATH, self._handle_health)
        ingress.add_route("POST", self._configured_path(), self._handle_telegram)

        self._started = True
        logger.info("webhook ingress mounted")

    async def stop(self) -> None:
        self._started = False
        logger.info("webhook ingress stopped")
