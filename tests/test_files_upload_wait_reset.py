import asyncio
import types

from bot import BotApp
from tg.callbacks import CallbackHandler
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ThreadModeConfig, ToolConfig


def _build_app(tmp_path):
    cfg = AppConfig(
        # These tests validate internal reset logic and should not be blocked by ACL.
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
        thread_mode=ThreadModeConfig(enabled=False),
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


class _FakeMessage:
    def __init__(self, chat_id=1, message_id=1):
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.message = _FakeMessage()
        self.edits = []

    async def answer(self):
        return None

    async def edit_message_text(self, text: str, reply_markup=None):
        self.edits.append({"text": text, "reply_markup": reply_markup})


def test_file_nav_cancel_resets_upload_and_rename_wait(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        session = types.SimpleNamespace(workdir=str(tmp_path))
        app.resolve_telegram_callback_scope = lambda _query: (1, None, 1, session)

        sleeper = asyncio.create_task(asyncio.sleep(3600))
        rename_sleeper = asyncio.create_task(asyncio.sleep(3600))
        app.ui_state.files_pending_upload[ui_key] = {"dir": str(tmp_path), "root": str(tmp_path), "expires_at": 9999999999}
        app.ui_state.files_pending_upload_tasks[ui_key] = sleeper
        app.ui_state.files_pending_rename[ui_key] = {"path": str(tmp_path / "a.txt"), "root": str(tmp_path), "expires_at": 9999999999}
        app.ui_state.files_pending_rename_tasks[ui_key] = rename_sleeper

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("file_nav:cancel"))
        await handler.handle_callback(update, context=object())
        await asyncio.sleep(0)

        assert ui_key not in app.ui_state.files_pending_upload
        assert ui_key not in app.ui_state.files_pending_upload_tasks
        assert ui_key not in app.ui_state.files_pending_rename
        assert ui_key not in app.ui_state.files_pending_rename_tasks
        assert sleeper.cancelled()
        assert rename_sleeper.cancelled()

    asyncio.run(_run())


def test_on_session_change_resets_upload_and_rename_wait(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        sleeper = asyncio.create_task(asyncio.sleep(3600))
        rename_sleeper = asyncio.create_task(asyncio.sleep(3600))
        app.ui_state.files_pending_upload[ui_key] = {"dir": str(tmp_path), "root": str(tmp_path), "expires_at": 9999999999}
        app.ui_state.files_pending_upload_tasks[ui_key] = sleeper
        app.ui_state.files_pending_rename[ui_key] = {"path": str(tmp_path / "a.txt"), "root": str(tmp_path), "expires_at": 9999999999}
        app.ui_state.files_pending_rename_tasks[ui_key] = rename_sleeper

        app._on_session_change(1)
        await asyncio.sleep(0)

        assert ui_key not in app.ui_state.files_pending_upload
        assert ui_key not in app.ui_state.files_pending_upload_tasks
        assert ui_key not in app.ui_state.files_pending_rename
        assert ui_key not in app.ui_state.files_pending_rename_tasks
        assert sleeper.cancelled()
        assert rename_sleeper.cancelled()

    asyncio.run(_run())


def test_rename_wait_timeout_resets_mode_and_notifies(tmp_path, monkeypatch):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        source = tmp_path / "old.txt"
        source.write_text("x", encoding="utf-8")
        calls = []

        async def _send_message(**kwargs):
            calls.append(dict(kwargs))
            return types.SimpleNamespace(message_id=1)

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))

        async def _fast_sleep(_delay):
            return None

        monkeypatch.setattr("tg.file_upload_handler.asyncio.sleep", _fast_sleep)

        app._start_files_rename_wait(
            chat_id=1,
            source_path=str(source),
            root_dir=str(tmp_path),
            context=ctx,
        )
        task = app.ui_state.files_pending_rename_tasks[ui_key]
        await task
        assert ui_key not in app.ui_state.files_pending_rename
        assert ui_key not in app.ui_state.files_pending_rename_tasks
        assert any("Режим переименования отменен" in (c.get("text") or "") for c in calls)

    asyncio.run(_run())


def test_pre_command_resets_rename_wait(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        rename_sleeper = asyncio.create_task(asyncio.sleep(3600))
        app.ui_state.files_pending_rename[ui_key] = {"path": str(tmp_path / "a.txt"), "root": str(tmp_path), "expires_at": 9999999999}
        app.ui_state.files_pending_rename_tasks[ui_key] = rename_sleeper
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(text="/files"),
        )
        await app.on_pre_command(update, context=object())
        await asyncio.sleep(0)
        assert ui_key not in app.ui_state.files_pending_rename
        assert ui_key not in app.ui_state.files_pending_rename_tasks
        assert rename_sleeper.cancelled()

    asyncio.run(_run())


def test_on_document_resets_rename_wait(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        rename_sleeper = asyncio.create_task(asyncio.sleep(3600))
        app.ui_state.files_pending_rename[ui_key] = {"path": str(tmp_path / "a.txt"), "root": str(tmp_path), "expires_at": 9999999999}
        app.ui_state.files_pending_rename_tasks[ui_key] = rename_sleeper

        async def _send_message(**kwargs):
            return types.SimpleNamespace(message_id=1)

        class _FakeFile:
            async def download_as_bytearray(self):
                return bytearray(b"hello")

        async def _get_file(_file_id):
            return _FakeFile()

        app.ensure_active_session = lambda _chat_id, _ctx: asyncio.sleep(0, result=None)
        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message, get_file=_get_file))
        doc = types.SimpleNamespace(
            file_id="doc-id",
            file_name="x.txt",
            file_unique_id="uniq",
            file_size=20,
            mime_type="text/plain",
        )
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(document=doc, caption=""),
        )
        await app.on_document(update, ctx)
        await asyncio.sleep(0)
        assert ui_key not in app.ui_state.files_pending_rename
        assert ui_key not in app.ui_state.files_pending_rename_tasks
        assert rename_sleeper.cancelled()

    asyncio.run(_run())
