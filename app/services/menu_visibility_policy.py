from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Optional

from app.services.advanced_orchestrator_service import DIRECT_CLI_MODE_ID, ORCHESTRATOR_MODE_ID
from app.services.run_operations_policy import RunOperationsPolicy
from sessions.session_state_access import get_active_mode

_ADMIN_SESSION_ACTIONS = frozenset(
    {
        "cli_selector",
        "status",
        "rename",
        "resume",
        "queue",
        "state",
        "snapshot_report",
        "close",
        "reset",
        "ssh",
        "orchestrator",
        "mode_selector",
        "new_session",
        "list_sessions",
    }
)

_USER_SESSION_ACTIONS = frozenset({"status", "snapshot_report", "reset", "new_session"})

_ADMIN_MODE_ACTIONS = {
    "agent": frozenset(
        {
            "enable",
            "disable",
            "status",
            "doctor",
            "recover",
            "resume",
            "promote_skills",
            "project_connect",
            "project_change",
            "project_disconnect",
            "plugins",
            "clean_all",
            "clean_session",
        }
    ),
    "analyst": frozenset(
        {
            "enable",
            "disable",
            "status",
            "doctor",
            "recover",
            "resume",
            "promote_skills",
            "download",
            "audit",
            "template",
        }
    ),
    "manager": frozenset(
        {
            "enable",
            "disable",
            "quiet_toggle",
            "status",
            "doctor",
            "recover",
            "resume",
            "promote_skills",
            "pause",
            "resume_paused",
            "reset",
        }
    ),
    "webmaster": frozenset(
        {
            "enable",
            "disable",
            "status",
            "doctor",
            "recover",
            "resume",
            "promote_skills",
            "reset",
        }
    ),
}

_USER_MODE_ACTIONS = {
    "agent": frozenset({"enable", "status", "project_connect", "project_change"}),
    "analyst": frozenset({"enable", "status", "download", "audit"}),
    "manager": frozenset({"enable", "status", "pause", "resume_paused", "reset"}),
    "webmaster": frozenset({"enable", "status", "reset"}),
}

_RUN_OPERATION_ACTIONS = frozenset(
    {
        "doctor",
        "recover",
        "resume",
        "apply_recommendation",
        "promote_skills",
    }
)
_RUN_OPERATIONS_POLICY = RunOperationsPolicy()


@dataclass(frozen=True)
class SessionOverviewVisibility:
    actions: FrozenSet[str]

    def allows(self, action: str) -> bool:
        return str(action or "").strip() in self.actions


@dataclass(frozen=True)
class ModeMenuVisibility:
    actions: FrozenSet[str]

    def allows(self, action: str) -> bool:
        return str(action or "").strip() in self.actions


def _safe_is_admin(*, access_policy: Any, chat_id: Any) -> bool:
    checker = getattr(access_policy, "is_admin", None) if access_policy is not None else None
    if not callable(checker):
        return False
    try:
        return bool(checker(int(chat_id), scope="generic"))
    except TypeError:
        return bool(checker(int(chat_id)))
    except Exception:
        return False


def _safe_is_direct_cli_allowed(*, access_policy: Any, chat_id: Any) -> bool:
    if access_policy is None:
        return False
    direct_checker = getattr(access_policy, "is_direct_cli_allowed_for_chat", None)
    if callable(direct_checker):
        try:
            return bool(direct_checker(int(chat_id)))
        except Exception:
            return False
    mode_checker = getattr(access_policy, "is_mode_allowed_for_chat", None)
    if callable(mode_checker):
        try:
            return bool(mode_checker(int(chat_id), DIRECT_CLI_MODE_ID))
        except Exception:
            return False
    return False


def _safe_is_orchestrator_allowed(*, access_policy: Any, chat_id: Any) -> bool:
    if access_policy is None:
        return False
    orch_checker = getattr(access_policy, "is_orchestrator_allowed_for_chat", None)
    if callable(orch_checker):
        try:
            return bool(orch_checker(int(chat_id)))
        except Exception:
            return False
    mode_checker = getattr(access_policy, "is_mode_allowed_for_chat", None)
    if callable(mode_checker):
        try:
            return bool(mode_checker(int(chat_id), ORCHESTRATOR_MODE_ID))
        except Exception:
            return False
    return False


