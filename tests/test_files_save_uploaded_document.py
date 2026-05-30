import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ThreadModeConfig, ToolConfig
from bot import BotApp


def _build_app(tmp_path, whitelist_chat_ids=None):
    cfg = AppConfig(
        telegram=TelegramConfig(
            token="",
            whitelist_chat_ids=whitelist_chat_ids or [1],
            admlist_chat_ids=whitelist_chat_ids or [1],
        ),
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
        thread_mode=ThreadModeConfig(enabled=False),
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


def test_on_document_saves_file_when_wait_mode_enabled(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        target_dir = tmp_path / "target"
        target_dir.mkdir(parents=True, exist_ok=True)
        ui_key = app.telegram_ui_key(1)

        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(message_id=1)

        class _FakeFile:
            async def download_as_bytearray(self):
                return bytearray(b"hello-from-telegram")

        async def _get_file(_file_id):
            return _FakeFile()

        ctx = types.SimpleNamespace(
            bot=types.SimpleNamespace(send_message=_send_message, get_file=_get_file)
        )

        app._start_files_upload_wait(
            chat_id=1,
            target_dir=str(target_dir),
            root_dir=str(tmp_path),
            context=ctx,
        )

        doc = types.SimpleNamespace(
            file_id="doc-id",
            file_name="note.txt",
            file_unique_id="uniq",
            file_size=20,
            mime_type="text/plain",
        )
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(document=doc, caption=""),
        )

        await app.on_document(update, ctx)

        out_path = target_dir / "note.txt"
        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == "hello-from-telegram"
        assert ui_key not in app.ui_state.files_pending_upload
        assert any("Файл сохранен:" in (c.get("text") or "") for c in calls)

    asyncio.run(_run())


def test_file_wait_timeout_resets_mode_and_notifies(tmp_path, monkeypatch):
    async def _run():
        app = _build_app(tmp_path)
        target_dir = tmp_path / "target"
        target_dir.mkdir(parents=True, exist_ok=True)
        ui_key = app.telegram_ui_key(1)

        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))

        async def _fast_sleep(_delay):
            return None

        monkeypatch.setattr("tg.file_upload_handler.asyncio.sleep", _fast_sleep)

        app._start_files_upload_wait(
            chat_id=1,
            target_dir=str(target_dir),
            root_dir=str(tmp_path),
            context=ctx,
        )

        task = app.ui_state.files_pending_upload_tasks[ui_key]
        await task

        assert ui_key not in app.ui_state.files_pending_upload
        assert ui_key not in app.ui_state.files_pending_upload_tasks
        assert any("Режим сохранения файла сброшен" in (c.get("text") or "") for c in calls)

    asyncio.run(_run())


def test_on_document_without_name_uses_attachment_txt(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        target_dir = tmp_path / "target"
        target_dir.mkdir(parents=True, exist_ok=True)

        async def _send_message(**kwargs):
            return types.SimpleNamespace(message_id=1)

        class _FakeFile:
            async def download_as_bytearray(self):
                return bytearray(b"fallback-name")

        async def _get_file(_file_id):
            return _FakeFile()

        ctx = types.SimpleNamespace(
            bot=types.SimpleNamespace(send_message=_send_message, get_file=_get_file)
        )

        app._start_files_upload_wait(
            chat_id=1,
            target_dir=str(target_dir),
            root_dir=str(tmp_path),
            context=ctx,
        )

        doc = types.SimpleNamespace(
            file_id="doc-id",
            file_name=None,
            file_unique_id="uniq",
            file_size=20,
            mime_type="text/plain",
        )
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(document=doc, caption=""),
        )

        await app.on_document(update, ctx)

        out_path = target_dir / "attachment.txt"
        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == "fallback-name"

    asyncio.run(_run())
