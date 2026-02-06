import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


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
        s1 = app.manager.create("dummy", str(tmp_path / "w1"))
        s2 = app.manager.create("dummy", str(tmp_path / "w2"))

        async def sleeper():
            await asyncio.sleep(3600)

        t1 = asyncio.create_task(sleeper())
        t2 = asyncio.create_task(sleeper())
        app.agent_tasks[s1.id] = t1
        app.agent_tasks[s2.id] = t2

        app._interrupt_before_close(s1.id, chat_id=123, context=None)  # context unused
        await asyncio.sleep(0)

        assert t1.cancelled() or t1.done()
        assert not (t2.cancelled() or t2.done())

        # Cleanup
        t1.cancel()
        t2.cancel()
        try:
            await t2
        except BaseException:
            pass
        try:
            await t1
        except BaseException:
            pass

    asyncio.run(_run())
