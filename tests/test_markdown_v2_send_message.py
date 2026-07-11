import asyncio
import types
from pathlib import Path

from telegram.error import BadRequest, TimedOut

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp
from app.services.rich_draft_coordinator import RichDraftCoordinator
from app.services.telegram_transport import TelegramEditOutcome, TelegramTransportContext
from tg.markdown import escape_markdown_v2_all, to_markdown_v2
from tg.rich import RICH_MARKDOWN_CHAR_LIMIT


_MD2_FALSE_TEST_ALLOWLIST = {
    "tests/test_markdown_v2_send_message.py": "Telegram transport boundary keeps md2=False retry and literal paths testable.",
    "tests/test_notification_queue_service.py": "TelegramTransportService queue tests need raw md2=False as boundary input.",
    "tests/test_telegram_thread_routing.py": "Thread-bound Telegram transport test exercises missing-thread rejection.",
    "tests/test_mode_sdk_base.py": "MessagingService boundary tests assert send_plain_text delegates to md2=False.",
    "tests/test_architecture_debt_evidence_protocol.py": "Architecture debt evidence protocol documents md2=False grep acceptance.",
}


def _build_transport_test_app(tmp_path):
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
    return app


def test_send_message_md2_uses_raw_rich_first(tmp_path, monkeypatch):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        captured = {}
        raw_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            return types.SimpleNamespace(message_id=1)

        async def _send_message(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message, _post=_post))

        await app._send_message(ctx, chat_id=1, text="**bold**", md2=True)
        assert captured == {}
        assert raw_calls == [
            {
                "endpoint": "sendRichMessage",
                "data": {
                    "chat_id": 1,
                    "rich_message": {"markdown": "**bold**"},
                },
            }
        ]

        captured.clear()
        await app._send_message(ctx, chat_id=1, text="plain", md2=False)
        assert "parse_mode" not in captured
        assert len(raw_calls) == 1

    asyncio.run(_run())


def test_send_and_edit_message_can_skip_raw_rich(tmp_path):
    async def _run():
        app = _build_transport_test_app(tmp_path)
        raw_calls = []
        sent_messages = []
        edited_messages = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            return types.SimpleNamespace(message_id=1)

        async def _send_message(**kwargs):
            sent_messages.append(dict(kwargs))
            return types.SimpleNamespace(message_id=1)

        async def _edit_message_text(**kwargs):
            edited_messages.append(dict(kwargs))
            return True

        ctx = types.SimpleNamespace(
            bot=types.SimpleNamespace(
                _post=_post,
                send_message=_send_message,
                edit_message_text=_edit_message_text,
            )
        )

        await app._send_message(
            ctx,
            chat_id=1,
            text="**status**",
            md2=True,
            prefer_rich=False,
        )
        await app._edit_message(
            ctx,
            chat_id=1,
            message_id=2,
            text="**updated**",
            md2=True,
            prefer_rich=False,
        )

        assert raw_calls == []
        assert sent_messages[0]["text"] == "status"
        assert len(sent_messages[0]["entities"]) == 1
        assert sent_messages[0]["entities"][0].type == "bold"
        assert edited_messages[0]["text"] == "updated"
        assert len(edited_messages[0]["entities"]) == 1
        assert edited_messages[0]["entities"][0].type == "bold"

    asyncio.run(_run())


def test_rich_message_draft_api_remains_available(tmp_path):
    async def _run():
        app = _build_transport_test_app(tmp_path)
        raw_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            return True

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(_post=_post))

        sent = await app._send_rich_message_draft(
            ctx,
            chat_id=1,
            message_thread_id=7,
            draft_id=42,
            rich_message={"markdown": "draft"},
        )

        assert sent is True
        assert isinstance(app.rich_draft_coordinator, RichDraftCoordinator)
        assert raw_calls == [
            {
                "endpoint": "sendRichMessageDraft",
                "data": {
                    "chat_id": 1,
                    "message_thread_id": 7,
                    "draft_id": 42,
                    "rich_message": {"markdown": "draft"},
                },
            }
        ]

    asyncio.run(_run())


def test_send_message_raw_rich_bad_request_falls_back_to_current_pipeline(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        calls = []
        raw_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            raise BadRequest("can't parse rich markdown")

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise BadRequest("Can't parse entities: can't find end of italic entity")
            return types.SimpleNamespace(message_id=2)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message, _post=_post))

        await app._send_message(ctx, chat_id=1, text="_broken italic_", md2=True)
        assert len(raw_calls) == 1
        assert raw_calls[0]["endpoint"] == "sendRichMessage"
        assert raw_calls[0]["data"]["rich_message"] == {"markdown": "_broken italic_"}
        assert len(calls) == 2
        assert "parse_mode" not in calls[0]
        assert calls[0].get("entities")
        # Second try should be safe MarkdownV2 (formatting disabled, but still parseable).
        assert calls[1].get("parse_mode") == "MarkdownV2"
        assert calls[1].get("text") is not None

    asyncio.run(_run())


