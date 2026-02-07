import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


def test_send_output_summary_uses_tail_50k(tmp_path, monkeypatch):
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

        seen = {"text": None}

        import session_management as sm_mod

        async def _fake_summary(text, config):
            seen["text"] = text
            return "SUMMARY", None

        monkeypatch.setattr(sm_mod, "summarize_text_with_reason", _fake_summary)

        # Avoid HTML path in test.
        async def _send_message(_ctx, chat_id, text, **kwargs):
            return True

        async def _send_document(_ctx, chat_id, document, **kwargs):
            return True

        monkeypatch.setattr(app, "_send_message", _send_message)
        monkeypatch.setattr(app, "_send_document", _send_document)

        # Avoid threads for html conversion and file IO.
        monkeypatch.setattr(sm_mod, "ansi_to_html", lambda _s: "<html/>")

        def _make_html_file(_html, _prefix):
            p = tmp_path / "out.html"
            p.write_text("x", encoding="utf-8")
            return str(p)

        monkeypatch.setattr(sm_mod, "make_html_file", _make_html_file)

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _to_thread)

        head = "H" * 60000
        tail = "T" * 50000
        output = head + tail

        dest = {"kind": "telegram", "chat_id": 1}
        await app.send_output(session, dest, output, context=None, force_html=True)
        assert seen["text"] == output[-50000:]

    asyncio.run(_run())
