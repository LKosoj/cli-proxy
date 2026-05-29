from __future__ import annotations

import logging
from typing import Any, Optional

from aiohttp import web


logger = logging.getLogger("miniapp")


class SharedHttpIngress:
    """Shared aiohttp TCP listener for HTTP surfaces mounted by the application."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8088
    DEFAULT_CLIENT_MAX_SIZE = 1024 ** 2
    MINIAPP_REQUEST_BODY_OVERHEAD_BYTES = 64 * 1024
    MINIAPP_REQUEST_BODY_EXPANSION_FACTOR = 2

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_max_size: Optional[int] = None,
        logger_instance: Optional[logging.Logger] = None,
    ) -> None:
        self.host = str(host or self.DEFAULT_HOST)
        self.port = int(port)
        self._logger = logger_instance or logger
        self._client_max_size = max(1, int(client_max_size or self.DEFAULT_CLIENT_MAX_SIZE))
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.BaseSite] = None
        self._bound_port: int = int(port)
        self._mounted_paths: set[str] = set()
        self._registered_routes: set[tuple[str, str]] = set()
        self._root = web.Application(client_max_size=self._client_max_size)

    @classmethod
    def from_config(cls, config: Any, *, logger_instance: Optional[logging.Logger] = None) -> "SharedHttpIngress":
        miniapp_cfg = getattr(config, "miniapp", None)
        host = str(getattr(miniapp_cfg, "bind_host", cls.DEFAULT_HOST) or cls.DEFAULT_HOST)
        port = int(getattr(miniapp_cfg, "bind_port", cls.DEFAULT_PORT))
        return cls(
            host=host,
            port=port,
            client_max_size=cls.recommended_client_max_size_for_config(config),
            logger_instance=logger_instance,
        )

    @classmethod
    def recommended_client_max_size_for_config(cls, config: Any) -> int:
        miniapp_cfg = getattr(config, "miniapp", None)
        return cls._recommended_client_max_size_for_miniapp_config(miniapp_cfg)

    @classmethod
    def _recommended_client_max_size_for_miniapp_config(cls, miniapp_cfg: Any) -> int:
        if not bool(getattr(miniapp_cfg, "enabled", False)):
            return cls.DEFAULT_CLIENT_MAX_SIZE
        max_edit_kb = max(1, int(getattr(miniapp_cfg, "max_edit_file_size_kb", 5120) or 5120))
        max_edit_bytes = max_edit_kb * 1024
        # MiniApp writes send file content inside a JSON string, so the HTTP body can be
        # noticeably larger than the final file size because of escaping.
        return max(
            cls.DEFAULT_CLIENT_MAX_SIZE,
            (max_edit_bytes * cls.MINIAPP_REQUEST_BODY_EXPANSION_FACTOR) + cls.MINIAPP_REQUEST_BODY_OVERHEAD_BYTES,
        )

    @staticmethod
    def normalize_base_path(path: str) -> str:
        token = str(path or "/cli-proxy").strip()
        if not token.startswith("/"):
            token = "/" + token
        return token.rstrip("/") or "/cli-proxy"

    @property
    def bound_port(self) -> int:
        return int(self._bound_port)

    @property
    def client_max_size(self) -> int:
        return int(self._client_max_size)

    def ensure_client_max_size(self, size: int) -> None:
        normalized = max(1, int(size))
        if normalized <= self._client_max_size:
            return
        if self._runner is not None:
            raise RuntimeError("cannot change client max size after ingress start")
        self._client_max_size = normalized
        setattr(self._root, "_client_max_size", normalized)

    def mount_subapp(self, base_path: str, subapp: web.Application, *, redirect_to_slash: bool = True) -> None:
        if self._runner is not None:
            raise RuntimeError("cannot mount subapp after ingress start")
        normalized = self.normalize_base_path(base_path)
        if normalized in self._mounted_paths:
            raise RuntimeError(f"subapp already mounted for base path: {normalized}")

        if redirect_to_slash:
            async def _redirect(_request: web.Request) -> web.Response:
                raise web.HTTPFound(normalized + "/")

            self._root.router.add_get(normalized, _redirect)
        self._root.add_subapp(normalized + "/", subapp)
        self._mounted_paths.add(normalized)

    def add_route(self, method: str, path: str, handler) -> None:
        if self._runner is not None:
            raise RuntimeError("cannot add route after ingress start")
        normalized_path = self.normalize_base_path(path)
        normalized_method = str(method or "GET").strip().upper() or "GET"
        route_key = (normalized_method, normalized_path)
        if route_key in self._registered_routes:
            raise RuntimeError(f"route already registered: {normalized_method} {normalized_path}")
        self._root.router.add_route(normalized_method, normalized_path, handler)
        self._registered_routes.add(route_key)

    async def start(self) -> None:
        if self._site is not None:
            return
        if not self._mounted_paths and not self._registered_routes:
            self._logger.info("shared http ingress has no registered routes")
            return
        self._runner = web.AppRunner(self._root)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        self._bound_port = self._resolve_bound_port()
        self._logger.info("shared http ingress started on %s:%s", self.host, self._bound_port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._bound_port = int(self.port)
        self._mounted_paths = set()
        self._registered_routes = set()
        self._root = web.Application(client_max_size=self._client_max_size)
        self._logger.info("shared http ingress stopped")

    def _resolve_bound_port(self) -> int:
        if self._site is None:
            return int(self.port)
        server = getattr(self._site, "_server", None)
        sockets = list(getattr(server, "sockets", []) or [])
        if not sockets:
            return int(self.port)
        sockname = sockets[0].getsockname()
        try:
            return int(sockname[1])
        except Exception:
            return int(self.port)
