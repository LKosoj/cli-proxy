from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest
from telegram.ext import CommandHandler

from bot import BotApp
from app.services.telegram_transport import TelegramTransportContext
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    TelegramConfig,
    ThreadModeConfig,
    ToolConfig,
)
from session import session_runtime_uid
from tg.wiring import register_handlers


class _FakeFile:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class _FakeBot:
    def __init__(self, thread_ids: list[int]) -> None:
        self._thread_ids = list(thread_ids)
        self.created_topics: list[dict[str, object]] = []
        self.renamed_topics: list[dict[str, object]] = []
        self.sent_messages: list[dict[str, object]] = []
        self.sent_documents: list[dict[str, object]] = []
        self.deleted_threads: set[tuple[int, int]] = set()
        self.create_topic_error: Exception | None = None
        self.file_payloads: dict[str, bytes] = {}

    async def create_forum_topic(self, *, chat_id: int, name: str):
        if self.create_topic_error is not None:
            raise self.create_topic_error
        if not self._thread_ids:
            raise RuntimeError("no fake thread ids left")
        thread_id = int(self._thread_ids.pop(0))
        self.deleted_threads.discard((int(chat_id), thread_id))
        self.created_topics.append(
            {
                "chat_id": int(chat_id),
                "message_thread_id": thread_id,
                "name": str(name),
            }
        )
        return SimpleNamespace(message_thread_id=thread_id)

    async def edit_forum_topic(self, *, chat_id: int, message_thread_id: int, name: str):
        if (int(chat_id), int(message_thread_id)) in self.deleted_threads:
            raise BadRequest("Message thread not found")
        self.renamed_topics.append(
            {
                "chat_id": int(chat_id),
                "message_thread_id": int(message_thread_id),
                "name": str(name),
            }
        )
        return True

    async def send_message(self, **kwargs):
        self.sent_messages.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.sent_messages))

    async def send_document(self, **kwargs):
        self.sent_documents.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.sent_documents))

    async def get_file(self, file_id: str):
        return _FakeFile(self.file_payloads[str(file_id)])


class _FakeApplication:
    def __init__(self) -> None:
        self.bot_data: dict[str, object] = {}
        self.handlers: list[tuple[object, int]] = []

    def add_handler(self, handler, group: int = 0) -> None:
        self.handlers.append((handler, int(group)))


def _build_config(tmp_path, *, intent: str) -> AppConfig:
    workdir = tmp_path / f"workdir_{intent}"
    runtime = tmp_path / f"runtime_{intent}"
    logs = tmp_path / f"logs_{intent}"
    workdir.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="token", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
                image_cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(workdir),
            state_path=str(runtime / "state.json"),
            toolhelp_path=str(runtime / "toolhelp.json"),
            log_path=str(logs / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / f"config_{intent}.yaml"),
        miniapp=MiniAppConfig(),
        thread_mode=ThreadModeConfig(
            enabled=True,
            mode="group",
            topics_chat_id=-100777000111,
            topic_title_prefix="cli",
        ),
    )


def _make_update(
    *,
    chat_id: int,
    message_thread_id: int | None,
    text: str | None = None,
    document=None,
    photo=None,
    caption: str = "",
    media_group_id: str | None = None,
    user_id: int = 1,
):
    message = SimpleNamespace(
        text=text,
        document=document,
        photo=photo,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
        video_note=None,
        caption=caption,
        media_group_id=media_group_id,
        message_thread_id=message_thread_id,
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        effective_user=SimpleNamespace(id=int(user_id)),
        effective_message=message,
        message=message,
    )


async def _build_threaded_app(tmp_path, *, intent: str):
    cfg = _build_config(tmp_path, intent=intent)
    fake_bot = _FakeBot([101, 202, 303, 404])
    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    workdir_one = tmp_path / f"project_one_{intent}"
    workdir_two = tmp_path / f"project_two_{intent}"
    workdir_one.mkdir()
    workdir_two.mkdir()
    session_one, err_one = await app.session_creation_service.create_session(
        1,
        "dummy",
        str(workdir_one),
        bot=fake_bot,
    )
    session_two, err_two = await app.session_creation_service.create_session(
        1,
        "dummy",
        str(workdir_two),
        bot=fake_bot,
    )
    assert err_one is None
    assert err_two is None
    return app, fake_bot, session_one, session_two


