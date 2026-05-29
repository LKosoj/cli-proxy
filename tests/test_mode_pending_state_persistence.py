import asyncio
from pathlib import Path

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.agent.mode import agent_project_scope_key
from session import session_scoped_key


def _build_app(tmp_path: Path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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
    return BotApp(cfg)


def test_pending_state_persists_across_bot_restart_for_agent_and_manager(tmp_path) -> None:
    app1 = _build_app(tmp_path)
    s1 = app1.manager.create(1, "dummy", str(tmp_path))
    s2 = app1.manager.create(1, "dummy", str(tmp_path))

    first_prompt = {"prompt": "intent-1", "dest": {"kind": "telegram", "chat_id": 1}, "created_at": 100.0}
    second_prompt = {"prompt": "intent-2", "dest": {"kind": "telegram", "chat_id": 1}, "created_at": 200.0}
    app1.mode_manager_resume_pending.set(s1.id, first_prompt)
    app1.mode_manager_resume_pending.set(s2.id, second_prompt)
    app1.mode_agent_project_pending_by_chat.set(
        agent_project_scope_key(1),
        {
            "session_id": s1.id,
            "session_scoped_key": session_scoped_key(s1),
            "ui_chat_id": 1,
            "message_thread_id": None,
        },
    )

    app2 = _build_app(tmp_path)

    assert app2.mode_manager_resume_pending.get(s1.id) == first_prompt
    assert app2.mode_manager_resume_pending.get(s2.id) == second_prompt
    assert app2.mode_agent_project_pending_by_chat.get(agent_project_scope_key(1)) == {
        "session_id": s1.id,
        "session_scoped_key": session_scoped_key(s1),
        "ui_chat_id": 1,
        "message_thread_id": None,
    }


def test_agent_pending_state_restores_and_consumes_on_dirs_selection(tmp_path) -> None:
    async def _run() -> None:
        app1 = _build_app(tmp_path)
        session = app1.manager.create(1, "dummy", str(tmp_path))
        app1.mode_agent_project_pending_by_chat.set(
            agent_project_scope_key(1),
            {
                "session_id": session.id,
                "session_scoped_key": session_scoped_key(session),
                "ui_chat_id": 1,
                "message_thread_id": None,
            },
        )

        project_root = tmp_path / "project_root"
        project_root.mkdir(parents=True, exist_ok=True)

        app2 = _build_app(tmp_path)
        agent_mode = app2.mode_registry.get("agent")
        assert agent_mode is not None
        restored_session = app2.manager.get(1, session.id)
        assert restored_session is not None
        restored_session.modes.active_mode = "agent"

        result = await agent_mode.handle_dirs_selection(
            flow="project",
            event="selected",
            path=str(project_root),
            ctx={"bot_app": app2, "chat_id": 1, "context": object(), "session": restored_session},
        )

        assert result is not None
        assert bool(result.ok) is True
        assert restored_session is not None
        assert restored_session.project_root == str(project_root.resolve())
        assert app2.mode_agent_project_pending_by_chat.get(agent_project_scope_key(1)) is None

    asyncio.run(_run())
