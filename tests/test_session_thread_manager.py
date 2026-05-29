from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from app.services.lifecycle_service import build_post_init
from bot import BotApp
from config import (
    AppConfig,
    DefaultsConfig,
    MCPConfig,
    MiniAppConfig,
    TelegramConfig,
    ThreadModeConfig,
    ToolConfig,
)
from sessions.conversation_scope import ConversationScope
from tg.callbacks import CallbackHandler


class _FakeBot:
    def __init__(self, thread_ids: list[int], *, has_topics_enabled: bool = True, chat_is_forum: bool = True) -> None:
        self._thread_ids = list(thread_ids)
        self._me = SimpleNamespace(has_topics_enabled=bool(has_topics_enabled), username="topics_bot", id=1)
        self._chat = SimpleNamespace(is_forum=bool(chat_is_forum))
        self.created_topics: list[dict[str, object]] = []
        self.renamed_topics: list[dict[str, object]] = []
        self.deleted_topics: list[dict[str, object]] = []
        self.chat_actions: list[dict[str, object]] = []
        self.sent_messages: list[dict[str, object]] = []
        self.deleted_threads: set[tuple[int, int]] = set()
        self.create_topic_error: Exception | None = None

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

    async def delete_forum_topic(self, *, chat_id: int, message_thread_id: int):
        self.deleted_threads.add((int(chat_id), int(message_thread_id)))
        self.deleted_topics.append(
            {
                "chat_id": int(chat_id),
                "message_thread_id": int(message_thread_id),
            }
        )
        return True

    async def send_chat_action(self, *, chat_id: int, message_thread_id: int | None = None, action: str, **_kwargs):
        if message_thread_id is not None and (int(chat_id), int(message_thread_id)) in self.deleted_threads:
            raise BadRequest("Message thread not found")
        self.chat_actions.append(
            {
                "chat_id": int(chat_id),
                "message_thread_id": int(message_thread_id) if message_thread_id is not None else None,
                "action": str(action),
            }
        )
        return True

    async def send_message(self, **kwargs):
        self.sent_messages.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.sent_messages))

    async def get_me(self):
        return self._me

    async def get_chat(self, chat_id: int):
        _ = int(chat_id)
        return self._chat


class _FakeCallbackQuery:
    def __init__(
        self,
        data: str,
        *,
        chat_id: int,
        message_thread_id: int | None = None,
        message_id: int = 1,
        from_user_id: int = 1,
    ) -> None:
        self.data = str(data)
        self.message = SimpleNamespace(
            chat_id=int(chat_id),
            message_id=int(message_id),
            message_thread_id=message_thread_id,
        )
        self.from_user = SimpleNamespace(id=int(from_user_id))
        self.answered = False

    async def answer(self) -> None:
        self.answered = True


def _build_config(
    tmp_path,
    *,
    intent: str,
    mode: str = "group",
    topics_chat_id: int | None = -100777000111,
) -> AppConfig:
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
            mode=mode,
            topics_chat_id=topics_chat_id if mode == "group" else None,
            topic_title_prefix="cli",
        ),
    )


def _text_update(*, chat_id: int, text: str, user_id: int | None = None, message_thread_id: int | None = None):
    message = SimpleNamespace(
        text=str(text),
        message_thread_id=message_thread_id,
        document=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        animation=None,
        video_note=None,
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        effective_user=SimpleNamespace(id=int(user_id if user_id is not None else chat_id)),
        effective_message=message,
        message=message,
    )


