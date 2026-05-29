from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


RunOperationVisibility = Literal["show", "disable", "hide"]

_KNOWN_SURFACES = frozenset({"telegram", "miniapp", "desktop"})
_ADMIN_ONLY_OPERATIONS = frozenset({"recover", "resume", "apply_recommendation", "promote_skills"})
_OWNER_OR_ADMIN_OPERATIONS = frozenset({"doctor"})
_KNOWN_OPERATIONS = _OWNER_OR_ADMIN_OPERATIONS | _ADMIN_ONLY_OPERATIONS


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    visibility: RunOperationVisibility


class RunOperationsPolicy:
    def can_run_operation(
        self,
        *,
        operation: str,
        user_id: int | str,
        is_admin: bool,
        session: Any,
        surface: str,
    ) -> PolicyDecision:
        op = str(operation or "").strip().lower()
        resolved_surface = str(surface or "").strip().lower()
        if resolved_surface not in _KNOWN_SURFACES:
            return PolicyDecision(
                allowed=False,
                reason="unknown_surface",
                visibility="hide",
            )
        if op not in _KNOWN_OPERATIONS:
            return PolicyDecision(
                allowed=False,
                reason="unknown_operation",
                visibility="hide",
            )
        if bool(is_admin):
            return PolicyDecision(
                allowed=True,
                reason="admin_allowed",
                visibility="show",
            )
        if op in _ADMIN_ONLY_OPERATIONS:
            return PolicyDecision(
                allowed=False,
                reason="admin_required",
                visibility="hide",
            )
        if self._is_session_owner(user_id=user_id, session=session):
            return PolicyDecision(
                allowed=True,
                reason="owner_allowed",
                visibility="show",
            )
        return PolicyDecision(
            allowed=False,
            reason="owner_or_admin_required",
            visibility="hide",
        )

    @classmethod
    def _is_session_owner(cls, *, user_id: int | str, session: Any) -> bool:
        user_token = cls._identity_token(user_id)
        if not user_token:
            return False
        return user_token in cls._session_owner_tokens(session)

    @classmethod
    def _session_owner_tokens(cls, session: Any) -> frozenset[str]:
        scope = getattr(session, "conversation_scope", None)
        candidates = (
            getattr(session, "owner_chat_id", None),
            getattr(session, "mode_launch_actor_chat_id", None),
            getattr(session, "telegram_chat_id", None),
            getattr(session, "chat_id", None),
            getattr(scope, "owner_chat_id", None),
            getattr(scope, "chat_id", None),
        )
        return frozenset(
            token
            for token in (cls._identity_token(candidate) for candidate in candidates)
            if token
        )

    @staticmethod
    def _identity_token(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()
