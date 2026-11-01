from types import SimpleNamespace

import pytest

from app.services.cli_backends.tmux_backend import TmuxExecutionBackend
from app.services.telegram_ui_scope import TelegramUiKey
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

    monkeypatch.setattr(TmuxExecutionBackend, "send_input", _send)
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
    assert query.edits[-1] == t("msg.input.question_stale", "ru")


@pytest.mark.asyncio
async def test_tmux_choice_click_keeps_card_when_pane_send_fails(monkeypatch):
    session = SimpleNamespace(id="s45", chat_id=142987535)
    attempts = []

    async def _send(_self, _tmux_sess, text):
        attempts.append(text)
        raise RuntimeError("pane down")

    monkeypatch.setattr(TmuxExecutionBackend, "send_input", _send)
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
