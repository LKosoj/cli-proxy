import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp
from session import session_runtime_uid


def test_interrupt_before_close_cancels_only_that_session(tmp_path):
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
        s1 = app.manager.create(123, "dummy", str(tmp_path / "w1"))
        s2 = app.manager.create(456, "dummy", str(tmp_path / "w2"))
        assert s1.id == s2.id
        uid1 = session_runtime_uid(s1)
        uid2 = session_runtime_uid(s2)
        assert uid1 != uid2

        async def sleeper():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

        app.mode_tasks.create(session_uid=uid1, mode_id="agent", coro=sleeper(), name="a1")
        app.mode_tasks.create(session_uid=uid2, mode_id="agent", coro=sleeper(), name="a2")

        app._interrupt_before_close(s1.id, chat_id=123, context=None)  # context unused
        await asyncio.sleep(0.05)

        assert app.mode_tasks.list(session_uid=uid1, mode_id="agent") == []
        assert app.mode_tasks.list(session_uid=uid2, mode_id="agent") == ["a2"]

        await app.mode_tasks.cancel_session(session_uid=uid2, timeout_s=0.5)

    asyncio.run(_run())
