import types

from app.services.run_artifact_store import RunArtifactStore
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.agent.mode import AgentMode
from sessions.conversation_scope import ConversationScope


def _build_config(tmp_path) -> AppConfig:
    return AppConfig(
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
            run_artifacts_enabled=True,
            run_metrics_enabled=True,
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )


def test_agent_reads_legacy_run_artifacts_without_migration(tmp_path) -> None:
    config = _build_config(tmp_path)
    session = types.SimpleNamespace(
        id="legacy-session",
        chat_id=1,
        workdir=str(tmp_path),
        conversation_scope=ConversationScope.from_parts(1),
    )
    artifact_store = RunArtifactStore(config)
    legacy_run = artifact_store.start_run(
        session=session,
        mode_id="agent",
        run_id="run_20260312T221000Z_agent_legacy001",
        phase="execute",
        source_prompt_hash="sha256:legacy-agent",
        mode_context={
            "run_scope": "mode_pipeline",
            "source_prompt": "legacy prompt",
        },
    )
    artifact_store.save_state(
        legacy_run,
        {
            "phase": "complete",
            "status": "completed",
            "mode_context": {
                "run_scope": "mode_pipeline",
                "source_prompt": "legacy prompt",
                "final_deliverable": "legacy answer",
            },
        },
    )

    mode = AgentMode()
    mode.config = config

    latest = mode._latest_mode_run(session)  # type: ignore[attr-defined]
    assert latest is not None
    assert latest.run_id == legacy_run.run_id
    state = artifact_store.load_state(latest)
    assert state["status"] == "completed"
    assert state["mode_context"]["final_deliverable"] == "legacy answer"
