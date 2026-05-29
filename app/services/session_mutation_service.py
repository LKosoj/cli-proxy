from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sessions.queue_item import append_session_queue_item
from sessions.session_state_access import set_active_mode as set_session_active_mode

logger = logging.getLogger(__name__)


class SessionMutationService:
    """Public entrypoint for mutating session state."""

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager

    def persist_all(self) -> bool:
        manager = self._manager
        if manager is None:
            return False
        try:
            persist_all = getattr(manager, "_persist_sessions", None)
            if callable(persist_all):
                persist_all()
                return True
            persist = getattr(manager, "persist", None)
            if callable(persist):
                persist()
                return True
        except Exception:
            logger.exception("session mutation persist_all failed")
            return False
        return False

    def persist_session(
        self,
        session: Any | None = None,
        *,
        chat_id: Any | None = None,
        session_id: Any | None = None,
    ) -> bool:
        manager = self._manager
        if manager is None:
            return False
        sid = str(session_id or getattr(session, "id", "") or "").strip()
        owner_chat_id = chat_id if chat_id is not None else getattr(session, "chat_id", None)
        owner_id = None
        if owner_chat_id is not None:
            try:
                owner_id = int(owner_chat_id)
            except (TypeError, ValueError):
                owner_id = None
        persist_one = getattr(manager, "persist_session", None)
        if callable(persist_one) and sid and owner_id is not None:
            try:
                return bool(persist_one(owner_id, sid))
            except Exception:
                logger.exception(
                    "session mutation persist_session failed chat_id=%r session_id=%s",
                    owner_chat_id,
                    sid,
                )
        return self.persist_all()

    def set_active_mode(self, session: Any, mode_id: str | None, *, persist: bool = True) -> bool:
        normalized = str(mode_id).strip() if mode_id is not None else None
        set_session_active_mode(session, normalized or None)
        if persist:
            self.persist_session(session)
        return True

    def set_cli_work_type(self, session: Any, cli_work_type: str | None, *, persist: bool = True) -> bool:
        normalized = str(cli_work_type).strip() if cli_work_type is not None else None
        normalized = normalized or None
        cli_state = getattr(session, "cli", None)
        if cli_state is not None:
            try:
                cli_state.cli_work_type = normalized
            except Exception:
                logger.exception("session mutation set_cli_work_type nested update failed")
                setattr(session, "cli_work_type", normalized)
        else:
            setattr(session, "cli_work_type", normalized)
        if persist:
            self.persist_session(session)
        return True

    def append_queue_item(
        self,
        session: Any,
        raw: Any,
        *,
        fallback_dest: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> bool:
        if not append_session_queue_item(session, raw, fallback_dest=fallback_dest):
            return False
        if persist:
            self.persist_session(session)
        return True


__all__ = ("SessionMutationService",)