@pytest.mark.asyncio
async def test_telegram_session_create_topic_and_rename_topic_flow(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="create_rename")
    workdir_one = tmp_path / "project_one"
    workdir_two = tmp_path / "project_two"
    workdir_one.mkdir()
    workdir_two.mkdir()
    app = BotApp(cfg)
    fake_bot = _FakeBot([101, 202])
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100777000111),
        effective_message=SimpleNamespace(message_thread_id=101),
        message=SimpleNamespace(message_thread_id=101),
    )

    try:
        session_one, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir_one),
            bot=fake_bot,
        )
        assert err is None
        assert session_one is not None
        assert isinstance(session_one.conversation_scope, ConversationScope)
        assert session_one.conversation_scope.session_uid == "thread:-100777000111:101"

        session_two, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir_two),
            bot=fake_bot,
        )
        assert err is None
        assert session_two is not None
        assert session_two.conversation_scope.session_uid == "thread:-100777000111:202"
        assert session_one.conversation_scope.session_uid != session_two.conversation_scope.session_uid

        records = app.session_thread_repository.list_mappings()
        assert {(record.session_id, record.message_thread_id) for record in records} == {
            ("s1", 101),
            ("s2", 202),
        }
        assert (
            app.session_thread_manager.resolve_session_uid(chat_id=-100777000111, message_thread_id=101)
            == session_one.conversation_scope.session_uid
        )
        assert (
            app.session_thread_manager.resolve_session_uid(chat_id=-100777000111, message_thread_id=202)
            == session_two.conversation_scope.session_uid
        )

        await app.handlers.cmd_rename(
            update,
            SimpleNamespace(args=["Новый топик"], bot=fake_bot),
        )

        assert len(fake_bot.created_topics) == 2
        assert [topic["message_thread_id"] for topic in fake_bot.created_topics] == [101, 202]
        assert all(topic["chat_id"] == -100777000111 for topic in fake_bot.created_topics)
        assert "s1" in str(fake_bot.created_topics[0]["name"])
        assert "s2" in str(fake_bot.created_topics[1]["name"])
        assert fake_bot.renamed_topics[-1]["chat_id"] == -100777000111
        assert fake_bot.renamed_topics[-1]["message_thread_id"] == 101
        assert "s1" in str(fake_bot.renamed_topics[-1]["name"])
        assert "Новый топик" in str(fake_bot.renamed_topics[-1]["name"])
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_close_session_with_cleanup_deletes_forum_topic_and_mapping(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="close_cleanup")
    workdir = tmp_path / "project_close_cleanup"
    workdir.mkdir()
    app = BotApp(cfg)
    fake_bot = _FakeBot([909])
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

    try:
        session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None
        assert app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id) is not None

        closed = await app.close_session_with_cleanup(
            session.id,
            1,
            SimpleNamespace(bot=fake_bot),
        )

        assert closed is True
        assert app.manager.get(1, session.id) is None
        assert fake_bot.deleted_topics == [
            {
                "chat_id": -100777000111,
                "message_thread_id": 909,
            }
        ]
        assert app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id) is None
        assert app.session_thread_repository.get_by_topic(
            topics_chat_id=-100777000111,
            message_thread_id=909,
        ) is None
        assert app.session_thread_manager.resolve_session_uid(
            chat_id=-100777000111,
            message_thread_id=909,
        ) is None
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_close_session_with_cleanup_private_mode_deletes_user_topic(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="close_cleanup_private", mode="private")
    workdir = tmp_path / "project_close_cleanup_private"
    workdir.mkdir()
    app = BotApp(cfg)
    fake_bot = _FakeBot([919])
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None

    try:
        session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None
        assert session.conversation_scope.chat_id == 1
        assert session.conversation_scope.message_thread_id == 919
        assert app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id) is not None

        closed = await app.close_session_with_cleanup(
            session.id,
            1,
            SimpleNamespace(bot=fake_bot),
        )

        assert closed is True
        assert app.manager.get(1, session.id) is None
        assert fake_bot.deleted_topics == [
            {
                "chat_id": 1,
                "message_thread_id": 919,
            }
        ]
        assert app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id) is None
        assert app.session_thread_manager.resolve_session_uid(
            chat_id=1,
            message_thread_id=919,
        ) is None
    finally:
        app.shutdown_html_process_pool()


