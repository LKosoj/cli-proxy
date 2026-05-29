import asyncio
import types

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig


def _build_app(tmp_path):
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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


def _install_authorized_route(app, session):
    route = types.SimpleNamespace(
        reply_chat_id=1,
        owner_chat_id=1,
        message_thread_id=None,
        session_uid=None,
        session=session,
        reply_kwargs=lambda: {"chat_id": 1},
    )
    app.ensure_telegram_inbound_authorized = lambda _update, _ctx, **_kwargs: asyncio.sleep(
        0,
        result=route,
    )
    app.ensure_telegram_inbound_session = lambda _update, _ctx, auto_create=False: asyncio.sleep(
        0,
        result=(None, session),
    )
    return route


def _capture_staged_input(app):
    captured = {"calls": []}

    async def _flush_buffer(_chat_id, _session, _ctx):
        return None

    async def _stage_user_input(_session, payload, _chat_id, _ctx, *, dest=None, image_path=None, image_paths=None):
        _ = image_path, image_paths
        captured["calls"].append({"payload": payload, "dest": dict(dest or {})})

    app._flush_buffer = _flush_buffer
    app._stage_user_input = _stage_user_input
    return captured


def _document_context(data: bytes):
    class _FakeFile:
        async def download_as_bytearray(self):
            return bytearray(data)

    async def _get_file(_file_id):
        return _FakeFile()

    async def _send_message(**_kwargs):
        return types.SimpleNamespace(message_id=1)

    return types.SimpleNamespace(bot=types.SimpleNamespace(get_file=_get_file, send_message=_send_message))


def _document_update(doc, *, caption: str = "", media_group_id: str = ""):
    return types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=1),
        effective_user=types.SimpleNamespace(id=10),
        message=types.SimpleNamespace(document=doc, caption=caption, media_group_id=media_group_id),
    )


def _assert_payload_uses_saved_attachment(payload: str, tmp_path, marker: str, data: bytes) -> None:
    assert marker not in payload
    attachment_ref = next(line for line in payload.splitlines() if line.startswith("@"))
    assert attachment_ref.startswith("@.cli-proxy/.attachments/")
    saved_path = tmp_path / attachment_ref[1:]
    assert saved_path.read_bytes() == data


def test_on_document_media_group_combines_text_documents_into_single_payload(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        route = types.SimpleNamespace(
            reply_chat_id=1,
            owner_chat_id=1,
            message_thread_id=None,
            session_uid=None,
            session=session,
            reply_kwargs=lambda: {"chat_id": 1},
        )
        app.ensure_telegram_inbound_authorized = lambda _update, _ctx, **_kwargs: asyncio.sleep(
            0,
            result=route,
        )
        app.ensure_telegram_inbound_session = lambda _update, _ctx, auto_create=False: asyncio.sleep(
            0,
            result=(None, session),
        )
        app.manager.get = lambda _chat_id, _sid: session

        captured = {"calls": []}

        async def _flush_buffer(_chat_id, _session, _ctx):
            return None

        async def _stage_user_input(_session, payload, _chat_id, _ctx, *, dest=None, image_path=None, image_paths=None):
            _ = image_path, image_paths
            captured["calls"].append({"payload": payload, "dest": dict(dest or {})})

        app._flush_buffer = _flush_buffer
        app._stage_user_input = _stage_user_input

        class _FakeFile:
            def __init__(self, data: bytes):
                self._data = data

            async def download_as_bytearray(self):
                return bytearray(self._data)

        async def _get_file(file_id):
            if file_id == "d1":
                return _FakeFile(b"first-content")
            return _FakeFile(b"second-content")

        async def _send_message(**_kwargs):
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(get_file=_get_file, send_message=_send_message))

        doc1 = types.SimpleNamespace(
            file_id="d1",
            file_name="a.txt",
            file_unique_id="uniq1",
            file_size=20,
            mime_type="text/plain",
        )
        doc2 = types.SimpleNamespace(
            file_id="d2",
            file_name="b.md",
            file_unique_id="uniq2",
            file_size=20,
            mime_type="text/markdown",
        )
        upd1 = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(document=doc1, caption="Сводка", media_group_id="g1"),
        )
        upd2 = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(document=doc2, caption="", media_group_id="g1"),
        )

        await app.on_document(upd1, ctx)
        await app.on_document(upd2, ctx)
        assert captured["calls"] == []
        assert (1, "g1") in app.media_group_documents

        await app._flush_media_groups_for_chat(1)
        await asyncio.sleep(0)

        assert len(captured["calls"]) == 1
        payload = captured["calls"][0]["payload"]
        assert "Сводка" in payload
        assert "===== Вложение: a.txt =====" in payload
        assert "===== Вложение: b.md =====" in payload
        assert "first-content" in payload
        assert "second-content" in payload
        assert (1, "g1") not in app.media_group_documents

    asyncio.run(_run())


def test_on_document_large_text_document_saves_attachment_and_passes_at_path(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        _install_authorized_route(app, session)

        large_data = b"BEGIN-LARGE-CONTENT\n" + b"x" * (5 * 1024)
        captured = _capture_staged_input(app)
        ctx = _document_context(large_data)
        doc = types.SimpleNamespace(
            file_id="d1",
            file_name="large.md",
            file_unique_id="uniq1",
            file_size=None,
            mime_type="text/markdown",
        )

        await app.on_document(_document_update(doc, caption="Смотри файл"), ctx)

        assert len(captured["calls"]) == 1
        payload = captured["calls"][0]["payload"]
        assert "Смотри файл" in payload
        assert "===== Вложение: large.md =====" in payload
        _assert_payload_uses_saved_attachment(payload, tmp_path, "BEGIN-LARGE-CONTENT", large_data)

    asyncio.run(_run())


def test_on_document_media_group_large_text_document_uses_attachment_path(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        _install_authorized_route(app, session)
        app.manager.get = lambda _chat_id, _sid: session

        large_data = b"GROUP-LARGE-CONTENT\n" + b"y" * (5 * 1024)
        captured = _capture_staged_input(app)
        ctx = _document_context(large_data)
        doc = types.SimpleNamespace(
            file_id="d1",
            file_name="large.log",
            file_unique_id="uniq1",
            file_size=999999,
            mime_type="text/plain",
        )

        await app.on_document(_document_update(doc, caption="Группа", media_group_id="g1"), ctx)
        await app._flush_media_groups_for_chat(1)
        await asyncio.sleep(0)

        assert len(captured["calls"]) == 1
        payload = captured["calls"][0]["payload"]
        assert "Группа" in payload
        assert "===== Вложение: large.log =====" in payload
        _assert_payload_uses_saved_attachment(payload, tmp_path, "GROUP-LARGE-CONTENT", large_data)

    asyncio.run(_run())