def test_send_message_uses_legacy_fallback_for_text_over_raw_rich_limit(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        raw_calls = []
        legacy_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            return types.SimpleNamespace(message_id=len(raw_calls))

        async def _send_message(**kwargs):
            legacy_calls.append(dict(kwargs))
            text = str(kwargs.get("text") or "")
            assert len(text) <= 4090
            return types.SimpleNamespace(message_id=len(legacy_calls))

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message, _post=_post))
        long_text = "**" + ("x" * (RICH_MARKDOWN_CHAR_LIMIT + 100)) + "**"

        await app._send_message(ctx, chat_id=1, text=long_text, md2=True)
        assert raw_calls == []
        assert len(legacy_calls) > 1
        assert "".join(
            str(call.get("text") or "")
            for call in legacy_calls
        ) == "x" * (RICH_MARKDOWN_CHAR_LIMIT + 100)

    asyncio.run(_run())


def test_send_message_splits_before_markdown_block_start(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        calls = []
        raw_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            raise BadRequest("can't parse rich markdown")

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(message_id=len(calls))

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message, _post=_post))
        prefix = "x" * 4088
        text = prefix + " **bold-block** tail"

        await app._send_message(ctx, chat_id=1, text=text, md2=True)
        assert len(raw_calls) == 1
        assert len(calls) >= 2
        first_chunk = str(calls[0].get("text") or "")
        second_chunk = str(calls[1].get("text") or "")

        assert "parse_mode" not in calls[0]
        assert "parse_mode" not in calls[1]
        assert first_chunk.endswith(" b")
        assert second_chunk == "old-block tail"
        assert first_chunk + second_chunk == prefix + " bold-block tail"
        first_entities = list(calls[0].get("entities") or [])
        second_entities = list(calls[1].get("entities") or [])
        assert len(first_entities) == 1
        assert first_entities[0].type == "bold"
        assert first_entities[0].length == 1
        assert len(second_entities) == 1
        assert second_entities[0].type == "bold"
        assert second_entities[0].length == 9

    asyncio.run(_run())


def test_edit_message_md2_uses_raw_rich_first(tmp_path):
    async def _run():
        app = _build_transport_test_app(tmp_path)
        raw_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            return True

        async def _edit_message_text(**_kwargs):
            raise AssertionError("legacy edit_message_text should not be used when raw rich succeeds")

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(_post=_post, edit_message_text=_edit_message_text))

        ok = await app._edit_message(ctx, chat_id=1, message_id=9, text="**bold**", md2=True)

        assert ok is True
        assert raw_calls == [
            {
                "endpoint": "editMessageText",
                "data": {
                    "chat_id": 1,
                    "message_id": 9,
                    "rich_message": {"markdown": "**bold**"},
                },
            }
        ]

    asyncio.run(_run())


def test_edit_message_md2_raw_rich_bad_request_falls_back_to_current_pipeline(tmp_path):
    async def _run():
        app = _build_transport_test_app(tmp_path)
        raw_calls = []
        edit_calls = []

        async def _post(endpoint, data=None, **_kwargs):
            raw_calls.append({"endpoint": endpoint, "data": dict(data or {})})
            raise BadRequest("can't parse rich markdown")

        async def _edit_message_text(**kwargs):
            edit_calls.append(dict(kwargs))
            return True

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(_post=_post, edit_message_text=_edit_message_text))

        ok = await app._edit_message(ctx, chat_id=1, message_id=9, text="**bold**", md2=True)

        assert ok is True
        assert len(raw_calls) == 1
        assert raw_calls[0]["endpoint"] == "editMessageText"
        assert edit_calls
        assert edit_calls[0]["chat_id"] == 1
        assert edit_calls[0]["message_id"] == 9
        assert edit_calls[0]["text"] == "bold"
        entities = list(edit_calls[0].get("entities") or [])
        assert len(entities) == 1
        assert entities[0].type == "bold"

    asyncio.run(_run())


def test_edit_message_outcome_classifies_retry_replace_and_not_modified(tmp_path):
    async def _run():
        app = _build_transport_test_app(tmp_path)
        errors = iter(
            (
                TimedOut("temporary"),
                BadRequest("Message to edit not found"),
                BadRequest("Message is not modified"),
            )
        )

        async def _edit_message_text(**_kwargs):
            raise next(errors)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(edit_message_text=_edit_message_text))

        outcomes = [
            await app._edit_message_outcome(
                ctx,
                chat_id=1,
                message_id=message_id,
                text="status",
                prefer_rich=False,
            )
            for message_id in (1, 2, 3)
        ]

        assert outcomes == [
            TelegramEditOutcome.RETRY,
            TelegramEditOutcome.REPLACE,
            TelegramEditOutcome.UPDATED,
        ]

    asyncio.run(_run())


