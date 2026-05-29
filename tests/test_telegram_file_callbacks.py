import asyncio
from types import SimpleNamespace

from app.security import SecurityFacade
from app.services.session_files_service import SessionFilesService
from app.services.telegram_ui_scope import TelegramUiKey
from tg.callback_actions.files import FileActionsMixin


class _FakeQuery:
    def __init__(self) -> None:
        self.message = SimpleNamespace(chat_id=1, message_id=10)
        self.edits = []


class _FileCallbackHarness(FileActionsMixin):
    def __init__(self, bot_app) -> None:
        self.bot_app = bot_app

    async def _edit_msg(self, _context, query, text, *, reply_markup=None, md2=True):
        query.edits.append({"text": text, "reply_markup": reply_markup, "md2": md2})
        return True


def _build_app(tmp_path):
    session = SimpleNamespace(id="s1", chat_id=1, workdir=str(tmp_path))
    ui_key = TelegramUiKey.from_parts(1)
    ui_state = SimpleNamespace(
        files_dir={ui_key: str(tmp_path)},
        files_entries={},
        files_page={ui_key: 0},
        files_pending_delete={},
        files_pending_upload={},
        files_pending_rename={},
    )
    manager = SimpleNamespace(
        get_by_uid=lambda uid: session if uid == "1:s1" else None,
        get=lambda chat_id, session_id: session if int(chat_id) == 1 and session_id == "s1" else None,
    )
    app = SimpleNamespace(
        config=SimpleNamespace(
            miniapp=SimpleNamespace(max_edit_file_size_kb=5120, enable_delete=True),
            path=str(tmp_path / "config.yaml"),
        ),
        manager=manager,
        security=SecurityFacade(),
        ui_state=ui_state,
        sent_documents=[],
        menus=[],
    )
    (tmp_path / "config.yaml").write_text("tools: {}\n", encoding="utf-8")
    app.session_files_service = SessionFilesService(app)
    app.telegram_ui_key_from_query = lambda _query: ui_key
    app.telegram_ui_key = lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)
    app.resolve_telegram_callback_scope = lambda _query: (1, None, 1, session)

    async def _send_document(_context, *, document, **kwargs):
        app.sent_documents.append(
            {
                "content": document.read(),
                "name": getattr(document, "name", ""),
                "kwargs": dict(kwargs),
            }
        )
        return True

    async def _send_files_menu(chat_id, menu_session, context, edit_message=None, message_thread_id=None):
        app.menus.append(
            {
                "chat_id": chat_id,
                "session": menu_session,
                "edit_message": edit_message,
                "message_thread_id": message_thread_id,
            }
        )

    app._send_document = _send_document
    app._send_files_menu = _send_files_menu
    app._start_files_upload_wait = lambda *_args, **_kwargs: None
    app._stop_files_upload_wait = lambda *_args, **_kwargs: None
    app._start_files_rename_wait = lambda *_args, **_kwargs: None
    app._stop_files_rename_wait = lambda *_args, **_kwargs: None
    return app, ui_key


def test_file_pick_downloads_via_shared_session_files_service(tmp_path):
    async def _run():
        app, ui_key = _build_app(tmp_path)
        file_path = tmp_path / "artifact.bin"
        file_path.write_bytes(b"\x00\x01telegram-binary")
        app.ui_state.files_entries[ui_key] = [{"path": str(file_path), "rel_path": "artifact.bin", "is_dir": False}]

        query = _FakeQuery()
        handled = await _FileCallbackHarness(app)._cb_file_pick(
            data="file_pick:0",
            chat_id=1,
            query=query,
            context=object(),
        )

        assert handled is True
        assert query.edits[0]["text"] == "Отправляю файл: artifact.bin"
        assert app.sent_documents == [
            {
                "content": b"\x00\x01telegram-binary",
                "name": "artifact.bin",
                "kwargs": {"chat_id": 1},
            }
        ]

    asyncio.run(_run())


def test_file_delete_confirmation_keeps_callback_data_and_uses_recursive_service_delete(tmp_path):
    async def _run():
        app, ui_key = _build_app(tmp_path)
        target_dir = tmp_path / "nested"
        target_dir.mkdir()
        (target_dir / "child.txt").write_text("content", encoding="utf-8")
        app.ui_state.files_entries[ui_key] = [{"path": str(target_dir), "rel_path": "nested", "is_dir": True}]

        harness = _FileCallbackHarness(app)
        confirm_query = _FakeQuery()
        handled = await harness._cb_file_del(data="file_del:0", chat_id=1, query=confirm_query, context=object())

        assert handled is True
        assert confirm_query.edits[-1]["text"] == "Удалить nested? Подтвердите:"
        buttons = confirm_query.edits[-1]["reply_markup"].inline_keyboard[0]
        assert [button.callback_data for button in buttons] == ["file_del_confirm", "file_del_cancel"]

        delete_query = _FakeQuery()
        handled = await harness._cb_file_del_confirm(
            data="file_del_confirm",
            chat_id=1,
            query=delete_query,
            context=object(),
        )

        assert handled is True
        assert not target_dir.exists()
        assert delete_query.edits[0]["text"] == "Удалено."
        assert len(app.menus) == 1

    asyncio.run(_run())
