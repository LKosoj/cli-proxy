import types

from app.services.menu_visibility_policy import build_mode_menu_visibility, build_session_overview_visibility


def _access_policy(*, is_admin: bool = False, direct_cli: bool = False):
    return types.SimpleNamespace(
        is_admin=lambda _chat_id, scope="generic": bool(is_admin),
        is_mode_allowed_for_chat=lambda _chat_id, mode_id: bool(direct_cli) if str(mode_id or "") == "direct_cli" else True,
    )


def test_session_overview_visibility_for_simple_user_active_mode_is_basic_only() -> None:
    session = types.SimpleNamespace(chat_id=1, queue=[], modes=types.SimpleNamespace(active_mode="agent"))

    visibility = build_session_overview_visibility(
        session=session,
        chat_id=1,
        access_policy=_access_policy(is_admin=False, direct_cli=False),
        available_tool_count=2,
        registered_mode_count=1,
        visible_session_count=1,
    )

    assert visibility.allows("status") is True
    assert visibility.allows("reset") is True
    assert visibility.allows("new_session") is True
    assert visibility.allows("mode_selector") is False
    assert visibility.allows("cli_selector") is False
    assert visibility.allows("rename") is False
    assert visibility.allows("resume") is False
    assert visibility.allows("queue") is False
    assert visibility.allows("state") is False
    assert visibility.allows("close") is False


def test_session_overview_visibility_for_direct_cli_user_shows_cli_and_mode_switchers() -> None:
    session = types.SimpleNamespace(chat_id=1, queue=[], modes=types.SimpleNamespace(active_mode=None))

    visibility = build_session_overview_visibility(
        session=session,
        chat_id=1,
        access_policy=_access_policy(is_admin=False, direct_cli=True),
        available_tool_count=2,
        registered_mode_count=1,
        visible_session_count=1,
    )

    assert visibility.allows("cli_selector") is True
    assert visibility.allows("mode_selector") is True


def test_session_overview_visibility_for_admin_keeps_operational_actions() -> None:
    session = types.SimpleNamespace(chat_id=1, queue=[{"text": "queued"}], modes=types.SimpleNamespace(active_mode="agent"))

    visibility = build_session_overview_visibility(
        session=session,
        chat_id=1,
        access_policy=_access_policy(is_admin=True, direct_cli=True),
        available_tool_count=1,
        registered_mode_count=2,
        visible_session_count=1,
    )

    assert visibility.allows("rename") is True
    assert visibility.allows("resume") is True
    assert visibility.allows("queue") is True
    assert visibility.allows("state") is True
    assert visibility.allows("close") is True
    assert visibility.allows("orchestrator") is True
    assert visibility.allows("mode_selector") is True
    assert visibility.allows("list_sessions") is True


def test_session_overview_tmux_reread_is_admin_only_and_tmux_only() -> None:
    session = types.SimpleNamespace(chat_id=1, queue=[], modes=types.SimpleNamespace(active_mode="agent"))

    def _visibility(*, is_admin: bool, tmux: bool):
        return build_session_overview_visibility(
            session=session,
            chat_id=1,
            access_policy=_access_policy(is_admin=is_admin, direct_cli=True),
            available_tool_count=1,
            registered_mode_count=1,
            visible_session_count=1,
            tmux_backend_active=tmux,
        )

    assert _visibility(is_admin=True, tmux=True).allows("tmux_reread") is True
    assert _visibility(is_admin=True, tmux=False).allows("tmux_reread") is False
    assert _visibility(is_admin=False, tmux=True).allows("tmux_reread") is False


def test_mode_menu_visibility_for_simple_user_hides_advanced_actions() -> None:
    session = types.SimpleNamespace(chat_id=1, modes=types.SimpleNamespace(active_mode="agent"))

    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="agent",
        access_policy=_access_policy(is_admin=False, direct_cli=False),
    )

    assert visibility.allows("status") is True
    assert visibility.allows("project_change") is True
    assert visibility.allows("disable") is False
    assert visibility.allows("doctor") is True
    assert visibility.allows("plugins") is False
    assert visibility.allows("clean_all") is False


def test_mode_menu_visibility_hides_doctor_for_non_owner_non_admin() -> None:
    session = types.SimpleNamespace(chat_id=1, modes=types.SimpleNamespace(active_mode="agent"))

    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="agent",
        access_policy=_access_policy(is_admin=False, direct_cli=False),
        user_id=2,
    )

    assert visibility.allows("status") is True
    assert visibility.allows("doctor") is False
    assert visibility.allows("recover") is False


def test_mode_menu_visibility_keeps_disable_when_direct_cli_is_available() -> None:
    session = types.SimpleNamespace(chat_id=1, modes=types.SimpleNamespace(active_mode="agent"))

    visibility = build_mode_menu_visibility(
        session=session,
        mode_id="agent",
        access_policy=_access_policy(is_admin=False, direct_cli=True),
    )

    assert visibility.allows("disable") is True
