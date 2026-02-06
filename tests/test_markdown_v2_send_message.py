import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def test_send_message_md2_sets_parse_mode_and_escapes(tmp_path, monkeypatch):
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
        app.agent.record_message = lambda *_args, **_kwargs: None

        captured = {}

        async def _send_message(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))

        await app._send_message(ctx, chat_id=1, text="**bold**", md2=True)
        assert captured.get("parse_mode") == "MarkdownV2"
        assert "*bold*" in (captured.get("text") or "")

        captured.clear()
        await app._send_message(ctx, chat_id=1, text="plain", md2=False)
        assert "parse_mode" not in captured

    asyncio.run(_run())
