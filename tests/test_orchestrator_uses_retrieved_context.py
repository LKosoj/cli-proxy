import asyncio

from modes.sdk.orchestrator_runner import OrchestratorRunner
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig


def test_orchestrator_passes_retrieved_context_to_planner(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        orch = OrchestratorRunner(cfg)

        monkeypatch.setattr(
            orch._deps,
            "retrieve_relevant_context",
            lambda _cwd, _query, limit=6: [
                {"source": "memory", "ts": "2026-02-11 10:00", "text": "используем sqlite fts5", "score": -1.2}
            ],
        )
        monkeypatch.setattr(
            orch._deps,
            "format_retrieved_context",
            lambda items, max_chars=1600: "- [memory] (2026-02-11 10:00): используем sqlite fts5" if items else "",
        )

        captured = {"ctx": ""}

        async def _fake_plan_steps(_cfg, _user_text, ctx_summary):
            captured["ctx"] = ctx_summary
            return []

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *_a, **_k: asyncio.sleep(0))

        session = type("S", (), {"id": "s1"})
        dest = {"kind": "telegram", "chat_id": 1, "chat_type": "private"}
        out = await orch.run(session, "ускорь память", bot=None, context=None, dest=dest)

        assert "retrieved_context:" in captured["ctx"]
        assert "sqlite fts5" in captured["ctx"]
        assert out == "(empty response)"

    asyncio.run(_run())
