import asyncio
from pathlib import Path

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig


def _build_app(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1]),
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
    return app


def test_send_output_logs_reason_when_document_send_fails(tmp_path, monkeypatch):
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))

        async def _send_message(_ctx, chat_id, text, **kwargs):
            return None

        async def _send_document(_ctx, chat_id, document, **kwargs):
            app._last_delivery_error = "mocked document failure reason"
            return False

        monkeypatch.setattr(app, "_send_message", _send_message)
        monkeypatch.setattr(app, "_send_document", _send_document)

        # Make HTML conversion fast/pure.
        import sessions.session_management as sm_mod

        monkeypatch.setattr(sm_mod, "ansi_to_html", lambda text, **_kw: "<html><body>x</body></html>")

        def _make_html_file(html_text, prefix):
            path = Path(tmp_path) / f"{prefix}-x.html"
            path.write_text(html_text, encoding="utf-8")
            return str(path)

        monkeypatch.setattr(sm_mod, "make_html_file", _make_html_file)

        warnings = []
        logger = __import__("logging").getLogger("bot.send_output")

        def _capture_warning(msg, *args, **kwargs):
            try:
                text = str(msg) % args if args else str(msg)
            except Exception:
                text = str(msg)
            warnings.append(text)

        monkeypatch.setattr(logger, "warning", _capture_warning)
        await app.send_output(session, {"kind": "telegram", "chat_id": 1}, "x" * 5000, context=None, force_html=True)
        assert any("failed to send document: mocked document failure reason" in m for m in warnings)

    asyncio.run(_run())
