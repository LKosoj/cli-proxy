import types

import pytest

from app.services.menu_visibility_policy import build_mode_menu_visibility
from modes.analyst.ui import build_analyst_menu
from modes.agent.ui import build_agent_menu
from modes.manager.ui import build_manager_menu_with_back
from modes.webmaster.ui import build_webmaster_menu


def _has_mode_action(callbacks: list[str], prefix: str) -> bool:
    token = str(prefix or "")
    return any(str(item).startswith(token) for item in callbacks)


def _callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def _access_policy(*, is_admin: bool):
    return types.SimpleNamespace(
        is_admin=lambda _chat_id, scope="generic": is_admin,
        is_mode_allowed_for_chat=lambda _chat_id, mode_id: str(mode_id or "") == "direct_cli",
    )


def _session_for_mode(mode_id: str, *, chat_id: int = 1):
    session = types.SimpleNamespace(
        chat_id=chat_id,
        modes=types.SimpleNamespace(active_mode=mode_id),
    )
    if mode_id == "agent":
        session.id = "test_session"
        session.project_root = "/test"
        session.conversation_scope = types.SimpleNamespace(session_uid="uid", chat_id=chat_id)
    elif mode_id == "analyst":
        session.modes.analyst_template_id = "default"
    elif mode_id == "manager":
        session.modes.manager_quiet_mode = False
    return session


def _build_menu(mode_id: str, session, visibility):
    if mode_id == "agent":
        return build_agent_menu(session, back_callback="x", back_text="Back", menu_visibility=visibility)
    if mode_id == "analyst":
        return build_analyst_menu(session, back_callback="x", back_text="Back", menu_visibility=visibility)
    if mode_id == "manager":
        return build_manager_menu_with_back(
            session,
            back_callback="x",
            back_text="Back",
            plan_status="active",
            menu_visibility=visibility,
        )
    if mode_id == "webmaster":
        return build_webmaster_menu(session, back_callback="x", back_text="Back", menu_visibility=visibility)
    raise AssertionError(f"unknown mode: {mode_id}")


def test_analyst_menu_contains_run_operations():
    session = types.SimpleNamespace(modes=types.SimpleNamespace(active_mode="analyst", analyst_template_id="default"))
    _text, markup = build_analyst_menu(session, back_callback="x", back_text="Back")

    callbacks = []
    for row in markup.inline_keyboard:
        for btn in row:
            callbacks.append(btn.callback_data)

    assert _has_mode_action(callbacks, "ma:analyst:doctor")
    assert _has_mode_action(callbacks, "ma:analyst:recover")
    assert _has_mode_action(callbacks, "ma:analyst:resume")
    assert _has_mode_action(callbacks, "ma:analyst:promote_skills")


def test_agent_menu_contains_run_operations():
    session = types.SimpleNamespace(
        id="test_session",
        project_root="/test",
        conversation_scope=types.SimpleNamespace(session_uid="uid"),
        modes=types.SimpleNamespace(active_mode="agent")
    )
    _text, markup = build_agent_menu(session, back_callback="x", back_text="Back")

    callbacks = []
    for row in markup.inline_keyboard:
        for btn in row:
            callbacks.append(btn.callback_data)

    assert _has_mode_action(callbacks, "ma:agent:doctor")
    assert _has_mode_action(callbacks, "ma:agent:recover")
    assert _has_mode_action(callbacks, "ma:agent:resume")
    assert _has_mode_action(callbacks, "ma:agent:promote_skills")


def test_manager_menu_contains_run_operations():
    session = types.SimpleNamespace(modes=types.SimpleNamespace(active_mode="manager", manager_quiet_mode=False))
    _text, markup = build_manager_menu_with_back(session, back_callback="x", back_text="Back", plan_status="active")

    callbacks = []
    for row in markup.inline_keyboard:
        for btn in row:
            callbacks.append(btn.callback_data)

    assert _has_mode_action(callbacks, "ma:manager:doctor")
    assert _has_mode_action(callbacks, "ma:manager:recover")
    assert _has_mode_action(callbacks, "ma:manager:resume")
    assert _has_mode_action(callbacks, "ma:manager:promote_skills")


def test_webmaster_menu_contains_run_operations():
    session = types.SimpleNamespace(modes=types.SimpleNamespace(active_mode="webmaster"))
    _text, markup = build_webmaster_menu(session, back_callback="x", back_text="Back")

    callbacks = []
    for row in markup.inline_keyboard:
        for btn in row:
            callbacks.append(btn.callback_data)

    assert _has_mode_action(callbacks, "ma:webmaster:doctor")
    assert _has_mode_action(callbacks, "ma:webmaster:recover")
    assert _has_mode_action(callbacks, "ma:webmaster:resume")
    assert _has_mode_action(callbacks, "ma:webmaster:promote_skills")


def test_agent_menu_hides_advanced_actions_for_simple_user() -> None:
    session = types.SimpleNamespace(
        chat_id=1,
        id="test_session",
        project_root="/test",
        conversation_scope=types.SimpleNamespace(session_uid="uid"),
        modes=types.SimpleNamespace(active_mode="agent"),
    )
    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="agent",
        access_policy=types.SimpleNamespace(
            is_admin=lambda _chat_id, scope="generic": False,
            is_mode_allowed_for_chat=lambda _chat_id, mode_id: str(mode_id or "") == "direct_cli",
        ),
    )
    _text, markup = build_agent_menu(
        session,
        back_callback="x",
        back_text="Back",
        menu_visibility=visibility,
    )

    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    assert _has_mode_action(callbacks, "ma:agent:status")
    assert _has_mode_action(callbacks, "ma:agent:project_change")
    assert _has_mode_action(callbacks, "ma:agent:disable")
    assert _has_mode_action(callbacks, "ma:agent:doctor")
    assert not _has_mode_action(callbacks, "ma:agent:recover")
    assert not _has_mode_action(callbacks, "ma:agent:resume")
    assert not _has_mode_action(callbacks, "ma:agent:promote_skills")
    assert not _has_mode_action(callbacks, "ma:agent:plugins")
    assert not _has_mode_action(callbacks, "ma:agent:clean_all")
    assert not _has_mode_action(callbacks, "ma:agent:clean_session")


