import asyncio
import types

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig


def test_start_command_prints_ids_and_admin_hint(tmp_path) -> None:
    async def _run() -> None:
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            effective_user=types.SimpleNamespace(id=456),
            message=types.SimpleNamespace(text="/start"),
        )

        await app.cmd_start(update, ctx)
        assert calls
        assert calls[-1]["chat_id"] == 123
        text = calls[-1]["text"]
        assert "456" in text
        assert "123" in text
        assert "Обратитесь к вашему администратору" in text

    asyncio.run(_run())
