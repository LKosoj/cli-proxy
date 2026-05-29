from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.path_normalization import normalize_optional_state_path
from app.services.session_mutation_service import SessionMutationService
from app.services.session_thread_repository import SessionThreadRepository
from app.services.task_service import CancellationToken, ManagedTask, TaskService
from session import Session, SessionManager, session_runtime_uid
from sessions.session_state_access import set_active_mode

logger = logging.getLogger(__name__)


class SessionService:
    """Сервисный слой над SessionManager c поддержкой отмены фоновых задач."""

    def __init__(self, manager: SessionManager, tasks: TaskService):
        self._manager = manager
        self._tasks = tasks
        self._session_mutations = SessionMutationService(manager)

    def create_session(self, chat_id: int, tool_name: Optional[str], workdir: str) -> Session:
        return self._manager.create(int(chat_id), tool_name, str(workdir))

    def list_sessions(self, chat_id: int) -> list[Session]:
        return list(self._manager.sessions_for_chat(int(chat_id)).values())

    def get_session(self, chat_id: str, session_id: str) -> Optional[Session]:
        return self._manager.get(chat_id, str(session_id))

    def get_session_by_uid(self, session_uid: str) -> Optional[Session]:
        return self._manager.get_by_uid(str(session_uid))

    def create_desktop_session(self, tool_name: Optional[str], workdir: str) -> Session:
        session = self._manager.create("desktop", tool_name, workdir)
        from sessions.conversation_scope import DesktopScope

        session.conversation_scope = DesktopScope("desktop", session.id)
        if not self._session_mutations.persist_all():
            logger.error(
                "legacy fallback used: persist sessions failed on create_desktop_session "
                "session_uid=%s session_id=%s workdir=%s",
                session_runtime_uid(session),
                session.id,
                workdir,
            )
        return session

    def list_desktop_sessions(self) -> list[Session]:
        return list(self._manager.sessions_for_chat("desktop").values())

    def get_session_by_scope(self, chat_id: int, message_thread_id: Optional[int] = None) -> Optional[Session]:
        return self._manager.get_by_scope(int(chat_id), message_thread_id)

    def set_mode(self, chat_id: int, session_id: str, mode_id: str) -> bool:
        session = self.get_session(chat_id, session_id)
        if session:
            mid = str(mode_id or "").strip()
            if not mid:
                return False
            set_active_mode(session, mid)
            # Persist immediately: Desktop expects mode to survive app restart.
            if not self._session_mutations.persist_all():
                logger.error(
                    "legacy fallback used: persist sessions failed on set_mode "
                    "chat_id=%s session_id=%s mode_id=%s",
                    chat_id,
                    session_id,
                    mid,
                )
            return True
        return False

    def clear_mode(self, chat_id: int, session_id: str) -> bool:
        session = self.get_session(chat_id, session_id)
        if session:
            set_active_mode(session, None)
            if not self._session_mutations.persist_all():
                logger.error(
                    "legacy fallback used: persist sessions failed on clear_mode "
                    "chat_id=%s session_id=%s",
                    chat_id,
                    session_id,
                )
            return True
        return False

    def set_active_cli(self, chat_id: int, session_id: str, cli_name: str) -> bool:
        session = self.get_session(chat_id, session_id)
        if session:
            if not self._session_is_idle(session):
                return False
            try:
                session.set_active_cli(str(cli_name))
                return True
            except ValueError:
                logger.warning(
                    "set_active_cli rejected invalid cli chat_id=%s session_id=%s cli_name=%s",
                    chat_id,
                    session_id,
                    cli_name,
                    exc_info=True,
                )
                return False
        return False

    async def close_session(self, chat_id: int, session_id: str, *, cancel_timeout_s: float = 1.0) -> bool:
        owner_chat_id = int(chat_id)
        sid = str(session_id or "").strip()
        session = self.get_session(owner_chat_id, sid)
        runtime_uid = session_runtime_uid(session) if session is not None else sid
        closed = self._manager.close(int(chat_id), str(session_id))
        if not closed:
            return False
        self._cleanup_thread_mapping(owner_chat_id=owner_chat_id, session_id=sid)
        await self._tasks.cancel_session_tasks(session_uid=runtime_uid, timeout_s=float(cancel_timeout_s))
        return True

    async def close_session_by_uid(self, session_uid: str, *, cancel_timeout_s: float = 1.0) -> bool:
        session = self.get_session_by_uid(session_uid)
        owner_chat_id = getattr(session, "chat_id", None) if session is not None else None
        sid = str(getattr(session, "id", "") or "").strip()
        runtime_uid = session_runtime_uid(session) if session is not None else str(session_uid or "").strip()
        closed = self._manager.close_by_uid(str(session_uid))
        if not closed:
            return False
        self._cleanup_thread_mapping(owner_chat_id=owner_chat_id, session_id=sid)
        await self._tasks.cancel_session_tasks(session_uid=runtime_uid, timeout_s=float(cancel_timeout_s))
        return True

    def _cleanup_thread_mapping(self, *, owner_chat_id: object, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        try:
            owner = int(owner_chat_id)
        except Exception:
            logger.warning(
                "legacy fallback used: thread mapping cleanup skipped invalid owner "
                "owner_chat_id=%r session_id=%s",
                owner_chat_id,
                sid,
                exc_info=True,
            )
            return
        try:
            defaults = getattr(getattr(self._manager, "config", None), "defaults", None)
            state_path = normalize_optional_state_path(getattr(defaults, "state_path", None))
        except TypeError:
            logger.warning(
                "legacy fallback used: thread mapping cleanup skipped invalid state_path "
                "owner_chat_id=%s session_id=%s",
                owner,
                sid,
                exc_info=True,
            )
            state_path = None
        if not state_path:
            return
        try:
            SessionThreadRepository(state_path).delete_by_session(owner_chat_id=owner, session_id=sid)
        except Exception:
            logger.exception(
                "legacy fallback used: thread mapping cleanup failed on session close "
                "owner_chat_id=%s session_id=%s",
                owner,
                sid,
            )

    def _resolve_runtime_task_session_uid(self, session_token: str) -> str:
        token = str(session_token or "").strip()
        if not token:
            return ""
        session = self.get_session_by_uid(token)
        if session is not None:
            return session_runtime_uid(session)
        matches: list[Session] = []
        for session_map in getattr(self._manager, "sessions_by_chat", {}).values():
            if not isinstance(session_map, dict):
                continue
            session = session_map.get(token)
            if session is not None:
                matches.append(session)
        if len(matches) == 1:
            return session_runtime_uid(matches[0])
        return token

    def start_background_task(self, session_id: str, name: str, runner) -> ManagedTask:
        return self._tasks.create(
            session_uid=self._resolve_runtime_task_session_uid(session_id),
            name=str(name),
            runner=runner,
        )

    async def cancel_background_tasks(self, session_id: str, *, reason: str = "cancelled", timeout_s: float = 1.0) -> int:
        return await self._tasks.cancel_session(
            session_uid=self._resolve_runtime_task_session_uid(session_id),
            reason=str(reason),
            timeout_s=float(timeout_s),
        )

    @staticmethod
    def throw_if_cancelled(token: CancellationToken) -> None:
        token.throw_if_cancelled()

    @staticmethod
    def _session_is_idle(session: Any) -> bool:
        busy = bool(getattr(session, "busy", False))
        run_lock = getattr(session, "run_lock", None)
        locked = bool(run_lock.locked()) if run_lock is not None else False
        tick_active = bool(
            getattr(session, "is_active_by_tick", None)
            and session.is_active_by_tick()
        )
        return not (busy or locked or tick_active)

    def set_mode_by_uid(self, session_uid: str, mode_id: str) -> bool:
        session = self.get_session_by_uid(session_uid)
        if session:
            mid = str(mode_id or "").strip()
            if not mid:
                return False
            set_active_mode(session, mid)
            if not self._session_mutations.persist_all():
                logger.error(
                    "legacy fallback used: persist sessions failed on set_mode_by_uid "
                    "session_uid=%s session_id=%s mode_id=%s",
                    session_uid,
                    getattr(session, "id", ""),
                    mid,
                )
            return True
        return False

    def clear_mode_by_uid(self, session_uid: str) -> bool:
        session = self.get_session_by_uid(session_uid)
        if session:
            set_active_mode(session, None)
            if not self._session_mutations.persist_all():
                logger.error(
                    "legacy fallback used: persist sessions failed on clear_mode_by_uid "
                    "session_uid=%s session_id=%s",
                    session_uid,
                    getattr(session, "id", ""),
                )
            return True
        return False

    def set_active_cli_by_uid(self, session_uid: str, cli_name: str) -> bool:
        session = self.get_session_by_uid(session_uid)
        if session:
            if not self._session_is_idle(session):
                return False
            try:
                session.set_active_cli(str(cli_name))
                return True
            except ValueError:
                logger.warning(
                    "set_active_cli_by_uid rejected invalid cli session_uid=%s session_id=%s cli_name=%s",
                    session_uid,
                    getattr(session, "id", ""),
                    cli_name,
                    exc_info=True,
                )
                return False
        return False