def test_analyst_menu_hides_template_and_run_operations_for_simple_user() -> None:
    session = types.SimpleNamespace(chat_id=1, modes=types.SimpleNamespace(active_mode="analyst", analyst_template_id="default"))
    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="analyst",
        access_policy=types.SimpleNamespace(
            is_admin=lambda _chat_id, scope="generic": False,
            is_mode_allowed_for_chat=lambda _chat_id, _mode_id: False,
        ),
    )
    _text, markup = build_analyst_menu(session, back_callback="x", back_text="Back", menu_visibility=visibility)

    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    assert "ma:analyst:status" in callbacks
    assert "ma:analyst:download" in callbacks
    assert "ma:analyst:audit" in callbacks
    assert _has_mode_action(callbacks, "ma:analyst:doctor")
    assert not _has_mode_action(callbacks, "ma:analyst:recover")
    assert not _has_mode_action(callbacks, "ma:analyst:resume")
    assert not _has_mode_action(callbacks, "ma:analyst:promote_skills")
    assert not _has_mode_action(callbacks, "ma:analyst:template")


def test_manager_menu_hides_quiet_and_run_operations_for_simple_user() -> None:
    session = types.SimpleNamespace(chat_id=1, modes=types.SimpleNamespace(active_mode="manager", manager_quiet_mode=False))
    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="manager",
        access_policy=types.SimpleNamespace(
            is_admin=lambda _chat_id, scope="generic": False,
            is_mode_allowed_for_chat=lambda _chat_id, _mode_id: False,
        ),
    )
    _text, markup = build_manager_menu_with_back(
        session,
        back_callback="x",
        back_text="Back",
        plan_status="active",
        menu_visibility=visibility,
    )

    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    assert "ma:manager:status" in callbacks
    assert "ma:manager:pause" in callbacks
    assert "ma:manager:reset" in callbacks
    assert not _has_mode_action(callbacks, "ma:manager:quiet_toggle")
    assert _has_mode_action(callbacks, "ma:manager:doctor")
    assert not _has_mode_action(callbacks, "ma:manager:recover")
    assert not _has_mode_action(callbacks, "ma:manager:resume")
    assert not _has_mode_action(callbacks, "ma:manager:promote_skills")


def test_webmaster_menu_hides_run_operations_for_simple_user() -> None:
    session = types.SimpleNamespace(chat_id=1, modes=types.SimpleNamespace(active_mode="webmaster"))
    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="webmaster",
        access_policy=types.SimpleNamespace(
            is_admin=lambda _chat_id, scope="generic": False,
            is_mode_allowed_for_chat=lambda _chat_id, _mode_id: False,
        ),
    )
    _text, markup = build_webmaster_menu(session, back_callback="x", back_text="Back", menu_visibility=visibility)

    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    assert "ma:webmaster:status" in callbacks
    assert "ma:webmaster:reset" in callbacks
    assert _has_mode_action(callbacks, "ma:webmaster:doctor")
    assert not _has_mode_action(callbacks, "ma:webmaster:recover")
    assert not _has_mode_action(callbacks, "ma:webmaster:resume")
    assert not _has_mode_action(callbacks, "ma:webmaster:promote_skills")


@pytest.mark.parametrize("mode_id", ["agent", "analyst", "manager", "webmaster"])
def test_mode_menu_visibility_applies_run_operations_policy_for_owner_non_admin(mode_id: str) -> None:
    session = _session_for_mode(mode_id, chat_id=42)
    visibility = build_mode_menu_visibility(
        session=session,
        mode_id=mode_id,
        access_policy=_access_policy(is_admin=False),
    )
    _text, markup = _build_menu(mode_id, session, visibility)

    callbacks = _callbacks(markup)

    assert visibility.allows("doctor")
    assert not visibility.allows("recover")
    assert not visibility.allows("resume")
    assert not visibility.allows("apply_recommendation")
    assert not visibility.allows("promote_skills")
    assert _has_mode_action(callbacks, f"ma:{mode_id}:doctor")
    assert not _has_mode_action(callbacks, f"ma:{mode_id}:recover")
    assert not _has_mode_action(callbacks, f"ma:{mode_id}:resume")
    assert not _has_mode_action(callbacks, f"ma:{mode_id}:promote_skills")


@pytest.mark.parametrize("mode_id", ["agent", "analyst", "manager", "webmaster"])
def test_mode_menu_visibility_applies_run_operations_policy_for_admin(mode_id: str) -> None:
    session = _session_for_mode(mode_id, chat_id=42)
    visibility = build_mode_menu_visibility(
        session=session,
        mode_id=mode_id,
        access_policy=_access_policy(is_admin=True),
    )
    _text, markup = _build_menu(mode_id, session, visibility)

    callbacks = _callbacks(markup)

    assert visibility.allows("doctor")
    assert visibility.allows("recover")
    assert visibility.allows("resume")
    assert visibility.allows("apply_recommendation")
    assert visibility.allows("promote_skills")
    assert _has_mode_action(callbacks, f"ma:{mode_id}:doctor")
    assert _has_mode_action(callbacks, f"ma:{mode_id}:recover")
    assert _has_mode_action(callbacks, f"ma:{mode_id}:resume")
    assert _has_mode_action(callbacks, f"ma:{mode_id}:promote_skills")