def test_send_message_retry_keeps_md2_false(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise TimedOut("temporary")
            return types.SimpleNamespace(message_id=42)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        await app._send_message(ctx, chat_id=1, text="plain", md2=False)

        assert len(calls) == 2
        assert "parse_mode" not in calls[0]
        assert "parse_mode" not in calls[1]

    asyncio.run(_run())


def test_send_message_retries_network_error_five_times(tmp_path, monkeypatch):
    async def _run():
        app = _build_transport_test_app(tmp_path)
        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            raise TimedOut("temporary")

        async def _sleep(_seconds):
            return None

        monkeypatch.setattr("app.services.telegram_transport.asyncio.sleep", _sleep)
        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))

        result = await app._send_message(
            ctx,
            chat_id=1,
            text="preview",
            prefer_rich=False,
        )

        assert result is None
        assert len(calls) == 5

    asyncio.run(_run())


def test_messaging_service_send_plain_text_keeps_markdown_symbols_literal(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("parse_mode") == "MarkdownV2" or kwargs.get("entities"):
                raise BadRequest("plain text must not use MarkdownV2 entities")
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        messaging = app._mode_messaging_factory(ctx)
        text = r"_literal_ *stars* [link](x) #hash! path\file"

        await messaging.send_plain_text(1, text, message_thread_id=77)

        assert calls == [{"chat_id": 1, "text": text, "message_thread_id": 77}]

    asyncio.run(_run())


def test_transport_context_preserves_direct_messages_topic_id(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        captured = {}

        async def _send_message(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(message_id=1)

        raw_context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        context = TelegramTransportContext(
            raw_context=raw_context,
            chat_id=1,
            message_thread_id=77,
            direct_messages_topic_id=888,
        )

        await app._send_message(context, text="hello", md2=False)

        assert captured["chat_id"] == 1
        assert captured["message_thread_id"] == 77
        assert captured["direct_messages_topic_id"] == 888

    asyncio.run(_run())


def test_private_missing_thread_retries_without_thread_routing(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("message_thread_id") is not None:
                raise BadRequest("Message thread not found")
            if kwargs.get("direct_messages_topic_id") is not None:
                raise BadRequest("direct messages topic fallback must not be used")
            return types.SimpleNamespace(message_id=42)

        raw_context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        context = TelegramTransportContext(
            raw_context=raw_context,
            chat_id=142987535,
            message_thread_id=374859,
        )

        result = await app._send_message(context, text="hello")

        assert result.message_id == 42
        assert app._last_delivery_error is None
        assert len(calls) == 4
        assert all(call["chat_id"] == 142987535 for call in calls)
        assert all(call.get("message_thread_id") == 374859 for call in calls[:3])
        assert calls[-1].get("message_thread_id") is None
        assert all(call.get("direct_messages_topic_id") is None for call in calls)

    asyncio.run(_run())


def test_md2_false_missing_thread_retries_without_thread_routing(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("message_thread_id") is not None:
                raise BadRequest("Message thread not found")
            return types.SimpleNamespace(message_id=44)

        raw_context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        context = TelegramTransportContext(
            raw_context=raw_context,
            chat_id=142987535,
            message_thread_id=374859,
        )

        result = await app._send_message(context, text="_literal_", md2=False)

        assert result.message_id == 44
        assert calls == [
            {"chat_id": 142987535, "text": "_literal_", "message_thread_id": 374859},
            {"chat_id": 142987535, "text": "_literal_"},
        ]

    asyncio.run(_run())


def test_group_missing_thread_retries_without_message_thread_id(tmp_path):
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
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            if kwargs.get("message_thread_id") is not None:
                raise BadRequest("Message thread not found")
            return types.SimpleNamespace(message_id=43)

        raw_context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        context = TelegramTransportContext(
            raw_context=raw_context,
            chat_id=-100777000111,
            message_thread_id=374859,
        )

        result = await app._send_message(context, text="hello")

        assert result.message_id == 43
        assert len(calls) == 4
        assert all(call.get("message_thread_id") == 374859 for call in calls[:3])
        assert calls[-1].get("message_thread_id") is None

    asyncio.run(_run())


def test_md2_false_test_usages_are_documented_boundary_allowlist() -> None:
    root = Path(__file__).resolve().parents[1]
    needle = "md2" + "=False"
    matched_files = {
        str(path.relative_to(root))
        for path in (root / "tests").rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    }

    unexpected = sorted(matched_files - set(_MD2_FALSE_TEST_ALLOWLIST))
    assert unexpected == []


def test_escape_markdown_v2_all_escapes_specials() -> None:
    raw = r"_*[]()~`>#+-=|{}.!\\"
    out = escape_markdown_v2_all(raw)
    # Every special symbol must be escaped with backslash.
    assert r"\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!\\\\" == out


def test_to_markdown_v2_rewrites_local_workspace_links() -> None:
    raw = "См.: [run_command.py#L48](/srv/git_projects/cli-proxy/agent/plugins/run_command.py#L48)"
    out = to_markdown_v2(raw)
    # Local file links are not kept as Markdown links for Telegram.
    assert "](" not in out
    plain = out.replace("\\", "")
    assert "run_command.py#L48" in plain
    assert "/srv/git_projects/cli-proxy/agent/plugins/run_command.py#L48" in plain


def test_to_markdown_v2_keeps_plain_underscores_literal() -> None:
    raw = "foo_bar_baz path a_b c_d"
    out = to_markdown_v2(raw)
    assert out.replace("\\", "") == raw
