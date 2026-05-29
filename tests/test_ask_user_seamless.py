import asyncio

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
    app.get_runtime_by_capability("message_tracking").record_message = lambda *_args, **_kwargs: None
    return app


def test_resolve_seamless_matching_option(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None

        app.ui_state.pending_questions = {
            "q1": {
                "chat_id": 1,
                "session_id": s1.id,
                "options": ["Опция А", "Опция Б"],
                "awaiting_custom": False,
                "allow_custom": True,
                "created_at": 10.0,
            }
        }
        app.ui_state.active_ask_question_by_chat[app.telegram_ui_key(1)] = "q1"

        # Без нажатия "Свой вариант" текст не должен резолвить ask-вопрос.
        ok = app._resolve_pending_custom_answer(1, "  опция а  ")
        assert ok is False
        assert calls == []

    asyncio.run(_run())


def test_resolve_seamless_does_not_guess_short_alias_for_button(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None

        app.ui_state.pending_questions = {
            "q1": {
                "chat_id": 1,
                "session_id": s1.id,
                "options": ["Продолжить остановленный план", "Начать новый план", "Отмена"],
                "awaiting_custom": False,
                "allow_custom": False,
                "created_at": 10.0,
            }
        }
        app.ui_state.active_ask_question_by_chat[app.telegram_ui_key(1)] = "q1"

        ok = app._resolve_pending_custom_answer(1, "продолжить")
        assert ok is False
        assert calls == []
        assert "q1" in app.ui_state.pending_questions

    asyncio.run(_run())


def test_resolve_seamless_custom_answer(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None

        app.ui_state.pending_questions = {
            "q1": {
                "chat_id": 1,
                "session_id": s1.id,
                "options": ["Да", "Нет"],
                "awaiting_custom": True,
                "allow_custom": True,
                "created_at": 10.0,
            }
        }
        app.ui_state.active_ask_question_by_chat[app.telegram_ui_key(1)] = "q1"

        ok = app._resolve_pending_custom_answer(1, "Свой текст")
        assert ok is True
        assert calls == [("q1", "Свой текст")]

    asyncio.run(_run())


def test_resolve_seamless_analyst_accepts_free_text_without_custom_button(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        s1.modes.active_mode = "analyst"
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None

        app.ui_state.pending_questions = {
            "q1": {
                "chat_id": 1,
                "session_id": s1.id,
                "options": ["Да", "Нет"],
                "awaiting_custom": False,
                "allow_custom": True,
                "created_at": 10.0,
            }
        }
        app.ui_state.active_ask_question_by_chat[app.telegram_ui_key(1)] = "q1"

        ok = app._resolve_pending_custom_answer(1, "Нужен backend handler")
        assert ok is True
        assert calls == [("q1", "Нужен backend handler")]

    asyncio.run(_run())


def test_resolve_seamless_custom_denied(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        s1 = app.manager.create(1, "dummy", str(tmp_path))
        calls = []

        class _Runtime:
            @staticmethod
            def resolve_question(question_id: str, answer: str) -> bool:
                calls.append((question_id, answer))
                return True

        app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None

        app.ui_state.pending_questions = {
            "q1": {
                "chat_id": 1,
                "session_id": s1.id,
                "options": ["Да", "Нет"],
                "awaiting_custom": False,
                "allow_custom": False,
                "created_at": 10.0,
            }
        }
        app.ui_state.active_ask_question_by_chat[app.telegram_ui_key(1)] = "q1"

        ok = app._resolve_pending_custom_answer(1, "Свой текст")
        assert ok is False
        assert len(calls) == 0

    asyncio.run(_run())


def test_ensure_min_ask_options_no_system(tmp_path):
    app = _build_app(tmp_path)
    opts = app._ensure_min_ask_options(["Только одна"], system_options=False)
    assert opts == ["Только одна"]

    opts = app._ensure_min_ask_options(["Одна"], system_options=True)
    assert opts == ["Одна", "Остановиться и уточнить"]


def test_normalize_ask_options_no_custom_filter_if_disabled(tmp_path):
    app = _build_app(tmp_path)
    # If allow_custom is False, we don't filter out custom-looking options from the list
    opts = app._normalize_ask_options(["A", "Custom"], allow_custom=False)
    assert "Custom" in opts

    opts = app._normalize_ask_options(["A", "Custom"], allow_custom=True)
    assert "Custom" not in opts
