from __future__ import annotations

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import (
    SessionManager,
    available_execution_backends,
    get_session_execution_backend,
    set_session_execution_backend,
)


def _cfg(tmp_path, *, default_backend: str = "headless") -> AppConfig:
    tools = {
        "claude": ToolConfig(
            name="claude",
            mode="headless",
            cmd=["claude", "-p", "{prompt}"],
            interactive_cmd=["claude"],
            execution_backends=["headless", "tmux"],
            default_execution_backend=None,
        ),
        "codex": ToolConfig(
            name="codex",
            mode="headless",
            cmd=["codex", "exec", "{prompt}"],
        ),
    }
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_cli="claude",
            default_execution_backend=default_backend,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_execution_backend_resolves_global_default_when_supported(tmp_path) -> None:
    manager = SessionManager(_cfg(tmp_path, default_backend="tmux"))
    session = manager.create(1, "claude", str(tmp_path))

    assert available_execution_backends(session) == ["headless", "tmux"]
    assert get_session_execution_backend(session) == "tmux"


def test_execution_backend_falls_back_to_legacy_headless_when_default_not_supported(tmp_path) -> None:
    manager = SessionManager(_cfg(tmp_path, default_backend="tmux"))
    session = manager.create(1, "codex", str(tmp_path))

    assert available_execution_backends(session) == ["headless"]
    assert get_session_execution_backend(session) == "headless"


def test_set_session_execution_backend_is_settings_only_and_not_persisted(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    manager = SessionManager(cfg)
    first = manager.create(1, "claude", str(tmp_path))
    second = manager.create(1, "claude", str(tmp_path))

    result = set_session_execution_backend(first, "tmux")
    manager.persist_session(1, first.id)

    assert result.switched is False
    assert result.reason == "execution backend is configured in settings"
    assert get_session_execution_backend(first) == "headless"
    assert get_session_execution_backend(second) == "headless"

    restored_manager = SessionManager(cfg)
    restored_first = restored_manager.get(1, first.id)
    restored_second = restored_manager.get(1, second.id)

    assert restored_first is not None
    assert restored_second is not None
    assert get_session_execution_backend(restored_first) == "headless"
    assert get_session_execution_backend(restored_second) == "headless"


def test_restored_legacy_session_without_backend_uses_current_settings(tmp_path) -> None:
    cfg = _cfg(tmp_path, default_backend="tmux")
    manager = SessionManager(cfg)
    manager._state_repo.save_sessions_by_chat(
        {
            "1": {
                "sessions": {
                    "s1": {
                        "workdir": str(tmp_path),
                        "cli": {
                            "active_cli": "claude",
                            "resume_tokens": {"claude": "old-token"},
                        },
                    }
                },
                "counter": 1,
            }
        }
    )

    restored_manager = SessionManager(cfg)
    restored = restored_manager.get(1, "s1")

    assert restored is not None
    assert get_session_execution_backend(restored) == "tmux"


def test_set_session_execution_backend_rejects_non_noop_as_settings_only(tmp_path) -> None:
    manager = SessionManager(_cfg(tmp_path))
    busy_session = manager.create(1, "claude", str(tmp_path))
    queued_session = manager.create(1, "claude", str(tmp_path))

    busy_session.busy = True
    queued_session.queue.append("later")

    busy_result = set_session_execution_backend(busy_session, "tmux")
    queued_result = set_session_execution_backend(queued_session, "tmux")

    assert busy_result.switched is False
    assert busy_result.reason == "execution backend is configured in settings"
    assert queued_result.switched is False
    assert queued_result.reason == "execution backend is configured in settings"
    assert get_session_execution_backend(busy_session) == "headless"
    assert get_session_execution_backend(queued_session) == "headless"


def test_set_session_execution_backend_allows_busy_noop(tmp_path) -> None:
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "claude", str(tmp_path))
    session.busy = True
    session.queue.append("later")

    result = set_session_execution_backend(session, "headless")

    assert result.switched is False
    assert result.reason is None
    assert result.active_backend == "headless"


def test_set_session_execution_backend_tick_active_does_not_matter_for_settings_only(tmp_path) -> None:
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "claude", str(tmp_path))
    session.is_active_by_tick = lambda: True

    result = set_session_execution_backend(session, "tmux")
    noop = set_session_execution_backend(session, "headless")

    assert result.switched is False
    assert result.reason == "execution backend is configured in settings"
    assert get_session_execution_backend(session) == "headless"
    assert noop.reason is None


def test_set_session_execution_backend_rejects_unavailable_backend(tmp_path) -> None:
    manager = SessionManager(_cfg(tmp_path))
    session = manager.create(1, "codex", str(tmp_path))

    result = set_session_execution_backend(session, "tmux")

    assert result.switched is False
    assert result.active_backend == "headless"
    assert result.reason == "backend not available for cli"
    assert get_session_execution_backend(session) == "headless"


def test_set_session_execution_backend_allows_interactive_noop_save(tmp_path) -> None:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1]),
        tools={
            "shell": ToolConfig(
                name="shell",
                mode="interactive",
                cmd=["bash"],
            )
        },
        defaults=DefaultsConfig(workdir=str(tmp_path)),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    session = SessionManager(cfg).create(1, "shell", str(tmp_path))

    assert available_execution_backends(session) == ["interactive"]
    result = set_session_execution_backend(session, "interactive")

    assert result.reason is None
    assert result.switched is False
    assert result.active_backend == "interactive"


def test_set_active_cli_uses_current_global_default_after_hot_reload(tmp_path) -> None:
    cfg = _cfg(tmp_path, default_backend="headless")
    cfg.tools["codex"].interactive_cmd = ["codex"]
    cfg.tools["codex"].execution_backends = ["headless", "tmux"]
    manager = SessionManager(cfg)
    session = manager.create(1, "claude", str(tmp_path))

    session.set_active_cli("codex")
    cfg.defaults.default_execution_backend = "tmux"

    assert get_session_execution_backend(session, "codex") == "tmux"
