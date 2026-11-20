import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.cli_backends.tmux_backend import TmuxExecutionBackend
from app.services.telegram_ui_scope import TelegramUiKey
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
    app._test_selected_session = None

    def _set_active(chat_id: int, session_id: str) -> bool:
        session = app.manager.get(int(chat_id), str(session_id))
        if session is None:
            return False
        app._test_selected_session = session
        return True

    app.manager.set_active = _set_active  # type: ignore[attr-defined]
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


def _ui_key(chat_id: int) -> TelegramUiKey:
    return TelegramUiKey.from_parts(chat_id)


def test_send_ask_question_deduplicates_custom_option(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        sent = {}

        async def _fake_send_message(_context, **kwargs):
            sent.update(kwargs)

        app._send_message = _fake_send_message

        await app._send_ask_question(
            context=object(),
            chat_id=1,
            session_id="s1",
            question_id="q1",
            question="Q?",
            options=["A", "Свой вариант", "A", "✍️ Свой вариант"],
        )

        assert app.ui_state.pending_questions["q1"]["question"] == "Q?"
        assert app.ui_state.pending_questions["q1"]["options"] == ["A", "Остановиться и уточнить"]
        assert app.ui_state.active_ask_question_by_chat.get(_ui_key(1)) is None
        keyboard = sent["reply_markup"].inline_keyboard
        labels = [row[0].text for row in keyboard]
        assert labels == ["A", "Остановиться и уточнить", "✍️ Свой вариант"]
        assert keyboard[0][0].callback_data == "ask:q1:0"
        assert keyboard[2][0].callback_data == "ask:q1:custom"

    asyncio.run(_run())


def test_send_ask_question_keeps_tmux_feed_flag(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)

        async def _fake_send_message(_context, **_kwargs):
            return None

        app._send_message = _fake_send_message
        app.ui_state.pending_questions["q1"] = {"tmux_feed": True, "question": "old"}

        await app._send_ask_question(
            context=object(),
            chat_id=1,
            session_id="s1",
            question_id="q1",
            question="Do you want to proceed?",
            options=["1. Yes", "2. No"],
            allow_custom=False,
            system_options=False,
        )
        await app._send_ask_question(
            context=object(),
            chat_id=1,
            session_id="s1",
            question_id="q2",
            question="Обычный вопрос?",
            options=["A", "B"],
            allow_custom=False,
            system_options=False,
        )

        assert app.ui_state.pending_questions["q1"]["tmux_feed"] is True
        assert app.ui_state.pending_questions["q1"]["options"] == ["1. Yes", "2. No"]
        assert "tmux_feed" not in app.ui_state.pending_questions["q2"]

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_tmux_multiple_choice_edits_the_same_rich_card(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    app.resolve_telegram_callback_scope = lambda _query: (1, None, 1, session)
    app.session_management.reread_tmux_output = AsyncMock()
    send_choice = AsyncMock()
    monkeypatch.setattr(TmuxExecutionBackend, "send_choice", send_choice)
    calls = []

    async def post(endpoint, *, data):
        calls.append((endpoint, data))
        return SimpleNamespace(message_id=10)

    context = SimpleNamespace(bot=SimpleNamespace(_post=post, send_message=AsyncMock()))
    query = SimpleNamespace(
        message=SimpleNamespace(chat_id=1, message_id=10, message_thread_id=None),
        from_user=SimpleNamespace(id=1, language_code="ru"),
    )
    qid = "tmux_choice_s1_example"
    app.ui_state.pending_questions[qid] = {"tmux_feed": True, "tmux_multi_select": True}
    await app._send_ask_question(
        context, 1, session.id, qid, "Что сделать?", ["1. Первое", "2. Второе", "4. Четвёртое"],
        allow_custom=False, system_options=False,
    )
    for action in ("0", "1", "2", "1"):
        await app.callbacks._cb_ask(data=f"ask:{qid}:{action}", chat_id=1, query=query, context=context)

    assert [endpoint for endpoint, _ in calls] == ["sendRichMessage"] + ["editMessageText"] * 4
    assert all(payload["message_id"] == 10 and "rich_message" in payload for _, payload in calls[1:])
    keyboard = calls[-1][1]["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == ["☑ 1. Первое", "☐ 2. Второе", "☑ 4. Четвёртое", "Отправить"]
    send_choice.assert_not_awaited()
    await app.callbacks._cb_ask(data=f"ask:{qid}:submit", chat_id=1, query=query, context=context)
    send_choice.assert_awaited_once_with(session, "1,4", multi_question_id=qid)
    app.session_management.reread_tmux_output.assert_awaited_once()
    assert qid not in app.ui_state.pending_questions


def test_send_ask_question_truncates_long_button_but_keeps_full_text(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        sent = {}

        async def _fake_send_message(_context, **kwargs):
            sent.update(kwargs)

        app._send_message = _fake_send_message
        long_choice = "1. " + ("удалить историю " * 12)
        await app._send_ask_question(
            context=object(),
            chat_id=1,
            session_id="s1",
            question_id="q1",
            question="Question: Очистка",
            options=[long_choice, "2. Оставить"],
            allow_custom=False,
            system_options=False,
        )

        assert app.ui_state.pending_questions["q1"]["options"] == [long_choice.strip(), "2. Оставить"]
        assert long_choice.strip() in sent["text"]
        label = sent["reply_markup"].inline_keyboard[0][0].text
        assert len(label) <= 64
        assert label.endswith("…")

    asyncio.run(_run())


def test_resolve_pending_custom_answer_prefers_active_question(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        s2 = app.manager.create(1, "dummy", str(tmp_path))
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None
        app.ui_state.pending_questions = {
            "q_old": {"chat_id": 1, "session_id": s1.id, "awaiting_custom": True, "created_at": 10.0, "options": ["A", "B"]},
            "q_new": {"chat_id": 1, "session_id": s2.id, "awaiting_custom": True, "created_at": 20.0, "options": ["C", "D"]},
        }
        app.ui_state.active_ask_question_by_chat[_ui_key(1)] = "q_new"

        ok = app._resolve_pending_custom_answer(1, "мой вариант")

        assert ok is True
        assert calls == [("q_new", "мой вариант")]
        assert "q_new" not in app.ui_state.pending_questions
        assert app.ui_state.active_ask_question_by_chat.get(_ui_key(1)) is None
        assert "q_old" in app.ui_state.pending_questions

    asyncio.run(_run())


def test_resolve_pending_custom_answer_does_not_fallback_to_other_session(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        s2 = app.manager.create(1, "dummy", str(tmp_path))
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None
        app.ui_state.pending_questions = {
            "q_old": {
                "chat_id": 1,
                "session_id": s1.id,
                "awaiting_custom": True,
                "created_at": 10.0,
                "options": ["A", "B"],
            },
            "q_new": {
                "chat_id": 1,
                "session_id": s2.id,
                "allow_custom": False,
                "awaiting_custom": False,
                "created_at": 20.0,
                "options": ["Продолжить остановленный план", "Начать новый план", "Отмена"],
            },
        }
        app.ui_state.active_ask_question_by_chat[_ui_key(1)] = "q_new"

        ok = app._resolve_pending_custom_answer(1, "любой текст")

        assert ok is False
        assert calls == []
        assert "q_old" in app.ui_state.pending_questions
        assert "q_new" in app.ui_state.pending_questions

    asyncio.run(_run())


def test_clear_pending_questions_keeps_active_question_of_other_session(tmp_path):
    app = _build_app(tmp_path)
    app.ui_state.pending_questions = {
        "q1": {"chat_id": 1, "session_id": "s1", "created_at": 10.0},
        "q2": {"chat_id": 1, "session_id": "s2", "created_at": 20.0},
    }
    app.ui_state.active_ask_question_by_chat[_ui_key(1)] = "q2"

    removed = app._clear_pending_questions(session_id="s1", chat_id=1)

    assert removed == 1
    assert "q1" not in app.ui_state.pending_questions
    assert "q2" in app.ui_state.pending_questions
    assert app.ui_state.active_ask_question_by_chat.get(_ui_key(1)) == "q2"


def test_session_switch_does_not_rebind_custom_pending_question(tmp_path):
    app = _build_app(tmp_path)
    s1 = app.manager.create(1, "dummy", str(tmp_path))
    s2 = app.manager.create(1, "dummy", str(tmp_path))
    app.ui_state.pending_questions = {
        "q_s1": {"chat_id": 1, "session_id": s1.id, "created_at": 10.0, "options": ["A", "B"]},
        "q_s2_old": {"chat_id": 1, "session_id": s2.id, "created_at": 20.0, "options": ["A", "B"]},
        "q_s2_new": {"chat_id": 1, "session_id": s2.id, "created_at": 30.0, "options": ["A", "B"]},
    }
    app.ui_state.active_ask_question_by_chat[_ui_key(1)] = "q_s1"

    ok = app.manager.set_active(1, s2.id)
    assert ok is True
    assert app.ui_state.active_ask_question_by_chat.get(_ui_key(1)) == "q_s1"

    ok = app.manager.set_active(1, s1.id)
    assert ok is True
    assert app.ui_state.active_ask_question_by_chat.get(_ui_key(1)) == "q_s1"


def test_resolve_pending_custom_answer_supports_cancel(tmp_path):
    async def _run() -> None:
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        app.ui_state.pending_questions = {
            "q1": {
                "chat_id": 1,
                "session_id": s1.id,
                "allow_custom": True,
                "awaiting_custom": True,
                "created_at": 10.0,
                "options": ["A", "B"],
            }
        }
        app.ui_state.active_ask_question_by_chat[_ui_key(1)] = "q1"

        ok = app._resolve_pending_custom_answer(1, "отмена")

        assert ok is True
        assert app.ui_state.pending_questions["q1"]["awaiting_custom"] is False
        assert app.ui_state.active_ask_question_by_chat.get(_ui_key(1)) is None
        assert app._pop_pending_custom_input_status(1) == "cancelled"

    asyncio.run(_run())