@pytest.mark.asyncio
async def test_thread_command_status_routes_to_mapped_session(tmp_path) -> None:
    app, fake_bot, session_one, session_two = await _build_threaded_app(tmp_path, intent="status")
    try:
        ctx = SimpleNamespace(args=[], bot=fake_bot)
        await app.handlers.cmd_status(
            _make_update(chat_id=-100777000111, message_thread_id=101, text="/status"),
            ctx,
        )
        await app.handlers.cmd_status(
            _make_update(chat_id=-100777000111, message_thread_id=202, text="/status"),
            ctx,
        )

        assert len(fake_bot.sent_messages) >= 2
        first, second = fake_bot.sent_messages[-2:]
        assert first["message_thread_id"] == 101
        assert second["message_thread_id"] == 202
        assert session_one.id in str(first["text"])
        assert session_two.id in str(second["text"])
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_thread_interrupt_stops_only_current_topic_and_clears_auto_continue_state(tmp_path) -> None:
    app, fake_bot, session_one, session_two = await _build_threaded_app(tmp_path, intent="interrupt")
    topic_chat_id = -100777000111
    interrupted: list[str] = []

    async def _sleeper() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    try:
        session_one.interrupt = lambda: interrupted.append("s1")  # type: ignore[method-assign]
        session_two.interrupt = lambda: interrupted.append("s2")  # type: ignore[method-assign]
        uid_one = session_runtime_uid(session_one)
        uid_two = session_runtime_uid(session_two)
        app.mode_tasks.create(session_uid=uid_one, mode_id="agent", coro=_sleeper(), name="topic-101")
        app.mode_tasks.create(session_uid=uid_two, mode_id="agent", coro=_sleeper(), name="topic-202")

        session_one.queue.append({"text": "queued-one", "dest": {"kind": "telegram", "chat_id": topic_chat_id, "message_thread_id": 101}})
        session_two.queue.append({"text": "queued-two", "dest": {"kind": "telegram", "chat_id": topic_chat_id, "message_thread_id": 202}})

        ctx = SimpleNamespace(args=[], bot=fake_bot)
        await app.message_buffer_service.buffer_or_send(session_one, "buffer-one", chat_id=topic_chat_id, context=ctx, user_id=1)
        await app.message_buffer_service.buffer_or_send(session_two, "buffer-two", chat_id=topic_chat_id, context=ctx, user_id=1)

        key_one = app.message_buffer_service._scope_buffer_key(session_one, topic_chat_id)
        key_two = app.message_buffer_service._scope_buffer_key(session_two, topic_chat_id)
        task_one = app.buffer_tasks[key_one]
        task_two = app.buffer_tasks[key_two]

        ui_key_one = app.telegram_ui_key(topic_chat_id, 101)
        ui_key_two = app.telegram_ui_key(topic_chat_id, 202)
        app.ui_state.pending_questions["q1"] = {
            "options": ["A", "B"],
            "chat_id": topic_chat_id,
            "message_thread_id": 101,
            "session_id": session_one.id,
            "awaiting_custom": False,
            "allow_custom": True,
            "created_at": 0.0,
        }
        app.ui_state.pending_questions["q2"] = {
            "options": ["A", "B"],
            "chat_id": topic_chat_id,
            "message_thread_id": 202,
            "session_id": session_two.id,
            "awaiting_custom": False,
            "allow_custom": True,
            "created_at": 0.0,
        }
        app.ui_state.active_ask_question_by_chat[ui_key_one] = "q1"
        app.ui_state.active_ask_question_by_chat[ui_key_two] = "q2"
        media_key_one = (topic_chat_id, "mg-101")
        media_key_two = (topic_chat_id, "mg-202")
        app.ui_state.media_group_images[media_key_one] = {
            "chat_id": topic_chat_id,
            "session_id": session_one.id,
            "session_uid": uid_one,
            "owner_chat_id": 1,
            "paths": ["one.png"],
            "caption": "",
            "context": ctx,
        }
        app.ui_state.media_group_documents[media_key_two] = {
            "chat_id": topic_chat_id,
            "session_id": session_two.id,
            "session_uid": uid_two,
            "owner_chat_id": 1,
            "blocks": ["doc"],
            "caption": "",
            "context": ctx,
        }
        app.ui_state.media_group_tasks[media_key_one] = asyncio.create_task(asyncio.sleep(3600))
        app.ui_state.media_group_document_tasks[media_key_two] = asyncio.create_task(asyncio.sleep(3600))
        media_task_one = app.ui_state.media_group_tasks[media_key_one]
        media_task_two = app.ui_state.media_group_document_tasks[media_key_two]

        await app.handlers.cmd_interrupt(
            _make_update(chat_id=topic_chat_id, message_thread_id=101, text="/interrupt"),
            ctx,
        )
        await asyncio.sleep(0)

        assert interrupted == ["s1"]
        assert app.mode_tasks.list(session_uid=uid_one, mode_id="agent") == []
        assert app.mode_tasks.list(session_uid=uid_two, mode_id="agent") == ["topic-202"]
        assert list(session_one.queue) == []
        assert [item["text"] for item in list(session_two.queue)] == ["queued-two"]
        assert key_one not in app.message_buffer
        assert app.message_buffer.get(key_two) == ["buffer-two"]
        assert key_one not in app.buffer_tasks
        assert key_two in app.buffer_tasks
        assert task_one.cancelled() or task_one.done()
        assert not task_two.done()
        assert media_key_one not in app.ui_state.media_group_images
        assert media_key_two in app.ui_state.media_group_documents
        assert media_key_one not in app.ui_state.media_group_tasks
        assert media_key_two in app.ui_state.media_group_document_tasks
        assert media_task_one.cancelled() or media_task_one.done()
        assert not media_task_two.done()
        assert "q1" not in app.ui_state.pending_questions
        assert "q2" in app.ui_state.pending_questions
        assert ui_key_one not in app.ui_state.active_ask_question_by_chat
        assert app.ui_state.active_ask_question_by_chat[ui_key_two] == "q2"

        assert fake_bot.sent_messages
        last = fake_bot.sent_messages[-1]
        assert last["chat_id"] == topic_chat_id
        assert last["message_thread_id"] == 101
        assert last["text"] in {
            "Прерывание отправлено.",
            "Текущая работа прервана. Сессия освобождена.",
        }
    finally:
        app.message_buffer_service.clear_buffer(session_two, topic_chat_id)
        await asyncio.sleep(0)
        if media_key_two in app.ui_state.media_group_document_tasks:
            app.ui_state.media_group_document_tasks[media_key_two].cancel()
        await app.mode_tasks.cancel_session(session_uid=session_runtime_uid(session_two), timeout_s=0.5)
        app.shutdown_html_process_pool()


