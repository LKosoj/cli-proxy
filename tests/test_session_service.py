import logging

from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from session import SessionManager, session_runtime_uid


def _build_config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


def test_set_mode_by_uid_logs_legacy_fallback_when_persist_fails(tmp_path, caplog) -> None:
    cfg = _build_config(tmp_path)
    manager = SessionManager(cfg)
    service = SessionService(manager, TaskService())
    session = service.create_session(1, "dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)

    def _raise_persist_failure() -> None:
        raise OSError("persist failed")

    manager._persist_sessions = _raise_persist_failure  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="app.services.session_service"):
        assert service.set_mode_by_uid(session_uid, "agent") is True

    assert session.modes.active_mode == "agent"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "legacy fallback used" in message
        and "set_mode_by_uid" in message
        and session_uid in message
        and "mode_id=agent" in message
        for message in messages
    )
    assert any(record.exc_info for record in caplog.records)
