from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from app.services.actor_identity import DESKTOP_ACTOR_ID, desktop_actor_id
from app.services.project_registry import (
    ProjectOwnershipError,
    ProjectRecord,
    ProjectRegistry,
)
from session import session_runtime_uid
from utils.ui import format_session_title


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesktopNotificationTarget:
    session_id: str
    session_uid: str
    label: str
    workdir: str
    project_slug: str


class DesktopIdentityProvider:
    DESKTOP_OWNER_ID = DESKTOP_ACTOR_ID

    def __init__(
        self,
        *,
        project_registry: ProjectRegistry,
        session_service: Any,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._project_registry = project_registry
        self._session_service = session_service
        self._logger = logger_ or logging.getLogger(__name__)

    @property
    def owner_id(self) -> str:
        return desktop_actor_id(self.DESKTOP_OWNER_ID)

    def list_owned_projects(self) -> list[ProjectRecord]:
        self._sync_desktop_projects()
        return self._project_registry.list_projects(owner_id=self.owner_id, enabled_only=True)

    def require_owned_project(self, project_slug: str) -> ProjectRecord:
        slug = str(project_slug or "").strip()
        if not slug:
            raise ProjectOwnershipError("project_slug is required")
        for record in self.list_owned_projects():
            if str(record.slug) == slug:
                return record
        raise ProjectOwnershipError(f"desktop project is not owned by current actor: {slug}")

    def resolve_project_slug(self, session_uid: str) -> Optional[str]:
        session = self.resolve_session(session_uid)
        if session is None:
            return None
        record = self._ensure_session_project(session)
        return str(record.slug)

    def list_project_sessions(self, project_slug: str) -> list[Any]:
        project = self.require_owned_project(project_slug)
        sessions = []
        for session in list(self._session_service.list_desktop_sessions() or []):
            if self._is_session_within_project(session=session, project_path=project.path):
                sessions.append(session)
        sessions.sort(key=lambda item: str(getattr(item, "id", "") or ""))
        return sessions

    def list_notification_targets(self, project_slug: str) -> list[DesktopNotificationTarget]:
        project = self.require_owned_project(project_slug)
        out: list[DesktopNotificationTarget] = []
        for session in self.list_project_sessions(project.slug):
            session_id = str(getattr(session, "id", "") or "").strip()
            if not session_id:
                continue
            label = format_session_title(session)
            out.append(
                DesktopNotificationTarget(
                    session_id=session_id,
                    session_uid=session_runtime_uid(session),
                    label=label,
                    workdir=str(getattr(session, "workdir", "") or ""),
                    project_slug=str(project.slug),
                )
            )
        return out

    def require_notification_target(self, project_slug: str, session_uid: str) -> DesktopNotificationTarget:
        token = str(session_uid or "").strip()
        if not token:
            raise ProjectOwnershipError("notification_target.telegram_session_uid is required")
        for item in self.list_notification_targets(project_slug):
            if token in {item.session_uid, item.session_id}:
                return item
        raise ProjectOwnershipError(
            f"scheduler notification target is outside owned project: {project_slug}"
        )

    def resolve_session(self, session_uid: str) -> Optional[Any]:
        token = str(session_uid or "").strip()
        if not token:
            return None
        resolver = getattr(self._session_service, "get_session_by_uid", None)
        if callable(resolver):
            session = resolver(token)
            if session is not None:
                return session
        if token.startswith("desktop:"):
            token = token.rsplit(":", 1)[-1]
        for session in list(self._session_service.list_desktop_sessions() or []):
            if str(getattr(session, "id", "") or "").strip() == token:
                return session
        return None

    def resolve_mode_launch_actor_chat_id(self, session: Any) -> Optional[int]:
        scope = getattr(session, "conversation_scope", None)
        for candidate in (
            getattr(session, "mode_launch_actor_chat_id", None),
            getattr(session, "owner_chat_id", None),
            getattr(session, "telegram_chat_id", None),
            getattr(session, "chat_id", None),
            getattr(scope, "chat_id", None),
        ):
            resolved = self._positive_chat_id(candidate)
            if resolved is not None:
                return resolved
        return None

    def _sync_desktop_projects(self) -> None:
        for session in list(self._session_service.list_desktop_sessions() or []):
            try:
                self._ensure_session_project(session)
            except ProjectOwnershipError:
                self._logger.warning(
                    "desktop project sync skipped foreign owner session_id=%s workdir=%s",
                    getattr(session, "id", ""),
                    getattr(session, "workdir", ""),
                )
            except Exception:
                self._logger.exception(
                    "desktop project sync failed session_id=%s workdir=%s",
                    getattr(session, "id", ""),
                    getattr(session, "workdir", ""),
                )

    def _ensure_session_project(self, session: Any) -> ProjectRecord:
        workdir = str(getattr(session, "workdir", "") or "").strip()
        if not workdir:
            raise ProjectOwnershipError("desktop session workdir is required")
        normalized = os.path.realpath(workdir)
        name = str(getattr(session, "name", "") or "").strip() or os.path.basename(normalized) or normalized
        return self._project_registry.register_project(
            path=normalized,
            owner_id=self.owner_id,
            name=name,
        )

    @staticmethod
    def _is_session_within_project(*, session: Any, project_path: str) -> bool:
        session_workdir = str(getattr(session, "workdir", "") or "").strip()
        if not session_workdir or not project_path:
            return False
        try:
            session_path = os.path.realpath(session_workdir)
            root_path = os.path.realpath(str(project_path))
            return os.path.commonpath([session_path, root_path]) == root_path
        except Exception:
            return False

    @staticmethod
    def _positive_chat_id(value: Any) -> Optional[int]:
        try:
            resolved = int(value)
        except Exception:
            return None
        return resolved if resolved > 0 else None