def test_private_mode_non_admin_handle_callback_close_deletes_user_topic(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="private_handle_callback_close", mode="private", topics_chat_id=None)
        workdir = tmp_path / "project_private_handle_callback_close"
        workdir.mkdir()
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        app.config.telegram.user_workdirs = {1: [str(workdir)]}
        fake_bot = _FakeBot([929])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        app.session_ui._edit_msg = _fake_edit_msg  # type: ignore[method-assign]

        try:
            session, err = await app.session_creation_service.create_session(
                1,
                "dummy",
                str(workdir),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None
            assert session.conversation_scope.chat_id == 1
            assert session.conversation_scope.message_thread_id == 929

            query = _FakeCallbackQuery(
                f"sess_close:{session.id}",
                chat_id=1,
                message_thread_id=929,
            )

            await handler.handle_callback(
                SimpleNamespace(callback_query=query),
                SimpleNamespace(bot=fake_bot),
            )

            assert query.answered is True
            assert edited[-1]["text"] == "Сессия закрыта и удалена из состояния."
            assert app.manager.get(1, session.id) is None
            assert fake_bot.deleted_topics == [
                {
                    "chat_id": 1,
                    "message_thread_id": 929,
                }
            ]
            assert app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id) is None
            assert app.session_thread_manager.resolve_session_uid(chat_id=1, message_thread_id=929) is None
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_group_mode_rejects_session_creation_commands_outside_topics_chat(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="group_reject")
    workdir = tmp_path / "group_reject_project"
    workdir.mkdir()
    app = BotApp(cfg)
    fake_bot = _FakeBot([101])
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=1))

    try:
        await app.handlers.cmd_new(
            update,
            SimpleNamespace(args=["dummy", str(workdir)], bot=fake_bot),
        )

        assert app.manager.sessions_for_chat(1) == {}
        assert fake_bot.sent_messages
        assert "только в настроенном forum-чате" in str(fake_bot.sent_messages[-1]["text"])
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_unknown_topic_does_not_fallback_to_single_chat_session_and_returns_explicit_error(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="unknown_topic")
    workdir = tmp_path / "unknown_topic_project"
    workdir.mkdir()
    app = BotApp(cfg)
    fake_bot = _FakeBot([101])
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100777000111),
        effective_message=SimpleNamespace(message_thread_id=999),
        message=SimpleNamespace(message_thread_id=999),
    )

    try:
        session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None
        assert session.conversation_scope.session_uid == "thread:-100777000111:101"

        resolved = app.resolve_telegram_scope_session(
            reply_chat_id=-100777000111,
            message_thread_id=999,
            owner_chat_id=1,
        )
        assert resolved is None

        route = app.resolve_telegram_inbound_route(update)
        assert route.unknown_thread is True
        assert route.session is None
        assert route.session_uid is None

        authorized = await app.ensure_telegram_inbound_authorized(
            update,
            SimpleNamespace(bot=fake_bot),
        )
        assert authorized is None
        assert fake_bot.sent_messages
        assert "Этот topic не связан ни с одной сессией CLI Proxy." in str(fake_bot.sent_messages[-1]["text"])
    finally:
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_startup_reconcile_restores_mapping_and_logs_stale_record(tmp_path, monkeypatch) -> None:
    cfg = _build_config(tmp_path, intent="reconcile")
    workdir = tmp_path / "reconcile_project"
    workdir.mkdir()
    fake_bot = _FakeBot([501])

    app_first = BotApp(cfg)
    app_first.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    session = None
    try:
        session, err = await app_first.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None
        app_first.state_repository.update_session_fields(
            chat_id=1,
            session_id=session.id,
            updates={
                "conversation_scope": {
                    "chat_id": 1,
                    "message_thread_id": None,
                    "session_uid": "chat:1",
                    "session_surface": "chat",
                },
                "message_thread_id": None,
                "session_uid": "chat:1",
                "session_surface": "chat",
            },
        )
        app_first.session_thread_repository.upsert_mapping(
            owner_chat_id=1,
            session_id="missing",
            session_uid="thread:-100777000111:999",
            topics_chat_id=-100777000111,
            message_thread_id=999,
            topic_name="stale-topic",
        )
    finally:
        app_first.shutdown_html_process_pool()

    app_second = BotApp(cfg)
    app_second.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    try:
        restored_before = app_second.manager.get(1, "s1")
        assert restored_before is not None
        assert restored_before.conversation_scope.message_thread_id is None

        async def _noop(*_args, **_kwargs):
            return None

        async def _deadline_checker(*_args, **_kwargs):
            await asyncio.sleep(0)

        monkeypatch.setattr(app_second, "set_bot_commands", _noop)
        monkeypatch.setattr(app_second.mcp, "start", _noop)
        monkeypatch.setattr(app_second.scheduler_service, "start", _noop)
        monkeypatch.setattr(app_second.webhook_ingress_service, "start", _noop)
        monkeypatch.setattr(app_second.miniapp_server, "start", _noop)
        monkeypatch.setattr(app_second.shared_http_ingress, "start", _noop)
        monkeypatch.setattr(app_second.handlers, "notify_pending_selfupdate", _noop)
        monkeypatch.setattr("app.services.lifecycle_service.run_task_deadline_checker", _deadline_checker)

        logged_warnings: list[str] = []

        def _capture_warning(message, *args, **_kwargs) -> None:
            rendered = str(message)
            if args:
                rendered = rendered % args
            logged_warnings.append(rendered)

        monkeypatch.setattr("app.services.session_thread_manager.logger.warning", _capture_warning)
        await build_post_init(app_second)(SimpleNamespace(bot=fake_bot))

        restored_after = app_second.manager.get(1, "s1")
        assert restored_after is not None
        assert restored_after.conversation_scope.session_uid == "thread:-100777000111:501"
        assert (
            app_second.session_thread_manager.resolve_session_uid(
                chat_id=-100777000111,
                message_thread_id=501,
            )
            == "thread:-100777000111:501"
        )
        assert any("pruned orphan mapping owner_chat_id=1 session_id=missing" in message for message in logged_warnings)
        assert app_second.session_thread_repository.get_by_session(owner_chat_id=1, session_id="missing") is None

        deadline_task = app_second._task_deadline_checker_task
        assert deadline_task is not None
        deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await deadline_task
        app_second._task_deadline_checker_task = None
    finally:
        app_second.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_session_thread_manager_does_not_leak_mapping_between_state_paths(tmp_path) -> None:
    cfg_a = _build_config(tmp_path, intent="intent_a")
    cfg_b = _build_config(tmp_path, intent="intent_b")
    workdir_a = tmp_path / "project_a"
    workdir_b = tmp_path / "project_b"
    workdir_a.mkdir()
    workdir_b.mkdir()
    fake_bot = _FakeBot([707])

    app_a = BotApp(cfg_a)
    app_b = None
    app_a.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    try:
        session_a, err = await app_a.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir_a),
            bot=fake_bot,
        )
        assert err is None
        assert session_a is not None

        app_b = BotApp(cfg_b)
        app_b.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        restored = await app_b.session_thread_manager.reconcile()

        assert restored == 0
        assert app_b.manager.sessions_for_chat(1) == {}
        assert (
            app_b.session_thread_manager.resolve_session_uid(
                chat_id=-100777000111,
                message_thread_id=707,
            )
            is None
        )
    finally:
        app_a.shutdown_html_process_pool()
        if app_b is not None:
            app_b.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_background_repair_job_recreates_deleted_topic_mapping(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="repair_deleted_topic")
    workdir = tmp_path / "repair_deleted_project"
    workdir.mkdir()
    fake_bot = _FakeBot([303, 909])

    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    repair_task = None
    try:
        session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None
        assert session.conversation_scope.session_uid == "thread:-100777000111:303"

        fake_bot.deleted_threads.add((-100777000111, 303))
        await app.session_thread_manager.start_repair_job(bot=fake_bot, interval_sec=0.01)
        repair_task = app.session_thread_manager._repair_task

        for _ in range(50):
            repaired = app.manager.get(1, session.id)
            assert repaired is not None
            if repaired.conversation_scope.session_uid == "thread:-100777000111:909":
                break
            await asyncio.sleep(0.01)

        repaired = app.manager.get(1, session.id)
        assert repaired is not None
        assert repaired.conversation_scope.session_uid == "thread:-100777000111:909"
        assert repaired.conversation_scope.message_thread_id == 909
        assert app.session_thread_repository.get_by_topic(
            topics_chat_id=-100777000111,
            message_thread_id=303,
        ) is None
        repaired_record = app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id)
        assert repaired_record is not None
        assert repaired_record.message_thread_id == 909
        assert (
            app.session_thread_manager.resolve_session_uid(
                chat_id=-100777000111,
                message_thread_id=909,
            )
            == "thread:-100777000111:909"
        )
        assert (
            app.session_thread_manager.resolve_session_uid(
                chat_id=-100777000111,
                message_thread_id=303,
            )
            is None
        )
        assert fake_bot.renamed_topics == []
    finally:
        await app.session_thread_manager.stop_repair_job()
        if repair_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await repair_task
        app.shutdown_html_process_pool()


