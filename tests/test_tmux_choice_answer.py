from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.services.cli_backends.tmux_backend import TmuxExecutionBackend
from app.services.telegram_ui_scope import TelegramUiKey
from bot import BotApp
from i18n import t
from tg.callback_actions.protocol import ProtocolActionsMixin


class _AskHarness(ProtocolActionsMixin):
    def __init__(self, bot_app) -> None:
        self.bot_app = bot_app

    def _ui_key(self, chat_id: int, _query):
        return TelegramUiKey.from_parts(int(chat_id))

    async def _edit_msg(self, _context, query, text, *, reply_markup=None, md2=True):
        query.edits.append(text)
        return True


def _app(*, pending: dict, get_session):
    def _clear(question_id: str) -> bool:
        return pending.pop(question_id, None) is not None

    return SimpleNamespace(
        config=SimpleNamespace(
            telegram=SimpleNamespace(user_languages={}),
            defaults=SimpleNamespace(default_language="ru"),
        ),
        manager=SimpleNamespace(get=get_session),
        ui_state=SimpleNamespace(pending_questions=pending, active_ask_question_by_chat={}),
        resolve_telegram_callback_scope=lambda _query: (100, None, 142987535, None),
        _clear_pending_question=_clear,
        session_management=SimpleNamespace(reread_tmux_output=AsyncMock(return_value="started")),
    )


def _query():
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42, language_code=None),
        message=SimpleNamespace(chat_id=100, message_id=10, message_thread_id=None),
        edits=[],
    )


def _pending(qid: str, *, tmux_feed: bool) -> dict:
    meta = {
        "chat_id": 100,
        "session_id": "s45",
        "options": ["1. Yes", "2. No"],
    }
    if tmux_feed:
        meta["tmux_feed"] = True
    return {qid: meta}


@pytest.mark.asyncio
async def test_tmux_choice_click_feeds_owner_session_and_second_click_is_stale(monkeypatch):
    session = SimpleNamespace(id="s45", chat_id=142987535)
    sent = []

    async def _send(_self, tmux_sess, text):
        sent.append((tmux_sess, text))

    monkeypatch.setattr(TmuxExecutionBackend, "send_choice", _send)
    resolves = []

    class _Runtime:
        @staticmethod
        def resolve_question(question_id: str, answer: str) -> bool:
            resolves.append((question_id, answer))
            return False

    qid = "tmux_choice_s45_cd3c802bc4d1"
    pending = _pending(qid, tmux_feed=True)

    def _get(chat_id, session_id):
        if int(chat_id) == 142987535 and session_id == "s45":
            return session
        return None

    app = _app(pending=pending, get_session=_get)
    app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None
    query = _query()
    harness = _AskHarness(app)

    ok = await harness._cb_ask(
        data=f"ask:{qid}:0",
        chat_id=100,
        query=query,
        context=object(),
    )

    assert ok is True
    assert sent == [(session, "1")]
    app.session_management.reread_tmux_output.assert_awaited_once()
    assert qid not in pending
    assert resolves == []
    assert query.edits == [t("msg.ask.choice_selected", "ru", answer="1. Yes")]
    assert qid in session._answered_tmux_choice_ids

    again = await harness._cb_ask(
        data=f"ask:{qid}:0",
        chat_id=100,
        query=query,
        context=object(),
    )

    assert again is True
    assert sent == [(session, "1")]
    app.session_management.reread_tmux_output.assert_awaited_once()
    assert query.edits[-1] == t("msg.input.question_stale", "ru")


@pytest.mark.asyncio
async def test_tmux_confirm_click_sends_letter_not_digit(monkeypatch):
    session = SimpleNamespace(id="s45", chat_id=142987535)
    sent = []

    async def _send(_self, tmux_sess, text):
        sent.append((tmux_sess, text))

    monkeypatch.setattr(TmuxExecutionBackend, "send_choice", _send)
    qid = "tmux_choice_s45_confirm"
    pending = _pending(qid, tmux_feed=True)
    pending[qid]["options"] = ["y. Submit answers", "n. Cancel"]
    app = _app(
        pending=pending,
        get_session=lambda chat_id, session_id: session
        if int(chat_id) == 142987535 and session_id == "s45"
        else None,
    )
    query = _query()

    ok = await _AskHarness(app)._cb_ask(
        data=f"ask:{qid}:0",
        chat_id=100,
        query=query,
        context=object(),
    )

    assert ok is True
    assert sent == [(session, "y")]
    assert qid not in pending


@pytest.mark.asyncio
async def test_tmux_choice_click_keeps_card_when_pane_send_fails(monkeypatch):
    session = SimpleNamespace(id="s45", chat_id=142987535)
    attempts = []

    async def _send(_self, _tmux_sess, text):
        attempts.append(text)
        raise RuntimeError("pane down")

    monkeypatch.setattr(TmuxExecutionBackend, "send_choice", _send)
    qid = "tmux_choice_s45_cd3c802bc4d1"
    pending = _pending(qid, tmux_feed=True)
    app = _app(
        pending=pending,
        get_session=lambda chat_id, session_id: session
        if int(chat_id) == 142987535 and session_id == "s45"
        else None,
    )
    query = _query()

    ok = await _AskHarness(app)._cb_ask(
        data=f"ask:{qid}:0",
        chat_id=100,
        query=query,
        context=object(),
    )

    assert ok is True
    assert attempts == ["1"]
    assert qid in pending
    assert pending[qid]["tmux_feed"] is True
    assert not getattr(session, "_answered_tmux_choice_ids", set())
    assert query.edits == [t("msg.error.tmux_current_send_failed", "ru")]


