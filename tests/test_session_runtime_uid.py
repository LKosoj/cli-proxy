from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager, session_runtime_uid, session_scoped_key
from sessions.conversation_scope import DesktopScope


def _build_config(tmp_path, *, intent: str = "runtime_uid") -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"])},
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_session_runtime_uid_prefers_scope_alias_over_raw_session_id() -> None:
    session = SimpleNamespace(
        id="legacy-s1",
        scope=SimpleNamespace(session_uid="thread:1:42"),
        conversation_scope=SimpleNamespace(session_uid="chat:1"),
    )

    assert session_runtime_uid(session) == "thread:1:42"


def test_session_runtime_uid_makes_plain_chat_sessions_unique() -> None:
    session = SimpleNamespace(
        id="plain-s1",
        conversation_scope=SimpleNamespace(session_uid="chat:1"),
    )

    assert session_runtime_uid(session) == "chat:1:plain-s1"


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (SimpleNamespace(id="legacy-no-scope"), "desktop:legacy-no-scope"),
        (SimpleNamespace(id="legacy-empty-scope", scope=SimpleNamespace(session_uid="")), "desktop:legacy-empty-scope"),
        (
            SimpleNamespace(
                id="legacy-empty-conversation-scope",
                conversation_scope=SimpleNamespace(session_uid=""),
            ),
            "desktop:legacy-empty-conversation-scope",
        ),
    ],
    ids=["missing-scope", "empty-scope-alias", "empty-conversation-scope"],
)
def test_session_runtime_uid_uses_desktop_fallback_for_fake_simple_namespace(session, expected: str) -> None:
    assert session_runtime_uid(session) == expected


@pytest.mark.parametrize(
    "fake_session",
    [
        SimpleNamespace(id="fake-no-scope"),
        SimpleNamespace(id="fake-empty-scope", scope=SimpleNamespace(session_uid="")),
        SimpleNamespace(id="fake-empty-conversation-scope", conversation_scope=SimpleNamespace(session_uid="")),
    ],
    ids=["missing-scope", "empty-scope-alias", "empty-conversation-scope"],
)
def test_session_manager_get_by_uid_resolves_canonical_desktop_fallback_for_fake_simple_namespace(
    tmp_path,
    fake_session,
) -> None:
    manager = SessionManager(_build_config(tmp_path))
    manager._ensure_chat(1)
    manager.sessions_by_chat[1][fake_session.id] = fake_session

    assert manager.get_by_uid(session_runtime_uid(fake_session)) is fake_session


@pytest.mark.parametrize(
    ("fake_session", "expected"),
    [
        (SimpleNamespace(id="s1", chat_id=1), "1_s1"),
        (SimpleNamespace(id="s1", chat_id=2), "2_s1"),
        (SimpleNamespace(id="s1", conversation_scope=DesktopScope("desktop", "s1")), "0_s1"),
        (SimpleNamespace(id="s1", chat_id=1, scoped_key="custom/key"), "custom_key"),
    ],
    ids=["derived-chat1", "derived-chat2", "desktop-scope", "explicit-custom"],
)
def test_session_scoped_key_supports_fake_simple_namespace_sessions(fake_session, expected: str) -> None:
    assert session_scoped_key(fake_session) == expected


def test_session_manager_get_by_uid_rejects_ambiguous_plain_chat_scope_uid(tmp_path) -> None:
    manager = SessionManager(_build_config(tmp_path))
    manager._ensure_chat(1)
    session_a = SimpleNamespace(id="s1", conversation_scope=SimpleNamespace(session_uid="chat:1"))
    session_b = SimpleNamespace(id="s2", conversation_scope=SimpleNamespace(session_uid="chat:1"))
    manager.sessions_by_chat[1]["s1"] = session_a
    manager.sessions_by_chat[1]["s2"] = session_b

    assert session_runtime_uid(session_a) == "chat:1:s1"
    assert session_runtime_uid(session_b) == "chat:1:s2"
    assert manager.get_by_uid("chat:1:s1") is session_a
    assert manager.get_by_uid("chat:1:s2") is session_b
    assert manager.get_by_uid("chat:1") is None
