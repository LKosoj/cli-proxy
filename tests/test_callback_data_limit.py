"""Ensure callback_data never exceeds Telegram's 64-byte limit."""

import types

from modes.sdk.services.callback_data import (
    _TG_CALLBACK_DATA_MAX_BYTES,
    build_mode_action_callback_data,
)


def _make_session(session_id: str = "s1"):
    return types.SimpleNamespace(id=session_id)


def test_uses_short_prefix():
    result = build_mode_action_callback_data("analyst", "enable")
    assert result == "ma:analyst:enable"


def test_includes_short_session_id():
    session = _make_session("s1")
    result = build_mode_action_callback_data("analyst", "enable", session=session)
    assert result == "ma:analyst:enable:s=s1"


def test_payload_uses_val_key():
    result = build_mode_action_callback_data("analyst", "template", payload="default")
    assert "val=default" in result


def test_with_session_and_payload_fits():
    session = _make_session("s1")
    result = build_mode_action_callback_data(
        "analyst", "template", session=session, payload="default"
    )
    assert result == "ma:analyst:template:s=s1|val=default"
    assert len(result.encode("utf-8")) <= _TG_CALLBACK_DATA_MAX_BYTES


def test_longest_action_fits():
    session = _make_session("s99")
    result = build_mode_action_callback_data("analyst", "promote_skills", session=session)
    assert len(result.encode("utf-8")) <= _TG_CALLBACK_DATA_MAX_BYTES


def test_no_full_session_uid_in_callback():
    """Session UID (chat:123:s1) must NOT appear — only short session_id (s1)."""
    session = types.SimpleNamespace(
        id="s1",
        scope=types.SimpleNamespace(session_uid="chat:1234567890"),
    )
    result = build_mode_action_callback_data("analyst", "enable", session=session)
    assert "chat:" not in result
    assert "s=s1" in result