@pytest.mark.asyncio
async def test_plain_ask_click_still_resolves_plugin_question(monkeypatch):
    sent = []

    async def _send(_self, _tmux_sess, text):
        sent.append(text)

    monkeypatch.setattr(TmuxExecutionBackend, "send_input", _send)
    resolves = []

    class _Runtime:
        @staticmethod
        def resolve_question(question_id: str, answer: str) -> bool:
            resolves.append((question_id, answer))
            return True

    qid = "q-plugin"
    pending = _pending(qid, tmux_feed=False)
    app = _app(
        pending=pending,
        get_session=lambda chat_id, session_id: SimpleNamespace(id="s45")
        if int(chat_id) == 142987535 and session_id == "s45"
        else None,
    )
    app.get_runtime_by_capability = lambda capability: _Runtime() if capability == "resolve_question" else None
    query = _query()

    ok = await _AskHarness(app)._cb_ask(
        data=f"ask:{qid}:0",
        chat_id=100,
        query=query,
        context=object(),
    )

    assert ok is True
    assert sent == []
    assert resolves == [(qid, "1. Yes")]
    assert qid not in pending
    assert query.edits == [t("msg.ask.choice_selected", "ru", answer="1. Yes")]


def _other_app(pending, session, qid):
    ui_key = TelegramUiKey.from_parts(100)

    def _clear(question_id: str) -> bool:
        return pending.pop(question_id, None) is not None

    return SimpleNamespace(
        ui_state=SimpleNamespace(
            pending_questions=pending,
            active_ask_question_by_chat={ui_key: qid},
        ),
        manager=SimpleNamespace(
            get=lambda chat_id, session_id: session if int(chat_id) == 142987535 and session_id == "s46" else None,
        ),
        telegram_ui_key=BotApp.telegram_ui_key,
        _clear_pending_question=_clear,
        session_management=SimpleNamespace(reread_tmux_output=AsyncMock(return_value="started")),
    )


@pytest.mark.asyncio
async def test_feed_tmux_other_answer_types_stored_number(monkeypatch):
    sent = []

    async def _send(_self, tmux_sess, text, choice_number=""):
        sent.append((tmux_sess, text, choice_number))

    monkeypatch.setattr(TmuxExecutionBackend, "send_other_text", _send)
    session = SimpleNamespace(id="s46")
    qid = "tmux_choice_s46_other"
    pending = {
        qid: {
            "chat_id": 100,
            "session_id": "s46",
            "tmux_feed": True,
            "awaiting_custom": True,
            "tmux_other_choice": "5",
            "options": ["1. Без картинок", "6. Chat about this"],
        }
    }
    app = _other_app(pending, session, qid)

    result = await BotApp.feed_tmux_other_answer(app, 100, "Кухня мира", owner_chat_id=142987535, context=None)

    assert result == "accepted"
    assert sent == [(session, "Кухня мира", "5")]
    app.session_management.reread_tmux_output.assert_awaited_once_with(session, None)
    assert qid not in pending
    assert qid in session._answered_tmux_choice_ids


@pytest.mark.asyncio
async def test_feed_tmux_other_answer_keeps_question_when_send_fails(monkeypatch):
    async def _send(_self, _tmux_sess, _text, choice_number=""):
        raise RuntimeError("pane down")

    monkeypatch.setattr(TmuxExecutionBackend, "send_other_text", _send)
    session = SimpleNamespace(id="s46")
    qid = "tmux_choice_s46_other"
    pending = {
        qid: {
            "chat_id": 100,
            "session_id": "s46",
            "tmux_feed": True,
            "awaiting_custom": True,
            "tmux_other_choice": "5",
        }
    }
    app = _other_app(pending, session, qid)

    result = await BotApp.feed_tmux_other_answer(app, 100, "Кухня мира", owner_chat_id=142987535, context=None)

    assert result == "failed"
    app.session_management.reread_tmux_output.assert_not_awaited()
    assert qid in pending
    assert not getattr(session, "_answered_tmux_choice_ids", set())


@pytest.mark.asyncio
async def test_feed_tmux_other_answer_ignores_plugin_question():
    qid = "q-plugin"
    pending = {
        qid: {
            "chat_id": 100,
            "session_id": "s46",
            "awaiting_custom": True,
            "allow_custom": True,
        }
    }
    app = _other_app(pending, SimpleNamespace(id="s46"), qid)

    result = await BotApp.feed_tmux_other_answer(app, 100, "свой текст", owner_chat_id=142987535, context=None)

    assert result == ""
    assert qid in pending
