import asyncio
import types

from bot import BotApp
from tg.callbacks import CallbackHandler
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ThreadModeConfig, ToolConfig


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
        thread_mode=ThreadModeConfig(enabled=False),
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


def _msg(text: str):
    return types.SimpleNamespace(
        text=text,
        document=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
        video_note=None,
    )


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.message = types.SimpleNamespace(chat_id=1, message_id=1)
        self.edits = []

    async def answer(self):
        return None

    async def edit_message_text(self, text: str, reply_markup=None):
        self.edits.append({"text": text, "reply_markup": reply_markup})


def test_process_message_renames_file_and_exits_mode(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        source = tmp_path / "old.txt"
        source.write_text("hello", encoding="utf-8")
        sent = []

        async def _send_message(**kwargs):
            sent.append(dict(kwargs))
            return types.SimpleNamespace(message_id=1)

        async def _send_files_menu(_chat_id, _session, _context, edit_message=None, message_thread_id=None):
            return None

        app._send_files_menu = _send_files_menu
        app.manager.active = lambda _chat_id: types.SimpleNamespace(workdir=str(tmp_path))

        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        app._start_files_rename_wait(
            chat_id=1,
            source_path=str(source),
            root_dir=str(tmp_path),
            context=ctx,
        )
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=1), message=_msg("new.txt"))
        await app.message_processor.process_message(update, ctx)

        assert not source.exists()
        assert (tmp_path / "new.txt").exists()
        assert ui_key not in app.ui_state.files_pending_rename
        assert any("Переименовано:" in (x.get("text") or "") for x in sent)

    asyncio.run(_run())


def test_file_rename_cancel_button_clears_mode(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        ui_key = app.telegram_ui_key(1)
        source = tmp_path / "old.txt"
        source.write_text("hello", encoding="utf-8")
        app.manager.active = lambda _chat_id: types.SimpleNamespace(workdir=str(tmp_path))

        async def _send_message(**kwargs):
            return types.SimpleNamespace(message_id=1)

        async def _send_files_menu(_chat_id, _session, _context, edit_message=None, message_thread_id=None):
            return None

        app._send_files_menu = _send_files_menu
        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=_send_message))
        app._start_files_rename_wait(
            chat_id=1,
            source_path=str(source),
            root_dir=str(tmp_path),
            context=ctx,
        )
        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("file_rename_cancel"))
        await handler.handle_callback(update, context=ctx)

        assert ui_key not in app.ui_state.files_pending_rename
        assert ui_key not in app.ui_state.files_pending_rename_tasks

    asyncio.run(_run())
