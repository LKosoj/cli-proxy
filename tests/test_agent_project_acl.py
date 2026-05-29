import os

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def test_agent_mode_project_root_is_limited_to_user_workdirs_for_non_admin(tmp_path):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    os.makedirs(allowed, exist_ok=True)
    os.makedirs(other, exist_ok=True)

    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=[1],
            admlist_chat_ids=[],
            user_workdirs={1: [str(allowed)]},
            user_modes={1: ["agent"]},
        ),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
            openai_api_key="k",
            openai_model="m",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    session = app.manager.create(1, "dummy", str(allowed))
    agent = app.mode_registry.get("agent")
    assert agent is not None

    ok, msg = agent._set_project_root(app, session, 1, None, str(other))
    assert not ok
    assert "недоступен" in msg.lower()

    ok2, _msg2 = agent._set_project_root(app, session, 1, None, str(allowed))
    assert ok2
    assert os.path.realpath(session.project_root) == os.path.realpath(str(allowed))
