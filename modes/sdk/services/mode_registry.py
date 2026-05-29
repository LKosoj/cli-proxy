from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from sessions.session_state_access import get_active_mode


@dataclass
class ModeRegistryService:
    registry: Optional[Any] = None
    log: logging.Logger = logging.getLogger(__name__)

    def load_modes(
        self,
        *,
        modes_dir: Optional[Path] = None,
        module_prefix: str = "modes.dynamic",
        excluded_dirs: Optional[set[str]] = None,
    ) -> Any:
        """
        Загружает плагины modes в in-memory реестр и сохраняет его в self.registry.

        Desktop bootstrap ожидает наличие этого метода, чтобы инициализировать реестр
        до построения фасада/окна.
        """
        from modes.registry import ModeLoader, ModeRegistry

        reg = ModeRegistry()
        loader = ModeLoader(modes_dir=modes_dir, module_prefix=str(module_prefix), excluded_dirs=excluded_dirs)
        loader.load_into(reg)
        self.registry = reg
        return reg

    def _registry(self, registry: Optional[Any] = None) -> Optional[Any]:
        return registry if registry is not None else self.registry

    def get(self, mode_id: str, *, registry: Optional[Any] = None) -> Optional[Any]:
        mid = str(mode_id or "").strip()
        if not mid:
            return None
        reg = self._registry(registry)
        if reg is None:
            return None
        try:
            return reg.get(mid)
        except Exception:
            return None

    def get_active_mode_id(self, session: Any) -> str:
        return str(get_active_mode(session, "") or "").strip()

    def get_active_mode(self, session: Any, *, registry: Optional[Any] = None) -> Optional[Any]:
        return self.get(self.get_active_mode_id(session), registry=registry)

    def list_modes(self, *, registry: Optional[Any] = None) -> list[tuple[str, str]]:
        reg = self._registry(registry)
        if not reg:
            return []
        out: list[tuple[str, str]] = []
        try:
            ids: Iterable[str] = reg.list_ids()
        except Exception:
            return []
        for mode_id in ids:
            mode = self.get(str(mode_id), registry=reg)
            if mode is None:
                continue
            label = str(getattr(mode, "display_name", "") or mode_id).strip()
            out.append((str(mode_id), label))
        return out

    def allows_agent_plugin_ui(self, session: Any, *, registry: Optional[Any] = None) -> bool:
        mode = self.get_active_mode(session, registry=registry)
        if mode is None:
            return False
        try:
            return bool(mode.allows_agent_plugin_ui())
        except Exception:
            return False

    def initialize_plugins(self, *, config: Any, services: dict[str, Any], registry: Optional[Any] = None) -> None:
        reg = self._registry(registry)
        if reg is None:
            return
        try:
            items = reg.modes.items()
        except Exception:
            self.log.exception("mode init: registry iteration failed")
            return
        for mode_id, plugin in items:
            try:
                plugin.initialize(config, services)
            except Exception:
                self.log.exception("mode init failed: %s", mode_id)