@pytest.mark.asyncio
async def test_repair_job_logs_error_when_topic_cannot_be_recreated(tmp_path, monkeypatch) -> None:
    cfg = _build_config(tmp_path, intent="repair_error")
    workdir = tmp_path / "repair_error_project"
    workdir.mkdir()
    fake_bot = _FakeBot([404])

    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    try:
        session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir),
            bot=fake_bot,
        )
        assert err is None
        assert session is not None

        fake_bot.deleted_threads.add((-100777000111, 404))
        fake_bot.create_topic_error = RuntimeError("telegram create topic denied")
        logged_errors: list[str] = []

        def _capture_exception(message, *args, **_kwargs) -> None:
            rendered = str(message)
            if args:
                rendered = rendered % args
            logged_errors.append(rendered)

        monkeypatch.setattr("app.services.session_thread_manager.logger.exception", _capture_exception)
        repaired = await app.session_thread_manager.repair_reconcile(bot=fake_bot)

        assert repaired == 0
        assert any(
            "session thread repair failed: owner_chat_id=1 session_id=s1 session_uid=thread:-100777000111:404"
            in message
            for message in logged_errors
        )
        persisted = app.manager.get(1, session.id)
        assert persisted is not None
        assert persisted.conversation_scope.session_uid == "thread:-100777000111:404"
    finally:
        await app.session_thread_manager.stop_repair_job()
        app.shutdown_html_process_pool()


def test_repair_reconcile_skips_topic_rename_when_saved_title_matches(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="repair_skip_same_title")
        workdir = tmp_path / "repair_skip_same_title_project"
        workdir.mkdir()
        fake_bot = _FakeBot([606])

        app = BotApp(cfg)
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            session, err = await app.session_creation_service.create_session(
                1,
                "dummy",
                str(workdir),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None

            fake_bot.renamed_topics.clear()
            repaired = await app.session_thread_manager.repair_reconcile(bot=fake_bot)

            assert repaired == 0
            assert fake_bot.renamed_topics == []
            assert fake_bot.chat_actions == [
                {
                    "chat_id": -100777000111,
                    "message_thread_id": 606,
                    "action": "typing",
                }
            ]
        finally:
            await app.session_thread_manager.stop_repair_job()
            app.shutdown_html_process_pool()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_repair_reconcile_waits_between_topic_checks(tmp_path, monkeypatch) -> None:
    cfg = _build_config(tmp_path, intent="repair_wait_between_checks")
    workdir_one = tmp_path / "repair_wait_project_one"
    workdir_two = tmp_path / "repair_wait_project_two"
    workdir_one.mkdir()
    workdir_two.mkdir()
    fake_bot = _FakeBot([606, 707])
    sleep_calls: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleep_calls.append(float(delay))

    app = BotApp(cfg)
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    try:
        first_session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir_one),
            bot=fake_bot,
        )
        assert err is None
        assert first_session is not None
        second_session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(workdir_two),
            bot=fake_bot,
        )
        assert err is None
        assert second_session is not None

        fake_bot.chat_actions.clear()
        monkeypatch.setattr("app.services.session_thread_manager.asyncio.sleep", _record_sleep)
        repaired = await app.session_thread_manager.repair_reconcile(bot=fake_bot)

        assert repaired == 0
        assert sleep_calls == [app.session_thread_manager._REPAIR_RECORD_DELAY_SEC]
        assert [action["message_thread_id"] for action in fake_bot.chat_actions] == [606, 707]
    finally:
        await app.session_thread_manager.stop_repair_job()
        app.shutdown_html_process_pool()


