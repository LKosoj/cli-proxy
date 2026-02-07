import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def test_run_agent_success_does_not_send_messages(tmp_path, monkeypatch):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
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
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        app = BotApp(cfg)
        session = app.manager.create("dummy", str(tmp_path / "w1"))
        session.agent_enabled = True

        sent = []

        async def _send_message(_ctx, chat_id, text, **kwargs):
            sent.append(("msg", text))
            return True

        async def _send_document(_ctx, chat_id, document, **kwargs):
            sent.append(("doc", "x"))
            return True

        monkeypatch.setattr(app, "_send_message", _send_message)
        monkeypatch.setattr(app, "_send_document", _send_document)

        async def _fake_orch_run(_session, _user_text, _bot, _context, _dest):
            return "FINAL ANSWER (should not be sent here)"

        monkeypatch.setattr(app.agent, "run", _fake_orch_run)

        await app.run_agent(session, "prompt", {"kind": "telegram", "chat_id": 1}, context=None)
        assert sent == []

    asyncio.run(_run())
