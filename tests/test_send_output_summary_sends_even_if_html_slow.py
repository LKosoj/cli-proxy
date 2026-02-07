import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def test_send_output_sends_preview_even_if_html_is_slow(tmp_path, monkeypatch):
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

        import bot as bot_mod

        # Make summary return quickly (no summary, with error).
        async def _fake_summary(_text, config):
            return None, "таймаут"

        monkeypatch.setattr(bot_mod, "summarize_text_with_reason", _fake_summary)

        # Make HTML generation block.
        gate = asyncio.Event()

        def _ansi_to_html(_s: str):
            return "<html/>"

        monkeypatch.setattr(bot_mod, "ansi_to_html", _ansi_to_html)

        def _make_html_file(_html, _prefix):
            p = tmp_path / "out.html"
            p.write_text("x", encoding="utf-8")
            return str(p)

        monkeypatch.setattr(bot_mod, "make_html_file", _make_html_file)

        async def _to_thread(fn, *args, **kwargs):
            # Block only the HTML conversion stage.
            if fn is _ansi_to_html:
                await gate.wait()
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _to_thread)

        # Don't wait for HTML to send preview in tests.
        monkeypatch.setattr(bot_mod, "_SUMMARY_WAIT_FOR_HTML_S", 0.0)

        events = []

        async def _send_message(_ctx, chat_id, text, **kwargs):
            events.append(("msg", text))
            return True

        async def _send_document(_ctx, chat_id, document, **kwargs):
            events.append(("doc", "sent"))
            return True

        monkeypatch.setattr(app, "_send_message", _send_message)
        monkeypatch.setattr(app, "_send_document", _send_document)

        dest = {"kind": "telegram", "chat_id": 1}
        output = "x" * 60000

        t = asyncio.create_task(app.send_output(session, dest, output, context=None, force_html=True))
        # Give the event loop a chance to run the summary send task.
        await asyncio.sleep(0)
        assert any(k == "msg" for (k, _v) in events)
        # Cleanup.
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    asyncio.run(_run())