def test_repair_reconcile_recreates_deleted_topic_without_extra_rename(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="repair_recreate_same_title")
        workdir = tmp_path / "repair_recreate_same_title_project"
        workdir.mkdir()
        fake_bot = _FakeBot([707, 808])

        app = BotApp(cfg)
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            session, err = await app.session_creation_service.create_session(
                1,
                "dummy",
                str(workdir),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None

            fake_bot.renamed_topics.clear()
            fake_bot.deleted_threads.add((-100777000111, 707))
            repaired = await app.session_thread_manager.repair_reconcile(bot=fake_bot)

            assert repaired == 1
            assert fake_bot.renamed_topics == []
            assert fake_bot.created_topics[-1]["message_thread_id"] == 808
            assert session.conversation_scope.session_uid == "thread:-100777000111:808"
        finally:
            await app.session_thread_manager.stop_repair_job()
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_creates_topic_in_owner_chat_for_new_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="private_create", mode="private")
        workdir = tmp_path / "private_create_project"
        workdir.mkdir()
        app = BotApp(cfg)
        fake_bot = _FakeBot([111])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            session, err = await app.session_creation_service.create_session(
                1,
                "dummy",
                str(workdir),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None
            assert session.conversation_scope.session_uid == "thread:1:111"
            assert len(fake_bot.created_topics) == 1
            assert fake_bot.created_topics[0]["chat_id"] == 1
            assert fake_bot.created_topics[0]["message_thread_id"] == 111
            record = app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id)
            assert record is not None
            assert record.topics_chat_id == 1
            assert record.message_thread_id == 111
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_threadless_message_requires_session_creation_when_none_exist(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="private_threadless_create", mode="private")
        app = BotApp(cfg)
        fake_bot = _FakeBot([111])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            update = _text_update(chat_id=1, text="hello")
            await app.on_message(update, SimpleNamespace(bot=fake_bot))

            assert fake_bot.sent_messages
            assert str(fake_bot.sent_messages[-1]["text"]) == "Сначала создайте сессию."
            assert fake_bot.sent_messages[-1].get("chat_id") == 1
            assert fake_bot.sent_messages[-1].get("message_thread_id") is None
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_threadless_message_requires_existing_topics_when_sessions_exist(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="private_threadless_topics", mode="private")
        workdir = tmp_path / "private_threadless_topics_project"
        workdir.mkdir()
        app = BotApp(cfg)
        fake_bot = _FakeBot([222])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            session, err = await app.session_creation_service.create_session(
                1,
                "dummy",
                str(workdir),
                bot=fake_bot,
            )
            assert err is None
            assert session is not None

            fake_bot.sent_messages.clear()
            update = _text_update(chat_id=1, text="hello")
            await app.on_message(update, SimpleNamespace(bot=fake_bot))

            assert fake_bot.sent_messages
            assert str(fake_bot.sent_messages[-1]["text"]) == "Используйте топики существующих сессий."
            assert fake_bot.sent_messages[-1].get("chat_id") == 1
            assert fake_bot.sent_messages[-1].get("message_thread_id") is None
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


@pytest.mark.parametrize("outside_thread_id", [None, 1])
def test_group_mode_sessions_outside_topic_shows_new_session_button_when_none_exist(
    tmp_path,
    outside_thread_id: int | None,
) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_sessions_outside_topic")
        app = BotApp(cfg)
        fake_bot = _FakeBot([333])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            await app.handlers.cmd_sessions(
                _text_update(
                    chat_id=-100777000111,
                    text="/sessions",
                    user_id=1,
                    message_thread_id=outside_thread_id,
                ),
                SimpleNamespace(args=[], bot=fake_bot),
            )

            assert fake_bot.sent_messages
            last = fake_bot.sent_messages[-1]
            assert last["chat_id"] == -100777000111
            assert last.get("message_thread_id") is None
            assert "Активных сессий нет." in str(last["text"]).replace("\\.", ".")
            keyboard = last.get("reply_markup")
            assert keyboard is not None
            assert keyboard.inline_keyboard[0][0].callback_data == "sess_new"
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


