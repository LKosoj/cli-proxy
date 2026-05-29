"""Centralized access policy checks and denial texts."""

from __future__ import annotations

from typing import Any, List, Optional

from app.security.interfaces import AuthDecision
from app.services.advanced_orchestrator_service import (
    DIRECT_CLI_MODE_ID as ORCHESTRATOR_DIRECT_CLI_MODE_ID,
    ORCHESTRATOR_MODE_ID as VIRTUAL_ORCHESTRATOR_MODE_ID,
)
from sessions.session_state_access import get_active_mode, set_active_mode


class AccessPolicyService:
    DIRECT_CLI_MODE_ID = ORCHESTRATOR_DIRECT_CLI_MODE_ID
    ORCHESTRATOR_MODE_ID = VIRTUAL_ORCHESTRATOR_MODE_ID
    DIRECT_CLI_DENIED_TEXT = "Прямой CLI недоступен для вашего пользователя."

    _ADMIN_DENIED_BY_SCOPE = {
        "new_projects": "Создание новых проектов ограничено администратором.",
        "git": "Доступ к /git ограничен администратором.",
        "files": "Доступ к /files ограничен администратором.",
        "miniapp": "MiniApp доступен только администраторам.",
        "global_skills": "Продвижение skills в global registry ограничено администратором.",
        "generic": "Доступ ограничен администратором.",
    }

    SESSION_REQUIRED_TEXT = "Сессия для текущего контекста не выбрана."

    def __init__(self, bot_app):
        self.bot_app = bot_app

    def authorize(
        self,
        chat_id: int,
        *,
        scope: str = "generic",
        require_admin: bool = False,
    ) -> AuthDecision:
        subject_chat_id = int(chat_id)
        security = getattr(self.bot_app, "security", None)
        if security is None or not hasattr(security, "authorize"):
            raise RuntimeError("SecurityFacade.authorize is not configured for AccessPolicyService")
        return security.authorize(
            subject_chat_id,
            scope=str(scope or "generic"),
            require_admin=bool(require_admin),
        )

    def is_allowed(self, chat_id: int, *, scope: str = "generic") -> bool:
        return bool(self.authorize(int(chat_id), scope=scope).allowed)

    def is_admin(self, chat_id: int, *, scope: str = "generic") -> bool:
        return bool(self.authorize(int(chat_id), scope=scope, require_admin=True).allowed)

    def is_user(self, chat_id: int, *, scope: str = "generic") -> bool:
        decision = self.authorize(int(chat_id), scope=scope)
        return bool(decision.is_user)

    def is_whitelisted(self, chat_id: int) -> bool:
        return int(chat_id) in set(getattr(self.bot_app.config.telegram, "whitelist_chat_ids", []) or [])

    async def ensure_allowed(self, chat_id: int, context, *, scope: str = "generic") -> bool:
        subject_chat_id = int(chat_id)
        decision = self.authorize(subject_chat_id, scope=scope)
        if decision.allowed:
            return True
        if self.is_whitelisted(subject_chat_id):
            await self.bot_app._send_message(
                context,
                chat_id=subject_chat_id,
                text="Доступ не настроен. Обратитесь к администратору.",
            )
        return False

    def admin_denied_text(self, scope: str = "generic") -> str:
        key = str(scope or "generic").strip().lower()
        return self._ADMIN_DENIED_BY_SCOPE.get(key, self._ADMIN_DENIED_BY_SCOPE["generic"])

    async def require_admin(self, chat_id: int, context, *, scope: str = "generic") -> bool:
        if self.is_admin(int(chat_id), scope=scope):
            return True
        await self.bot_app._send_message(context, chat_id=int(chat_id), text=self.admin_denied_text(scope))
        return False

    def can_input_project_path(self, chat_id: int, *, mode_id: str = "", flow: str = "") -> bool:
        if self.is_admin(int(chat_id), scope="new_projects"):
            return True
        return bool(str(mode_id or "").strip() and str(flow or "").strip())

    def callback_admin_scope(self, chat_id: int, data: str, *, mode_id: str = "", flow: str = "") -> str:
        if self.is_admin(int(chat_id)):
            return ""
        token = str(data or "")
        parts = [part.strip().lower() for part in token.split(":") if part.strip()]
        if parts and parts[0] in ("ma", "mode_action") and "promote_skills" in parts:
            return "global_skills"
        if token.startswith("file_") or token.startswith("file_nav:") or token.startswith("file_pick:"):
            return "files"
        if token.startswith("git_") or token.startswith("gitpull_") or token.startswith("git_conflict"):
            return "git"
        if token == "dir_git_clone":
            return "git"
        if token in {"dir_enter", "dir_create"} or token.startswith("dir_create:"):
            return "new_projects"
        if token.startswith("dir_") or token.startswith("dir_pick:") or token.startswith("dir_page:"):
            if not (str(mode_id or "").strip() and str(flow or "").strip()):
                return "new_projects"
        return ""

    async def require_scope_session(
        self,
        chat_id: int,
        context,
        *,
        auto_create: bool = False,
        reply_chat_id: int | None = None,
        message_thread_id: int | None = None,
    ):
        if auto_create:
            session = await self.bot_app.ensure_scope_session(
                int(chat_id),
                context,
                reply_chat_id=reply_chat_id,
                message_thread_id=message_thread_id,
            )
        else:
            resolver = getattr(self.bot_app, "resolve_telegram_scope_session", None)
            session = (
                resolver(
                    reply_chat_id=int(reply_chat_id if reply_chat_id is not None else chat_id),
                    message_thread_id=message_thread_id,
                    owner_chat_id=int(chat_id),
                )
                if callable(resolver)
                else None
            )
        if session:
            return session
        await self.bot_app._send_message(context, chat_id=int(chat_id), text=self.SESSION_REQUIRED_TEXT)
        return None

    def user_modes(self, chat_id: int):
        return (getattr(self.bot_app.config.telegram, "user_modes", {}) or {}).get(int(chat_id))

    def _available_mode_ids(self) -> List[str]:
        svc = getattr(self.bot_app, "mode_registry_service", None)
        all_modes = [str(mid) for mid, _ in (svc.list_modes() if svc and hasattr(svc, "list_modes") else [])]
        for virtual_mode_id in (self.DIRECT_CLI_MODE_ID, self.ORCHESTRATOR_MODE_ID):
            if virtual_mode_id not in all_modes:
                all_modes.append(virtual_mode_id)
        return all_modes

    def allowed_mode_ids_for_chat(self, chat_id: int) -> List[str]:
        chat_id = int(chat_id)
        all_modes = self._available_mode_ids()
        decision = self.authorize(chat_id)
        if decision.is_admin:
            return all_modes
        if not decision.is_user:
            return []
        raw = self.user_modes(chat_id)
        if raw is None:
            return []
        if isinstance(raw, str):
            if str(raw).strip().lower() == "all":
                return all_modes
            raw = [str(raw).strip()]
        if not isinstance(raw, list):
            return []
        allowed = {str(x).strip() for x in raw if str(x).strip()}
        return [mid for mid in all_modes if mid in allowed]

    def is_mode_allowed_for_chat(self, chat_id: int, mode_id: str) -> bool:
        mid = str(mode_id or "").strip()
        if not mid:
            return False
        return mid in set(self.allowed_mode_ids_for_chat(int(chat_id)))

    def is_direct_cli_allowed_for_chat(self, chat_id: int) -> bool:
        return self.is_mode_allowed_for_chat(int(chat_id), self.DIRECT_CLI_MODE_ID)

    def is_orchestrator_allowed_for_chat(self, chat_id: int) -> bool:
        return self.is_mode_allowed_for_chat(int(chat_id), self.ORCHESTRATOR_MODE_ID)

    def default_mode_id_for_chat(self, chat_id: Any) -> Optional[str]:
        try:
            resolved_chat_id = int(chat_id)
        except Exception:
            return None
        if resolved_chat_id <= 0:
            return None
        allowed_mode_ids = [
            str(mode_id or "").strip()
            for mode_id in self.allowed_mode_ids_for_chat(resolved_chat_id)
            if str(mode_id or "").strip()
        ]
        if self.DIRECT_CLI_MODE_ID in allowed_mode_ids:
            return None
        for mode_id in allowed_mode_ids:
            if mode_id != self.ORCHESTRATOR_MODE_ID:
                return mode_id
        return None

    def apply_default_mode_for_session(self, session: Any, *, chat_id: Any = None) -> Optional[str]:
        if session is None:
            return None
        if str(get_active_mode(session, "") or "").strip():
            return None
        default_mode_id = self.default_mode_id_for_chat(
            getattr(session, "chat_id", None) if chat_id is None else chat_id
        )
        if not default_mode_id:
            return None
        set_active_mode(session, default_mode_id)
        return default_mode_id
