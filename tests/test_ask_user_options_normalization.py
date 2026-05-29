import asyncio

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
