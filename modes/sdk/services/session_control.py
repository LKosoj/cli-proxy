from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


PersistSessionsFn = Callable[[], Any]
CancelModeTasksFn = Callable[[str, str, float], Awaitable[int]]
CancelSessionTasksFn = Callable[[str, float], Awaitable[int]]


@dataclass
class SessionControlService:
    persist_sessions: Optional[PersistSessionsFn] = None
    cancel_mode_tasks: Optional[CancelModeTasksFn] = None
    cancel_session_tasks: Optional[CancelSessionTasksFn] = None

    def persist(self) -> None:
        if not self.persist_sessions:
            raise RuntimeError("SessionControlService.persist_sessions is not configured")
        self.persist_sessions()

    async def cancel_mode(self, *, session_id: str, mode_id: str, timeout_s: float = 0.2) -> int:
        if not self.cancel_mode_tasks:
            raise RuntimeError("SessionControlService.cancel_mode_tasks is not configured")
        return int(await self.cancel_mode_tasks(str(session_id), str(mode_id), float(timeout_s)))

    async def cancel_session(self, *, session_id: str, timeout_s: float = 0.2) -> int:
        if not self.cancel_session_tasks:
            raise RuntimeError("SessionControlService.cancel_session_tasks is not configured")
        return int(await self.cancel_session_tasks(str(session_id), float(timeout_s)))