@pytest.mark.parametrize("outside_thread_id", [None, 1])
def test_group_mode_outside_topic_callback_flow_can_create_first_session(
    tmp_path,
    outside_thread_id: int | None,
) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_outside_topic_create")
        app = BotApp(cfg)
        fake_bot = _FakeBot([444])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="new_tool:dummy",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=outside_thread_id,
            ),
            from_user=SimpleNamespace(id=1),
        )
        ctx = SimpleNamespace(bot=fake_bot)
        try:
            handled = await handler._cb_new_tool(
                data="new_tool:dummy",
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True
            ui_key = app.telegram_ui_key(-100777000111, outside_thread_id)
            assert app.ui_state.pending_new_tool[ui_key] == "dummy"
            assert app.ui_state.dirs_mode[ui_key] == "new_session"

            query.data = "dir_use_current"
            handled = await handler._cb_dir_use_current(
                data="dir_use_current",
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True

            sessions = app.manager.sessions_for_chat(1)
            assert len(sessions) == 1
            session = next(iter(sessions.values()))
            assert session.chat_id == 1
            assert isinstance(session.conversation_scope, ConversationScope)
            assert session.conversation_scope.chat_id == -100777000111
            assert session.conversation_scope.message_thread_id == 444
            assert fake_bot.created_topics[-1]["chat_id"] == -100777000111
            assert fake_bot.created_topics[-1]["message_thread_id"] == 444
            assert edited[-1]["text"] == "Сессия s1 создана и привязана к новому topic. Продолжайте там."
            assert fake_bot.sent_messages[-1]["chat_id"] == -100777000111
            assert fake_bot.sent_messages[-1]["message_thread_id"] == 444
            assert "Активная сессия:" in str(fake_bot.sent_messages[-1]["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_new_session_from_existing_topic_redirects_to_new_topic(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_existing_topic_create")
        app = BotApp(cfg)
        fake_bot = _FakeBot([222, 444])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []
        samovar = tmp_path / "samovar"
        second = tmp_path / "nikatop"
        samovar.mkdir()
        second.mkdir()

        initial_session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(samovar),
            bot=fake_bot,
            register_project=True,
        )
        assert err is None
        assert initial_session is not None
        assert initial_session.conversation_scope.message_thread_id == 222

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="new_tool:dummy",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=222,
            ),
            from_user=SimpleNamespace(id=1),
        )
        ctx = SimpleNamespace(bot=fake_bot)
        try:
            handled = await handler._cb_new_tool(
                data="new_tool:dummy",
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True
            ui_key = app.telegram_ui_key(-100777000111, 222)
            app.ui_state.dirs_base[ui_key] = str(second)
            app.ui_state.dirs_root[ui_key] = str(tmp_path)

            query.data = "dir_use_current"
            handled = await handler._cb_dir_use_current(
                data="dir_use_current",
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True

            sessions = app.manager.sessions_for_chat(1)
            assert len(sessions) == 2
            new_session = max(sessions.values(), key=lambda item: int(str(item.id).lstrip("s") or "0"))
            assert new_session.conversation_scope.chat_id == -100777000111
            assert new_session.conversation_scope.message_thread_id == 444
            assert edited[-1]["text"] == f"Сессия {new_session.id} создана и привязана к новому topic. Продолжайте там."
            assert fake_bot.sent_messages[-1]["chat_id"] == -100777000111
            assert fake_bot.sent_messages[-1]["message_thread_id"] == 444
            assert "Активная сессия:" in str(fake_bot.sent_messages[-1]["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_non_admin_sess_new_opens_tool_picker_like_admin(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_user_sess_new_tool_picker")
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        app.config.telegram.user_workdirs = {1: [str(tmp_path / "allowed_project")]}
        (tmp_path / "allowed_project").mkdir()
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []

        async def _fake_edit_message(_context, *, chat_id, message_id, text, reply_markup=None, md2=True):
            edited.append(
                {
                    "chat_id": int(chat_id),
                    "message_id": int(message_id),
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        app._edit_message = _fake_edit_message  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="sess_new",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=None,
            ),
            from_user=SimpleNamespace(id=1),
        )

        try:
            handled = await handler._cb_sess_new(
                data="sess_new",
                chat_id=-100777000111,
                query=query,
                context=SimpleNamespace(bot=_FakeBot([333])),
            )

            assert handled is True
            assert edited[-1]["text"] == "Выберите инструмент для новой сессии:"
            keyboard = edited[-1]["reply_markup"]
            assert keyboard is not None
            assert keyboard.inline_keyboard[0][0].callback_data == "new_tool:dummy"
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_non_admin_new_tool_opens_project_picker_with_tool_context(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_user_new_tool_picker")
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        first = tmp_path / "user_project_one"
        second = tmp_path / "user_project_two"
        first.mkdir()
        second.mkdir()
        app.config.telegram.user_workdirs = {1: [str(first), str(second)]}
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="new_tool:dummy",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=None,
            ),
            from_user=SimpleNamespace(id=1),
        )

        try:
            handled = await handler._cb_new_tool(
                data="new_tool:dummy",
                chat_id=-100777000111,
                query=query,
                context=SimpleNamespace(bot=_FakeBot([333])),
            )

            assert handled is True
            assert edited[-1]["text"] == "Выбран инструмент dummy. Выберите проект."
            keyboard = edited[-1]["reply_markup"]
            callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
            assert callbacks == [
                "user_project_pick_new:tool=dummy:0",
                "user_project_pick_new:tool=dummy:1",
                "sess_new",
            ]
            ui_key = app.telegram_ui_key(-100777000111, None)
            assert ui_key not in app.ui_state.pending_new_tool
            assert ui_key not in app.ui_state.dirs_mode
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_mode_non_admin_handle_callback_new_tool_opens_project_picker(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="private_user_new_tool_callback", mode="private", topics_chat_id=None)
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        project = tmp_path / "private_user_project"
        project.mkdir()
        app.config.telegram.user_workdirs = {1: [str(project)]}
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = _FakeCallbackQuery("new_tool:dummy", chat_id=1)

        try:
            await handler.handle_callback(
                SimpleNamespace(callback_query=query),
                SimpleNamespace(bot=_FakeBot([333])),
            )

            assert query.answered is True
            assert edited[-1]["text"] == "Выбран инструмент dummy. Выберите проект."
            keyboard = edited[-1]["reply_markup"]
            callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
            assert callbacks == [
                "user_project_pick_new:tool=dummy:0",
                "sess_new",
            ]
            ui_key = app.telegram_ui_key(1, None)
            assert ui_key not in app.ui_state.pending_new_tool
            assert ui_key not in app.ui_state.dirs_mode
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_non_admin_new_tool_rejects_unknown_tool_before_project_picker(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_user_new_tool_invalid")
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        project = tmp_path / "user_project"
        project.mkdir()
        app.config.telegram.user_workdirs = {1: [str(project)]}
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="new_tool:ghost",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=None,
            ),
            from_user=SimpleNamespace(id=1),
        )

        try:
            handled = await handler._cb_new_tool(
                data="new_tool:ghost",
                chat_id=-100777000111,
                query=query,
                context=SimpleNamespace(bot=_FakeBot([333])),
            )

            assert handled is True
            assert edited[-1]["text"] == "Инструмент не найден."
            assert edited[-1]["reply_markup"] is None
            ui_key = app.telegram_ui_key(-100777000111, None)
            assert ui_key not in app.ui_state.pending_new_tool
            assert ui_key not in app.ui_state.dirs_mode
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_user_project_pick_from_existing_topic_redirects_to_new_topic(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_user_project_pick_redirect")
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        fake_bot = _FakeBot([222, 444])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []
        samovar = tmp_path / "samovar_user_project"
        second = tmp_path / "nikatop_user_project"
        samovar.mkdir()
        second.mkdir()
        app.config.telegram.user_workdirs = {1: [str(samovar), str(second)]}

        initial_session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(samovar),
            bot=fake_bot,
            register_project=True,
        )
        assert err is None
        assert initial_session is not None
        assert initial_session.conversation_scope.message_thread_id == 222

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data=f"user_project_pick:{initial_session.conversation_scope.session_uid}:1",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=222,
            ),
            from_user=SimpleNamespace(id=1),
        )
        ctx = SimpleNamespace(bot=fake_bot)
        try:
            handled = await handler._cb_user_project_pick(
                data=query.data,
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True

            sessions = app.manager.sessions_for_chat(1)
            assert len(sessions) == 2
            new_session = max(sessions.values(), key=lambda item: int(str(item.id).lstrip("s") or "0"))
            assert new_session.workdir == str(second)
            assert new_session.conversation_scope.chat_id == -100777000111
            assert new_session.conversation_scope.message_thread_id == 444
            assert edited[-1]["text"] == f"Сессия {new_session.id} создана и привязана к новому topic. Продолжайте там."
            assert fake_bot.sent_messages[-1]["chat_id"] == -100777000111
            assert fake_bot.sent_messages[-1]["message_thread_id"] == 444
            assert "Активная сессия:" in str(fake_bot.sent_messages[-1]["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_user_project_pick_new_creates_fresh_session_for_same_project(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_user_project_pick_new_same_project")
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        fake_bot = _FakeBot([222, 444])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []
        only_project = tmp_path / "same_project_user"
        only_project.mkdir()
        app.config.telegram.user_workdirs = {1: [str(only_project)]}

        initial_session, err = await app.session_creation_service.create_session(
            1,
            "dummy",
            str(only_project),
            bot=fake_bot,
            register_project=True,
        )
        assert err is None
        assert initial_session is not None
        assert initial_session.conversation_scope.message_thread_id == 222

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data=f"user_project_pick_new:{initial_session.conversation_scope.session_uid}:0",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=222,
            ),
            from_user=SimpleNamespace(id=1),
        )
        ctx = SimpleNamespace(bot=fake_bot)
        try:
            handled = await handler._cb_user_project_pick_new(
                data=query.data,
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True

            sessions = list(app.manager.sessions_for_chat(1).values())
            assert len(sessions) == 2
            new_session = max(sessions, key=lambda item: int(str(item.id).lstrip("s") or "0"))
            assert new_session is not initial_session
            assert new_session.workdir == str(only_project)
            assert new_session.conversation_scope.chat_id == -100777000111
            assert new_session.conversation_scope.message_thread_id == 444
            assert edited[-1]["text"] == f"Сессия {new_session.id} создана и привязана к новому topic. Продолжайте там."
            assert fake_bot.sent_messages[-1]["chat_id"] == -100777000111
            assert fake_bot.sent_messages[-1]["message_thread_id"] == 444
            assert "Активная сессия:" in str(fake_bot.sent_messages[-1]["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_group_mode_user_project_pick_new_with_tool_token_creates_session(tmp_path) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="group_user_project_pick_new_tool_token")
        app = BotApp(cfg)
        app.config.telegram.admlist_chat_ids = []
        fake_bot = _FakeBot([444])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        handler = CallbackHandler(app)
        edited = []
        only_project = tmp_path / "fresh_user_project"
        only_project.mkdir()
        app.config.telegram.user_workdirs = {1: [str(only_project)]}

        async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
            edited.append(
                {
                    "text": str(text),
                    "reply_markup": reply_markup,
                    "md2": bool(md2),
                }
            )
            return True

        handler._edit_msg = _fake_edit_msg  # type: ignore[method-assign]
        query = SimpleNamespace(
            data="user_project_pick_new:tool=dummy:0",
            message=SimpleNamespace(
                chat_id=-100777000111,
                message_id=1,
                message_thread_id=None,
            ),
            from_user=SimpleNamespace(id=1),
        )
        ctx = SimpleNamespace(bot=fake_bot)
        try:
            handled = await handler._cb_user_project_pick_new(
                data=query.data,
                chat_id=-100777000111,
                query=query,
                context=ctx,
            )
            assert handled is True

            sessions = list(app.manager.sessions_for_chat(1).values())
            assert len(sessions) == 1
            session = sessions[0]
            assert session.tool.name == "dummy"
            assert session.workdir == str(only_project)
            assert session.conversation_scope.chat_id == -100777000111
            assert session.conversation_scope.message_thread_id == 444
            assert edited[-1]["text"] == "Сессия s1 создана и привязана к новому topic. Продолжайте там."
            assert fake_bot.sent_messages[-1]["chat_id"] == -100777000111
            assert fake_bot.sent_messages[-1]["message_thread_id"] == 444
            assert "Активная сессия:" in str(fake_bot.sent_messages[-1]["text"])
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_post_init_backfills_existing_private_sessions_without_threads(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        cfg = _build_config(tmp_path, intent="private_backfill", mode="private")
        workdir = tmp_path / "private_backfill_project"
        workdir.mkdir()
        app = BotApp(cfg)
        fake_bot = _FakeBot([222])
        app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
        try:
            session = app.manager.create(1, "dummy", str(workdir))
            assert session.conversation_scope.session_uid == "chat:1"

            async def _noop(*_args, **_kwargs):
                return None

            async def _deadline_checker(*_args, **_kwargs):
                await asyncio.sleep(0)

            monkeypatch.setattr(app, "set_bot_commands", _noop)
            monkeypatch.setattr(app.mcp, "start", _noop)
            monkeypatch.setattr(app.scheduler_service, "start", _noop)
            monkeypatch.setattr(app.webhook_ingress_service, "start", _noop)
            monkeypatch.setattr(app.miniapp_server, "start", _noop)
            monkeypatch.setattr(app.shared_http_ingress, "start", _noop)
            monkeypatch.setattr(app.handlers, "notify_pending_selfupdate", _noop)
            monkeypatch.setattr("app.services.lifecycle_service.run_task_deadline_checker", _deadline_checker)

            await build_post_init(app)(SimpleNamespace(bot=fake_bot))

            restored = app.manager.get(1, session.id)
            assert restored is not None
            assert restored.conversation_scope.session_uid == "thread:1:222"
            assert len(fake_bot.created_topics) == 1
            assert fake_bot.created_topics[0]["chat_id"] == 1
            assert fake_bot.created_topics[0]["message_thread_id"] == 222

            record = app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id)
            assert record is not None
            assert record.topics_chat_id == 1
            assert record.message_thread_id == 222

            deadline_task = app._task_deadline_checker_task
            assert deadline_task is not None
            deadline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await deadline_task
            app._task_deadline_checker_task = None
        finally:
            app.shutdown_html_process_pool()

    asyncio.run(_run())


def test_private_thread_mode_rebinds_single_recent_stale_mapping(tmp_path) -> None:
    cfg = _build_config(tmp_path, intent="private_stale_rebind", mode="private", topics_chat_id=None)
    workdir = tmp_path / "private_project"
    workdir.mkdir()
    app = BotApp(cfg)
    try:
        session = app.manager.create(1, "dummy", str(workdir), message_thread_id=222)
        app.session_thread_manager.bind_existing_topic_for_session(
            owner_chat_id=1,
            session=session,
            topics_chat_id=1,
            message_thread_id=222,
        )
        app.session_thread_manager.mark_topic_stale(
            topics_chat_id=1,
            message_thread_id=222,
            reason="Message thread not found",
        )

        route = app.resolve_telegram_inbound_route(
            _text_update(chat_id=1, text="hello", message_thread_id=444)
        )

        assert route.session is session
        assert route.message_thread_id == 444
        assert session.conversation_scope.message_thread_id == 444
        assert app.session_thread_repository.get_by_topic(topics_chat_id=1, message_thread_id=222) is None
        rebound = app.session_thread_repository.get_by_session(owner_chat_id=1, session_id=session.id)
        assert rebound is not None
        assert rebound.message_thread_id == 444
    finally:
        app.shutdown_html_process_pool()