def _with_run_operations_policy(
    *,
    mode_id: str,
    actions: set[str],
    session: Any,
    user_id: Any,
    is_admin: bool,
) -> set[str]:
    resolved_actions = set(actions)
    resolved_actions.difference_update(_RUN_OPERATION_ACTIONS)
    supported_operations = set(_ADMIN_MODE_ACTIONS.get(mode_id, frozenset())) & set(_RUN_OPERATION_ACTIONS)
    if mode_id in _ADMIN_MODE_ACTIONS:
        supported_operations.add("apply_recommendation")
    for operation in supported_operations:
        decision = _RUN_OPERATIONS_POLICY.can_run_operation(
            operation=operation,
            user_id=user_id,
            is_admin=is_admin,
            session=session,
            surface="telegram",
        )
        if decision.allowed and decision.visibility == "show":
            resolved_actions.add(operation)
    return resolved_actions


def build_session_overview_visibility(
    *,
    session: Any,
    chat_id: Any,
    access_policy: Any,
    available_tool_count: int,
    registered_mode_count: int,
    visible_session_count: int,
) -> SessionOverviewVisibility:
    resolved_chat_id = int(chat_id)
    is_admin = _safe_is_admin(access_policy=access_policy, chat_id=resolved_chat_id)
    active_mode = str(get_active_mode(session, "") or "").strip()
    queue_len = len(list(getattr(session, "queue", []) or []))
    actions = set(_ADMIN_SESSION_ACTIONS if is_admin else _USER_SESSION_ACTIONS)

    if not is_admin:
        if visible_session_count > 1:
            actions.add("list_sessions")
        if not active_mode and int(available_tool_count) > 1:
            actions.add("cli_selector")
        if _safe_is_orchestrator_allowed(access_policy=access_policy, chat_id=resolved_chat_id):
            actions.add("orchestrator")
        if int(registered_mode_count) > 1 or (not active_mode and int(registered_mode_count) > 0):
            actions.add("mode_selector")
        return SessionOverviewVisibility(actions=frozenset(actions))

    if queue_len <= 0:
        actions.discard("queue")
    if int(available_tool_count) <= 0:
        actions.discard("cli_selector")
    if int(registered_mode_count) <= 0:
        actions.discard("mode_selector")
    return SessionOverviewVisibility(actions=frozenset(actions))


def build_mode_menu_visibility(
    *,
    session: Any,
    mode_id: str,
    access_policy: Any,
    user_id: Any = None,
) -> ModeMenuVisibility:
    mid = str(mode_id or "").strip()
    chat_id = getattr(session, "chat_id", None)
    actor_id = chat_id if user_id is None else user_id
    is_admin = _safe_is_admin(access_policy=access_policy, chat_id=chat_id) if chat_id is not None else False
    if is_admin:
        actions = set(_ADMIN_MODE_ACTIONS.get(mid, frozenset({"enable", "disable", "status"})))
        actions = _with_run_operations_policy(
            mode_id=mid,
            actions=actions,
            session=session,
            user_id=actor_id,
            is_admin=True,
        )
        return ModeMenuVisibility(actions=frozenset(actions))

    actions = set(_USER_MODE_ACTIONS.get(mid, frozenset({"enable"})))
    if (
        str(get_active_mode(session, "") or "").strip() == mid
        and chat_id is not None
        and _safe_is_direct_cli_allowed(access_policy=access_policy, chat_id=chat_id)
    ):
        actions.add("disable")
    actions = _with_run_operations_policy(
        mode_id=mid,
        actions=actions,
        session=session,
        user_id=actor_id,
        is_admin=False,
    )
    return ModeMenuVisibility(actions=frozenset(actions))


def call_mode_build_menu(
    plugin: Any,
    session: Any,
    *,
    back_callback: str,
    back_text: str,
    menu_visibility: Optional[ModeMenuVisibility] = None,
):
    if menu_visibility is None:
        return plugin.build_menu(session, back_callback=back_callback, back_text=back_text)
    try:
        return plugin.build_menu(
            session,
            back_callback=back_callback,
            back_text=back_text,
            menu_visibility=menu_visibility,
        )
    except TypeError as exc:
        if "menu_visibility" not in str(exc):
            raise
        return plugin.build_menu(session, back_callback=back_callback, back_text=back_text)
