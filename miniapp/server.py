import logging
from typing import Any, Optional

from aiohttp import web

from app.services.shared_http_ingress import SharedHttpIngress
from .routes import MiniAppRoutes

logger = logging.getLogger("miniapp")


class MiniAppServer:
    def __init__(self, bot_app: Any):
        self.bot_app = bot_app
        self._base_path: Optional[str] = None
        self._started: bool = False

    @staticmethod
    def _normalize_base_path(path: str) -> str:
        return SharedHttpIngress.normalize_base_path(path)

    def _shared_ingress(self) -> SharedHttpIngress:
        ingress = getattr(self.bot_app, "shared_http_ingress", None)
        if ingress is None:
            ingress = SharedHttpIngress.from_config(getattr(self.bot_app, "config", None))
            setattr(self.bot_app, "shared_http_ingress", ingress)
        return ingress

    def _request_body_limit_bytes(self) -> int:
        return SharedHttpIngress.recommended_client_max_size_for_config(getattr(self.bot_app, "config", None))

    def _current_base_path(self) -> str:
        if self._base_path is not None:
            return self._base_path
        cfg = getattr(self.bot_app.config, "miniapp", None)
        return self._normalize_base_path(str(getattr(cfg, "base_path", "/cli-proxy") or "/cli-proxy"))

    @web.middleware
    async def _runtime_guard(self, request: web.Request, handler) -> web.StreamResponse:
        cfg = getattr(self.bot_app.config, "miniapp", None)
        if not cfg or not bool(getattr(cfg, "enabled", False)):
            raise web.HTTPNotFound()
        current_base_path = self._current_base_path()
        request_path = str(request.path or "")
        if request_path != current_base_path and not request_path.startswith(current_base_path + "/"):
            raise web.HTTPNotFound()
        return await handler(request)

    async def start(self) -> None:
        cfg = getattr(self.bot_app.config, "miniapp", None)
        if not cfg or not bool(getattr(cfg, "enabled", False)):
            logger.info("miniapp disabled")
            return
        if self._started:
            return

        base_path = self._normalize_base_path(str(getattr(cfg, "base_path", "/cli-proxy")))
        ingress = self._shared_ingress()
        request_body_limit = self._request_body_limit_bytes()
        ingress.ensure_client_max_size(request_body_limit)
        subapp = web.Application(client_max_size=request_body_limit, middlewares=[self._runtime_guard])
        MiniAppRoutes(self.bot_app).register(subapp)
        ingress.mount_subapp(base_path, subapp)
        self._base_path = base_path
        self._started = True
        logger.info("miniapp mounted on shared ingress at %s", base_path)

    async def stop(self) -> None:
        self._base_path = None
        self._started = False
        logger.info("miniapp stopped")