def test_thread_command_sessions_replies_in_same_topic(tmp_path) -> None:
    async def _run() -> None:
        app, fake_bot, _session_one, _session_two = await _build_threaded_app(tmp_path, intent="sessions")
        try:
            await app.handlers.cmd_sessions(
                _make_update(chat_id=-100777000111, message_thread_id=202, text="/sessions"),
                SimpleNamespace(args=[], bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == -100777000111
            assert last["message_thread_id"] == 202
            assert "Активная сессия" in str(last["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_thread_command_sessions_user_falls_back_from_orphan_topic_to_sessions_menu(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_orphan_sessions")
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555])
        fake_app = _FakeApplication()
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        register_handlers(app=fake_app, bot_app=app, config=cfg)

        try:
            sessions_handler = next(
                handler
                for handler, _group in fake_app.handlers
                if isinstance(handler, CommandHandler) and "sessions" in getattr(handler, "commands", ())
            )
            await sessions_handler.callback(
                _make_update(chat_id=-100777000111, message_thread_id=999, text="/sessions", user_id=2),
                SimpleNamespace(args=[], bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == -100777000111
            assert "message_thread_id" not in last
            assert "Активных сессий нет." in str(last["text"])
            assert "Этот topic не связан" not in str(last["text"])
            keyboard = last["reply_markup"]
            callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
            assert callbacks == ["sess_new", "sess_close_menu"]
            assert fake_bot.created_topics == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_thread_command_sessions_user_falls_back_from_any_thread_to_sessions_menu(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_thread_sessions")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555])
        fake_app = _FakeApplication()
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        register_handlers(app=fake_app, bot_app=app, config=cfg)

        try:
            sessions_handler = next(
                handler
                for handler, _group in fake_app.handlers
                if isinstance(handler, CommandHandler) and "sessions" in getattr(handler, "commands", ())
            )
            update = _make_update(chat_id=2, message_thread_id=999, text="/sessions", user_id=2)

            await app.on_pre_command(update, SimpleNamespace(args=[], bot=fake_bot))
            await sessions_handler.callback(update, SimpleNamespace(args=[], bot=fake_bot))

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert "message_thread_id" not in last
            assert "Активных сессий нет." in str(last["text"])
            assert "Этот topic не связан" not in str(last["text"])
            keyboard = last["reply_markup"]
            callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
            assert callbacks == ["sess_new", "sess_close_menu"]
            assert fake_bot.created_topics == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_user_sessions_command_with_existing_session_opens_active_overview(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_existing_session_sessions")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.admlist_chat_ids = [1]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555, 777])
        fake_app = _FakeApplication()
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        register_handlers(app=fake_app, bot_app=app, config=cfg)

        try:
            session, err = await app.session_creation_service.create_session(
                2,
                "dummy",
                str(tmp_path / "user_project"),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None
            assert session.conversation_scope.message_thread_id == 555

            sessions_handler = next(
                handler
                for handler, _group in fake_app.handlers
                if isinstance(handler, CommandHandler) and "sessions" in getattr(handler, "commands", ())
            )
            update = _make_update(chat_id=2, message_thread_id=555, text="/sessions", user_id=2)

            await app.on_pre_command(update, SimpleNamespace(args=[], bot=fake_bot))
            await sessions_handler.callback(update, SimpleNamespace(args=[], bot=fake_bot))

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert last["message_thread_id"] == 555
            assert "Активная сессия:" in str(last["text"])
            keyboard = last["reply_markup"]
            callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
            assert f"sess_status:{session.id}" in callbacks
            assert f"sess_reset:{session.id}" in callbacks
            assert not any(str(item).startswith("user_project_menu") for item in callbacks)
            assert "sess_new" in callbacks
            assert not any(str(item).startswith("user_project_pick_new:") for item in callbacks)
            assert fake_bot.created_topics == [
                {
                    "chat_id": 2,
                    "message_thread_id": 555,
                    "name": f"cli | {session.id} | {session.name}",
                }
            ]
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_user_sessions_command_without_scope_session_matches_admin_overview(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_existing_session_without_scope")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.admlist_chat_ids = [1]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555, 777])
        fake_app = _FakeApplication()
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        register_handlers(app=fake_app, bot_app=app, config=cfg)

        try:
            session, err = await app.session_creation_service.create_session(
                2,
                "dummy",
                str(tmp_path / "user_project"),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None
            assert session.conversation_scope.message_thread_id == 555

            sessions_handler = next(
                handler
                for handler, _group in fake_app.handlers
                if isinstance(handler, CommandHandler) and "sessions" in getattr(handler, "commands", ())
            )
            update = _make_update(chat_id=2, message_thread_id=None, text="/sessions", user_id=2)

            await app.on_pre_command(update, SimpleNamespace(args=[], bot=fake_bot))
            await sessions_handler.callback(update, SimpleNamespace(args=[], bot=fake_bot))

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert "message_thread_id" not in last
            assert "Текущая scope-bound сессия не определена." in str(last["text"])
            keyboard = last["reply_markup"]
            callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
            assert callbacks == ["sess_list", "sess_new", "sess_close_menu"]
            assert fake_bot.created_topics == [
                {
                    "chat_id": 2,
                    "message_thread_id": 555,
                    "name": f"cli | {session.id} | {session.name}",
                }
            ]
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_user_send_without_session_requires_creation_not_topic(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_send_without_session")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.admlist_chat_ids = [1]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        try:
            await app.handlers.cmd_send(
                _make_update(chat_id=2, message_thread_id=None, text="/send ping", user_id=2),
                SimpleNamespace(args=["ping"], bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert last.get("message_thread_id") is None
            assert str(last["text"]) == "Сначала создайте сессию."
            assert fake_bot.created_topics == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_user_send_without_scope_session_requires_existing_topics(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_send_without_scope")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.admlist_chat_ids = [1]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555, 777])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        try:
            session, err = await app.session_creation_service.create_session(
                2,
                "dummy",
                str(tmp_path / "user_project"),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None

            fake_bot.sent_messages.clear()
            await app.handlers.cmd_send(
                _make_update(chat_id=2, message_thread_id=None, text="/send ping", user_id=2),
                SimpleNamespace(args=["ping"], bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert last.get("message_thread_id") is None
            assert str(last["text"]) == "Используйте топики существующих сессий."
            assert fake_bot.created_topics == [
                {
                    "chat_id": 2,
                    "message_thread_id": 555,
                    "name": f"cli | {session.id} | {session.name}",
                }
            ]
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_user_text_without_session_requires_creation_not_topic(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_text_without_session")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.admlist_chat_ids = [1]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        try:
            await app.on_message(
                _make_update(chat_id=2, message_thread_id=None, text="hello", user_id=2),
                SimpleNamespace(bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert last.get("message_thread_id") is None
            assert str(last["text"]) == "Сначала создайте сессию."
            assert fake_bot.created_topics == []
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_user_text_without_scope_session_requires_existing_topics(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="user_private_text_without_scope")
        cfg.thread_mode.mode = "private"
        cfg.thread_mode.topics_chat_id = None
        cfg.telegram.whitelist_chat_ids = [1, 2]
        cfg.telegram.admlist_chat_ids = [1]
        cfg.telegram.user_workdirs = {2: [str(tmp_path / "user_project")]}
        (tmp_path / "user_project").mkdir()

        app = BotApp(cfg)
        fake_bot = _FakeBot([555, 777])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

        try:
            session, err = await app.session_creation_service.create_session(
                2,
                "dummy",
                str(tmp_path / "user_project"),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None

            fake_bot.sent_messages.clear()
            await app.on_message(
                _make_update(chat_id=2, message_thread_id=None, text="hello", user_id=2),
                SimpleNamespace(bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == 2
            assert last.get("message_thread_id") is None
            assert str(last["text"]) == "Используйте топики существующих сессий."
            assert fake_bot.created_topics == [
                {
                    "chat_id": 2,
                    "message_thread_id": 555,
                    "name": f"cli | {session.id} | {session.name}",
                }
            ]
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_thread_command_new_menu_replies_in_same_topic(tmp_path) -> None:
    async def _run() -> None:
        app, fake_bot, _session_one, _session_two = await _build_threaded_app(tmp_path, intent="new_menu")
        try:
            await app.handlers.cmd_new(
                _make_update(chat_id=-100777000111, message_thread_id=101, text="/new"),
                SimpleNamespace(args=[], bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == -100777000111
            assert last["message_thread_id"] == 101
            assert "Выберите инструмент" in str(last["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_thread_command_send_preserves_session_and_thread_dest(tmp_path) -> None:
    app, fake_bot, _session_one, session_two = await _build_threaded_app(tmp_path, intent="send")
    captured: list[dict[str, object]] = []

    async def _fake_handle_cli_input(session, text, chat_id, context, dest=None, image_path=None, image_paths=None):
        captured.append(
            {
                "session_id": str(session.id),
                "text": str(text),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
                "image_path": image_path,
                "image_paths": list(image_paths or []),
            }
        )

    app._handle_cli_input = _fake_handle_cli_input
    try:
        await app.handlers.cmd_send(
            _make_update(chat_id=-100777000111, message_thread_id=202, text="/send ping"),
            SimpleNamespace(args=["ping"], bot=fake_bot),
        )

        assert captured == [
            {
                "session_id": session_two.id,
                "text": "ping",
                "chat_id": -100777000111,
                "dest": {
                    "kind": "telegram",
                    "chat_id": -100777000111,
                    "user_id": 1,
                    "message_thread_id": 202,
                },
                "image_path": None,
                "image_paths": [],
            }
        ]
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_thread_text_messages_from_different_topics_stay_isolated(tmp_path) -> None:
    app, fake_bot, session_one, session_two = await _build_threaded_app(tmp_path, intent="text_isolation")
    captured: list[dict[str, object]] = []

    async def _fake_stage_user_input(session, text, chat_id, context, *, dest=None, image_path=None, image_paths=None):
        _ = image_path, image_paths
        captured.append(
            {
                "session_id": str(session.id),
                "text": str(text),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
            }
        )

    app._stage_user_input = _fake_stage_user_input
    ctx = SimpleNamespace(bot=fake_bot)
    try:
        await app.on_message(
            _make_update(chat_id=-100777000111, message_thread_id=101, text="alpha", user_id=11),
            ctx,
        )
        await app.on_message(
            _make_update(chat_id=-100777000111, message_thread_id=202, text="beta", user_id=22),
            ctx,
        )

        assert captured == []
        await app._flush_buffer(-100777000111, session_one, ctx)
        await app._flush_buffer(-100777000111, session_two, ctx)
        await asyncio.sleep(0)

        assert captured == [
            {
                "session_id": session_one.id,
                "text": "alpha",
                "chat_id": -100777000111,
                "dest": {
                    "kind": "telegram",
                    "chat_id": -100777000111,
                    "user_id": 11,
                    "message_thread_id": 101,
                },
            },
            {
                "session_id": session_two.id,
                "text": "beta",
                "chat_id": -100777000111,
                "dest": {
                    "kind": "telegram",
                    "chat_id": -100777000111,
                    "user_id": 22,
                    "message_thread_id": 202,
                },
            },
        ]
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_thread_document_routes_to_mapped_session(tmp_path) -> None:
    app, fake_bot, session_one, _session_two = await _build_threaded_app(tmp_path, intent="document")
    fake_bot.file_payloads["doc-101"] = b"hello-from-topic-101"
    captured: list[dict[str, object]] = []

    async def _fake_stage_user_input(session, text, chat_id, context, *, dest=None, image_path=None, image_paths=None):
        _ = image_path, image_paths
        captured.append(
            {
                "session_id": str(session.id),
                "text": str(text),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
            }
        )

    app._stage_user_input = _fake_stage_user_input
    document = SimpleNamespace(
        file_id="doc-101",
        file_name="note.txt",
        file_unique_id="uniq-doc-101",
        file_size=128,
        mime_type="text/plain",
    )
    try:
        await app.on_document(
            _make_update(
                chat_id=-100777000111,
                message_thread_id=101,
                document=document,
                caption="caption-101",
            ),
            SimpleNamespace(bot=fake_bot),
        )

        assert len(captured) == 1
        call = captured[0]
        assert call["session_id"] == session_one.id
        assert call["chat_id"] == -100777000111
        assert call["dest"]["message_thread_id"] == 101
        assert "caption-101" in call["text"]
        assert "hello-from-topic-101" in call["text"]
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_thread_photo_routes_to_mapped_session(tmp_path) -> None:
    app, fake_bot, _session_one, session_two = await _build_threaded_app(tmp_path, intent="photo")
    fake_bot.file_payloads["photo-202"] = b"jpeg-binary"
    captured: list[dict[str, object]] = []

    async def _fake_stage_user_input(session, text, chat_id, context, *, dest=None, image_path=None, image_paths=None):
        captured.append(
            {
                "session_id": str(session.id),
                "prompt": str(text),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
                "image_path": image_path,
                "image_paths": list(image_paths or []),
            }
        )
        return None

    app._stage_user_input = _fake_stage_user_input
    photo = SimpleNamespace(
        file_id="photo-202",
        file_unique_id="uniq-photo-202",
        file_size=128,
    )
    try:
        await app.on_photo(
            _make_update(
                chat_id=-100777000111,
                message_thread_id=202,
                photo=[photo],
                caption="photo-caption",
            ),
            SimpleNamespace(bot=fake_bot),
        )
        await asyncio.sleep(0)

        assert len(captured) == 1
        call = captured[0]
        assert call["session_id"] == session_two.id
        assert call["prompt"] == "photo-caption"
        assert call["chat_id"] == -100777000111
        assert call["dest"] == {}
        assert call["image_path"] is None
        assert len(call["image_paths"]) == 1
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_unknown_thread_returns_clear_error(tmp_path) -> None:
    app, fake_bot, _session_one, _session_two = await _build_threaded_app(tmp_path, intent="unknown_thread")
    captured: list[str] = []

    async def _fake_handle_user_input(*_args, **_kwargs):
        captured.append("called")

    app._handle_user_input = _fake_handle_user_input
    try:
        await app.on_message(
            _make_update(chat_id=-100777000111, message_thread_id=999, text="who am i"),
            SimpleNamespace(bot=fake_bot),
        )

        assert captured == []
        assert fake_bot.sent_messages
        last = fake_bot.sent_messages[-1]
        assert last["message_thread_id"] == 999
        assert "не связан" in str(last["text"]).lower()
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_messaging_service_sends_text_and_document_to_same_topic(tmp_path) -> None:
    app, fake_bot, session_one, _session_two = await _build_threaded_app(tmp_path, intent="outbound_messaging")
    try:
        dest = app.build_telegram_reply_dest(session_one, -100777000111, user_id=11)
        transport_context = app.build_telegram_transport_context(
            SimpleNamespace(bot=fake_bot),
            session=session_one,
            chat_id=-100777000111,
            dest=dest,
            user_id=11,
        )
        messaging = app._mode_messaging_factory(transport_context)

        await messaging.send_plain_text(-100777000111, "threaded reply")
        await messaging.send_doc(-100777000111, "fake-document")

        assert fake_bot.sent_messages
        assert fake_bot.sent_documents
        assert fake_bot.sent_messages[-1]["chat_id"] == -100777000111
        assert fake_bot.sent_messages[-1]["message_thread_id"] == 101
        assert fake_bot.sent_documents[-1]["chat_id"] == -100777000111
        assert fake_bot.sent_documents[-1]["message_thread_id"] == 101
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_telegram_transport_rejects_missing_thread_id_for_thread_bound_context(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="missing_thread_contract")
    app = BotApp(cfg)
    fake_bot = _FakeBot([101])
    transport_context = TelegramTransportContext(
        raw_context=SimpleNamespace(bot=fake_bot),
        chat_id=-100777000111,
        message_thread_id=None,
        require_thread_id=True,
        session_uid="thread:-100777000111:missing",
    )
    try:
        message_result = await app.transport_service.send_message(
            transport_context,
            text="must fail",
            md2=False,
        )
        document_result = await app.transport_service.send_document(
            transport_context,
            document="fake-document",
        )

        assert message_result is None
        assert document_result is False
        assert fake_bot.sent_messages == []
        assert fake_bot.sent_documents == []
        assert "message_thread_id is required" in str(app._last_delivery_error)
    finally:
        app.shutdown_html_process_pool()
