from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from desktop.services.application_facade import ApplicationFacade, AppNotification


@dataclass(slots=True)
class DesktopUiState:
    """Модель состояния UI для сохранения между сессиями."""
    window_geometry: Optional[str] = None  # Base64 encoded QByteArray
    window_state: Optional[str] = None     # Base64 encoded QByteArray
    active_tab: str = "chat"
    last_session_id: Optional[str] = None
    theme: str = "system"
    recent_sessions: List[str] = field(default_factory=list)
    sidebar_collapsed: bool = False
    splitter_sizes: List[int] = field(default_factory=list)
    session_panel_visible: bool = True
    context_panel_visible: bool = False
    context_panel_tool: str = "none"
    command_palette_last_query: str = ""
    command_palette_recent: List[str] = field(default_factory=list)
    # Per-session chat history.
    # Phase 1: entries may also contain "attachments": List[Dict[str, Any]].
    chat_history: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)


class DesktopUiStateService:
    """Сервис управления состоянием UI (геометрия, вкладки, настройки)."""

    def __init__(self, facade: ApplicationFacade, logger: Optional[logging.Logger] = None):
        self.facade = facade
        self.logger = logger or logging.getLogger(__name__)
        self.state = DesktopUiState()
        self._path: Optional[str] = None
        self._ready_event = asyncio.Event()
        self._unsubscribe = self.facade.subscribe(self._on_notification)

    @property
    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    async def wait_ready(self) -> None:
        await self._ready_event.wait()

    def _on_notification(self, note: AppNotification) -> None:
        if note.event == "startup:ready":
            # Загрузка состояния строго по сигналу от ApplicationFacade
            asyncio.create_task(self.load())

    async def load(self) -> DesktopUiState:
        """Загрузка состояния из файла (не блокирует event loop)."""
        if not self.facade.runtime_params:
            self.logger.warning("facade runtime_params not ready, cannot load ui state")
            return self.state

        self._path = self.facade.runtime_params.desktop_state_path

        def _read():
            if not os.path.exists(self._path):
                return None
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            data = await asyncio.to_thread(_read)
            if data:
                # Оптимизация: фильтрация полей для исключения ошибок десериализации
                # при изменении модели в будущем.
                valid_fields = {f.name for f in DesktopUiState.__dataclass_fields__.values()}
                filtered_data = {k: v for k, v in data.items() if k in valid_fields}
                self.state = DesktopUiState(**filtered_data)
                self.logger.info("ui state loaded from %s", self._path)
        except Exception:
            self.logger.exception("failed to load ui state from %s", self._path)

        self._ready_event.set()
        self.facade.notify("ui_state:ready", path=self._path)
        return self.state

    async def save(self, **updates: Any) -> None:
        """Сохранение состояния (атомарно, не блокирует event loop)."""
        if not self._path:
            if self.facade.runtime_params:
                self._path = self.facade.runtime_params.desktop_state_path
            else:
                self.logger.warning("cannot save ui state: path unknown")
                return

        for k, v in updates.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)

        def _write(path: str, data: dict) -> None:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=parent or ".",
                prefix=".desktop_state_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        try:
            await asyncio.to_thread(_write, self._path, asdict(self.state))
            self.logger.info("ui state saved to %s", self._path)
        except Exception:
            self.logger.exception("failed to save ui state to %s", self._path)

    def shutdown(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
