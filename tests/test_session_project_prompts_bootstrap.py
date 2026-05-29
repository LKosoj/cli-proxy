import logging

from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from session import SessionManager


def _cfg(tmp_path):
    tools = {
        "dummy": ToolConfig(name="dummy", mode="headless", cmd=["bash", "-lc", "cat"]),
    }
    return AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1]),
        tools=tools,
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            default_cli="dummy",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_session_create_bootstraps_project_prompts_for_manager_and_webmaster(tmp_path):
    cfg = _cfg(tmp_path)
    manager = SessionManager(cfg)

    _ = manager.create(1, "dummy", str(tmp_path))

    for mode_id in ("manager", "webmaster"):
        prompt_dir = tmp_path / ".cli-proxy" / f".{mode_id}" / "prompt"
        assert (prompt_dir / "prompts.yaml").exists()
        assert (prompt_dir / "learning.yaml").exists()


def test_desktop_session_service_create_bootstraps_project_prompts_for_manager_and_webmaster(tmp_path):
    cfg = _cfg(tmp_path)
    sessions = SessionService(SessionManager(cfg), TaskService())

    _ = sessions.create_session(1, "dummy", str(tmp_path))

    for mode_id in ("manager", "webmaster"):
        prompt_dir = tmp_path / ".cli-proxy" / f".{mode_id}" / "prompt"
        assert (prompt_dir / "prompts.yaml").exists()
        assert (prompt_dir / "learning.yaml").exists()


def test_session_create_does_not_fail_when_prompt_bootstrap_errors(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path)
    manager = SessionManager(cfg)

    def _boom(_workdir):
        raise RuntimeError("boom")

    monkeypatch.setattr("session.ensure_project_prompts", _boom)
    with caplog.at_level(logging.ERROR):
        session = manager.create(1, "dummy", str(tmp_path))

    assert session is not None
    assert session.id == "s1"
    assert "project prompts bootstrap failed" in caplog.text
