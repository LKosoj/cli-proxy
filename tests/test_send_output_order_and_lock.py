import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def test_send_output_sends_html_before_summary(tmp_path, monkeypatch):
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
                summary_max_chars=200,
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )

        app = BotApp(cfg)
        session = app.manager.create("dummy", str(tmp_path / "w1"))

        events = []

        async def _send_message(_ctx, chat_id, text, **kwargs):
            events.append(("msg", text))
            return True

        async def _send_document(_ctx, chat_id, document, **kwargs):
            events.append(("doc", "sent"))
            return True

        monkeypatch.setattr(app, "_send_message", _send_message)
        monkeypatch.setattr(app, "_send_document", _send_document)

        # Avoid threads / heavy conversion in tests.
        import bot as bot_mod

        summary_started = asyncio.Event()
        allow_html = asyncio.Event()

        async def _fake_summary(text, config):
            summary_started.set()
            return "SUMMARY", None

        monkeypatch.setattr(bot_mod, "summarize_text_with_reason", _fake_summary)

        def _ansi_to_html(_s):
            # This runs inside asyncio.to_thread in prod. In test we override to_thread to be awaitable,
            # so we can block it until summary has started.
            return "<html>ok</html>"

        monkeypatch.setattr(bot_mod, "ansi_to_html", _ansi_to_html)

        def _make_html_file(html, prefix):
            p = tmp_path / "out.html"
            p.write_text(html, encoding="utf-8")
            return str(p)

        monkeypatch.setattr(bot_mod, "make_html_file", _make_html_file)

        async def _to_thread(fn, *args, **kwargs):
            # Force the HTML path to wait until summary started to prove we run them in parallel.
            if fn is _ansi_to_html:
                # Wait until summary coroutine starts, otherwise we'd be sequential.
                await asyncio.wait_for(summary_started.wait(), timeout=1.0)
                # Additionally wait for explicit release so ordering is deterministic.
                await asyncio.wait_for(allow_html.wait(), timeout=1.0)
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _to_thread)

        dest = {"kind": "telegram", "chat_id": 1}
        output = "x" * 5000
        # Let HTML generation proceed only after we've observed summary started.

        async def _release():
            await asyncio.wait_for(summary_started.wait(), timeout=1.0)
            allow_html.set()

        asyncio.create_task(_release())
        await app.send_output(session, dest, output, context=None)

        # We expect: header msg, then document, then summary msg.
        kinds = [k for (k, _v) in events]
        assert kinds.count("doc") == 1
        assert kinds[0] == "msg"
        assert kinds[1] == "doc"
        assert kinds[2] == "msg"
        assert events[2][1] == "SUMMARY"

    asyncio.run(_run())
