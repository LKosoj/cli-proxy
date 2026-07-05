import asyncio

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp
from app.services.notification_queue_service import NotificationQueueService
from app.services.telegram_transport import TelegramTransportContext
from sessions.conversation_scope import ConversationScope


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
        session = app.manager.create(1, "dummy", str(tmp_path / "w1"))

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
        import sessions.session_management as sm_mod

        summary_started = asyncio.Event()
        allow_html = asyncio.Event()

        async def _fake_summary(text, config=None, *, language="ru"):
            summary_started.set()
            return "SUMMARY", None

        monkeypatch.setattr(sm_mod, "summarize_text_with_reason", _fake_summary)

        def _ansi_to_html(_s, **_kw):
            # This runs inside asyncio.to_thread in prod. In test we override to_thread to be awaitable,
            # so we can block it until summary has started.
            return "<html>ok</html>"

        monkeypatch.setattr(sm_mod, "ansi_to_html", _ansi_to_html)

        def _make_html_file(html, prefix):
            p = tmp_path / "out.html"
            p.write_text(html, encoding="utf-8")
            return str(p)

        monkeypatch.setattr(sm_mod, "make_html_file", _make_html_file)

        async def _to_thread(fn, *args, **kwargs):
            # Force the HTML path to wait until summary started to prove we run them in parallel.
            # H8: session path wraps the renderer in functools.partial(..., allow_network_fetch=True),
            # so unwrap .func before the identity check.
            if getattr(fn, "func", fn) is _ansi_to_html:
                # Wait until summary coroutine starts, otherwise we'd be sequential.
                await asyncio.wait_for(summary_started.wait(), timeout=1.0)
                # Additionally wait for explicit release so ordering is deterministic.
                await asyncio.wait_for(allow_html.wait(), timeout=1.0)
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _to_thread)

        dest = {"kind": "telegram", "chat_id": 1}
        output = "x" * 33000
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


def test_send_output_can_skip_summary(tmp_path, monkeypatch):
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
        session = app.manager.create(1, "dummy", str(tmp_path / "w1"))

        events = []
        called = {"summary": 0}

        async def _send_message(_ctx, chat_id, text, **kwargs):
            events.append(("msg", text))
            return True

        async def _send_document(_ctx, chat_id, document, **kwargs):
            events.append(("doc", "sent"))
            return True

        monkeypatch.setattr(app, "_send_message", _send_message)
        monkeypatch.setattr(app, "_send_document", _send_document)

        import sessions.session_management as sm_mod

        async def _fake_summary(_text, config=None, *, language="ru"):
            called["summary"] += 1
            return "SUMMARY", None

        monkeypatch.setattr(sm_mod, "summarize_text_with_reason", _fake_summary)
        monkeypatch.setattr(sm_mod, "ansi_to_html", lambda _s, **_kw: "<html>ok</html>")

        def _make_html_file(html, prefix):
            p = tmp_path / "out.html"
            p.write_text(html, encoding="utf-8")
            return str(p)

        monkeypatch.setattr(sm_mod, "make_html_file", _make_html_file)

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _to_thread)

        dest = {"kind": "telegram", "chat_id": 1}
        await app.send_output(
            session,
            dest,
            "x" * 5000,
            context=None,
            send_header=False,
            force_html=True,
            send_summary=False,
        )

        assert events == [("doc", "sent")]
        assert called["summary"] == 0

    asyncio.run(_run())


def test_send_output_uses_notification_queue_as_atomic_report_delivery(tmp_path, monkeypatch):
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
        app.notification_queue_service = NotificationQueueService(min_interval_sec=0.0)
        await app.notification_queue_service.start()
        session = app.manager.create(1, "dummy", str(tmp_path / "w1"))
        session.conversation_scope = ConversationScope.from_parts(-100777000111, 101)

        events = []
        header_started = asyncio.Event()
        release_header = asyncio.Event()

        class _FakeBot:
            async def send_message(self, **kwargs):
                text = str(kwargs.get("text") or "")
                events.append(("msg", text, kwargs.get("message_thread_id")))
                if len(events) == 1:
                    header_started.set()
                    await release_header.wait()
                return type("Msg", (), {"message_id": len(events)})()

            async def send_document(self, **kwargs):
                events.append(("doc", "sent", kwargs.get("message_thread_id")))
                return True

        raw_context = type("Ctx", (), {"bot": _FakeBot()})()
        context = TelegramTransportContext(
            raw_context,
            chat_id=-100777000111,
            message_thread_id=101,
            require_thread_id=True,
            session_uid=session.conversation_scope.session_uid,
        )

        import sessions.session_management as sm_mod

        async def _fake_summary(_text, config=None, *, language="ru"):
            return "SUMMARY", None

        monkeypatch.setattr(sm_mod, "summarize_text_with_reason", _fake_summary)
        monkeypatch.setattr(sm_mod, "ansi_to_html", lambda _s, **_kw: "<html>ok</html>")

        def _make_html_file(html, prefix):
            p = tmp_path / "out.html"
            p.write_text(html, encoding="utf-8")
            return str(p)

        monkeypatch.setattr(sm_mod, "make_html_file", _make_html_file)

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", _to_thread)

        dest = {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 101}
        report_task = asyncio.create_task(app.send_output(session, dest, "x" * 33000, context=context))

        await asyncio.wait_for(header_started.wait(), timeout=1.0)

        async def _other() -> str:
            events.append(("other", "queued", 101))
            return "other"

        other_task = asyncio.create_task(
            app.notification_queue_service.enqueue(
                session.conversation_scope,
                operation="other",
                factory=_other,
            )
        )

        await asyncio.sleep(0)
        assert other_task.done() is False

        release_header.set()

        await report_task
        assert await other_task == "other"

        assert events[0][0] == "msg"
        assert events[0][2] == 101
        assert events[1] == ("doc", "sent", 101)
        assert events[2] == ("msg", "SUMMARY", 101)
        assert events[3] == ("other", "queued", 101)

        await app.notification_queue_service.shutdown()

    asyncio.run(_run())
