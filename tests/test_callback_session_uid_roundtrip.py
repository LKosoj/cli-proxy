import asyncio
import types

import pytest

from app.services.session_transfer.canonical import CanonicalMessage, CanonicalSession
from modes.sdk.services.callback_data import parse_compact_callback_payload
from sessions.conversation_scope import ConversationScope
from tg.callbacks import CallbackHandler


COMPLEX_SESSION_UID = "forum:-100777000111:101"

CALLBACK_UID_ROUNDTRIP_INVENTORY = {
    "orch_transition": [
        {
            "name": "apply",
            "callback_data": f"orch_transition:apply:{COMPLEX_SESSION_UID}:manager",
            "expected_lookups": [COMPLEX_SESSION_UID],
            "expected_mode_id": "manager",
        },
        {
            "name": "cancel",
            "callback_data": f"orch_transition:cancel:{COMPLEX_SESSION_UID}",
            "expected_lookups": [COMPLEX_SESSION_UID],
            "expected_mode_id": "analyst",
        },
    ],
    "sess_mode_pick": [
        {
            "name": "session_only",
            "callback_data": f"sess_mode_pick:{COMPLEX_SESSION_UID}",
            "expected_lookups": [COMPLEX_SESSION_UID],
            "expected_menu": "MENU:analyst",
        },
        {
            "name": "session_and_mode",
            "callback_data": f"sess_mode_pick:{COMPLEX_SESSION_UID}:manager",
            "expected_lookups": [f"{COMPLEX_SESSION_UID}:manager", COMPLEX_SESSION_UID],
            "expected_menu": "MENU:manager",
        },
    ],
}

BASELINE_MUTABLE_CALLBACK_DATA_INVENTORY = [
    {
        "prefix": "orch_transition",
        "name": "apply",
        "callback_data": f"orch_transition:apply:{COMPLEX_SESSION_UID}:manager",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "orch_transition",
        "name": "cancel",
        "callback_data": f"orch_transition:cancel:{COMPLEX_SESSION_UID}",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "sess_active_pick",
        "name": "active_pick",
        "callback_data": f"sess_active_pick:{COMPLEX_SESSION_UID}",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "sess_mode_pick",
        "name": "session_only",
        "callback_data": f"sess_mode_pick:{COMPLEX_SESSION_UID}",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "sess_mode_pick",
        "name": "session_and_mode",
        "callback_data": f"sess_mode_pick:{COMPLEX_SESSION_UID}:manager",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "sess_transfer_yes",
        "name": "transfer_yes",
        "callback_data": f"sess_transfer_yes:{COMPLEX_SESSION_UID}:claude",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "sess_transfer_no",
        "name": "transfer_no",
        "callback_data": f"sess_transfer_no:{COMPLEX_SESSION_UID}",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "ma:agent:plugins",
        "name": "agent_plugins",
        "callback_data": f"ma:agent:plugins:s={COMPLEX_SESSION_UID}",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
    {
        "prefix": "ma:agent:plugin",
        "name": "agent_plugin",
        "callback_data": f"ma:agent:plugin:s={COMPLEX_SESSION_UID}|p=plugin-alpha",
        "expected_session_uid": COMPLEX_SESSION_UID,
    },
]


def _extract_inventory_session_uid(case: dict) -> str:
    data = str(case["callback_data"])
    prefix = str(case["prefix"])
    name = str(case["name"])
    if prefix == "orch_transition":
        parts = data.split(":")
        if name == "apply":
            return ":".join(parts[2:-1])
        return ":".join(parts[2:])
    if prefix == "sess_active_pick":
        return data.split(":", 1)[1]
    if prefix == "sess_mode_pick":
        token = data.split(":", 1)[1]
        if name == "session_and_mode":
            return token.rsplit(":", 1)[0]
        return token
    if prefix == "sess_transfer_yes":
        return data.split(":", 1)[1].rsplit(":", 1)[0]
    if prefix == "sess_transfer_no":
        return data.split(":", 1)[1]
    if prefix in {"ma:agent:plugins", "ma:agent:plugin"}:
        payload = data.split(":", 3)[3]
        return parse_compact_callback_payload(payload)["s"]
    raise AssertionError(f"unhandled inventory prefix: {prefix}")


class _StrictManager:
    def __init__(self, session) -> None:
        self._session = session
        self.lookups: list[str] = []

    def get_by_uid(self, session_uid: str):
        token = str(session_uid or "")
        self.lookups.append(token)
        if token == str(self._session.conversation_scope.session_uid):
            return self._session
        return None


class _OrchestratorService:
    def apply_mode(self, *, session, target_mode_id: str) -> None:
        session.modes.active_mode = str(target_mode_id)


class _MenuPlugin:
    def __init__(self, mode_id: str) -> None:
        self.mode_id = str(mode_id)

    def build_menu(self, session, back_callback="sess_active", back_text="⬅️ Назад"):
        _ = session, back_callback, back_text
        return f"MENU:{self.mode_id}", None


class _ModeRegistryService:
    def __init__(self) -> None:
        self._plugins = {
            "analyst": _MenuPlugin("analyst"),
            "manager": _MenuPlugin("manager"),
        }

    def get(self, mode_id):
        return self._plugins.get(str(mode_id))


def test_baseline_mutable_callback_data_inventory_has_colon_session_uid_payloads() -> None:
    expected_prefixes = {
        "orch_transition",
        "sess_active_pick",
        "sess_mode_pick",
        "sess_transfer_yes",
        "sess_transfer_no",
        "ma:agent:plugins",
        "ma:agent:plugin",
    }

    assert {case["prefix"] for case in BASELINE_MUTABLE_CALLBACK_DATA_INVENTORY} == expected_prefixes
    for case in BASELINE_MUTABLE_CALLBACK_DATA_INVENTORY:
        extracted = _extract_inventory_session_uid(case)
        assert extracted == case["expected_session_uid"] == COMPLEX_SESSION_UID
        assert ":" in extracted


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CALLBACK_UID_ROUNDTRIP_INVENTORY["orch_transition"])
async def test_orchestrator_callback_roundtrip_uses_exact_session_uid(case):
    session = types.SimpleNamespace(
        id="s1",
        conversation_scope=types.SimpleNamespace(session_uid=COMPLEX_SESSION_UID),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "handoff",
                "dest": {"kind": "telegram", "chat_id": 1},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )
    manager = _StrictManager(session)
    handed_off = []

    async def _handle_user_input_no_orchestration(session, text, chat_id, context, *, dest=None):
        _ = context
        handed_off.append(
            {
                "mode_id": str(getattr(session.modes, "active_mode", "") or ""),
                "text": str(text or ""),
                "chat_id": int(chat_id),
                "dest": dict(dest or {}),
            }
        )
        return None

    bot_app = types.SimpleNamespace(
        manager=manager,
        advanced_orchestrator_service=_OrchestratorService(),
        mode_session_control=types.SimpleNamespace(persist=lambda: None),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=_handle_user_input_no_orchestration,
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)

    ok = await handler._cb_orch_transition(
        data=case["callback_data"],
        chat_id=1,
        query=types.SimpleNamespace(message=types.SimpleNamespace(chat_id=1, message_id=10)),
        context=object(),
    )

    assert ok is True
    assert manager.lookups == case["expected_lookups"]
    assert session.modes.active_mode == case["expected_mode_id"]
    assert session.orchestrator.pending_input is None
    assert handed_off == [
        {
            "mode_id": case["expected_mode_id"],
            "text": "handoff",
            "chat_id": 1,
            "dest": {"kind": "telegram", "chat_id": 1},
        }
    ]


@pytest.mark.asyncio
async def test_orchestrator_callback_keeps_pending_input_on_stale_apply_callback() -> None:
    session = types.SimpleNamespace(
        id="s1",
        conversation_scope=types.SimpleNamespace(session_uid=COMPLEX_SESSION_UID),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "handoff",
                "dest": {"kind": "telegram", "chat_id": 1},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )
    manager = _StrictManager(session)
    handed_off = []
    edited = {"text": None}

    async def _handle_user_input_no_orchestration(_session, _text, _chat_id, _context, *, dest=None):
        handed_off.append(dict(dest or {}))
        return None

    bot_app = types.SimpleNamespace(
        manager=manager,
        advanced_orchestrator_service=_OrchestratorService(),
        mode_session_control=types.SimpleNamespace(persist=lambda: None),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=_handle_user_input_no_orchestration,
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup, md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    ok = await handler._cb_orch_transition(
        data=f"orch_transition:apply:{COMPLEX_SESSION_UID}:analyst",
        chat_id=1,
        query=types.SimpleNamespace(message=types.SimpleNamespace(chat_id=1, message_id=10)),
        context=object(),
    )

    assert ok is True
    assert manager.lookups == [COMPLEX_SESSION_UID]
    assert session.modes.active_mode == "analyst"
    assert session.orchestrator.pending_input == {
        "text": "handoff",
        "dest": {"kind": "telegram", "chat_id": 1},
        "target_mode_id": "manager",
        "disable_orchestrator_on_cancel": False,
    }
    assert handed_off == []
    assert edited["text"] == "Переход устарел. Отправьте сообщение снова."


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CALLBACK_UID_ROUNDTRIP_INVENTORY["sess_mode_pick"])
async def test_session_menu_callback_roundtrip_parses_colon_bearing_session_uid(case):
    session = types.SimpleNamespace(
        id="s1",
        conversation_scope=types.SimpleNamespace(session_uid=COMPLEX_SESSION_UID),
        modes=types.SimpleNamespace(active_mode="analyst"),
    )
    manager = _StrictManager(session)
    bot_app = types.SimpleNamespace(
        manager=manager,
        handlers=types.SimpleNamespace(
            build_sessions_active_overview=lambda owner_chat_id, session: ("OVERVIEW", None),
        ),
        mode_registry_service=_ModeRegistryService(),
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handler = CallbackHandler(bot_app)
    edited = {"text": None}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup
        _ = md2
        edited["text"] = str(text or "")
        return True

    handler._edit_msg = _fake_edit_msg
    ok = await handler._cb_sess_mode_pick(
        data=case["callback_data"],
        chat_id=1,
        query=types.SimpleNamespace(message=types.SimpleNamespace(chat_id=1, message_id=10)),
        context=object(),
    )

    assert ok is True
    assert manager.lookups == case["expected_lookups"]
    assert edited["text"] == case["expected_menu"]


@pytest.mark.asyncio
async def test_session_transfer_yes_callback_roundtrip_parses_colon_bearing_session_uid(monkeypatch):
    from app.services.session_transfer import service as transfer_service

    session = types.SimpleNamespace(
        id="s1",
        chat_id=1,
        conversation_scope=types.SimpleNamespace(session_uid=COMPLEX_SESSION_UID),
        cli=types.SimpleNamespace(
            active_cli="codex",
            resume_tokens={"claude": "claude-source-token"},
        ),
        workdir="/repo",
        resume_token="",
    )
    manager = _StrictManager(session)
    persisted = []
    manager.persist_session = lambda chat_id, session_id: persisted.append((chat_id, session_id)) or True
    bot_app = types.SimpleNamespace(
        manager=manager,
        mode_session_control=types.SimpleNamespace(persist=lambda: None),
        config=types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={}),
            defaults=types.SimpleNamespace(default_language="ru"),
        ),
    )
    handler = CallbackHandler(bot_app)
    edited = {"text": None}
    calls = {}

    async def _fake_edit_msg(_context, _query, text, *, reply_markup=None, md2=True):
        _ = reply_markup, md2
        edited["text"] = str(text or "")
        return True

    def _extract_session(source_cli, session_id, workspace):
        calls["extract"] = (source_cli, session_id, workspace)
        return CanonicalSession(
            source_cli=source_cli,
            session_id=session_id,
            workspace=workspace,
            messages=[CanonicalMessage(role="user", content="handoff")],
        )

    def _write_target_session(canonical, target_cli, workspace):
        calls["write"] = (canonical, target_cli, workspace)
        return "codex-new-token"

    handler._edit_msg = _fake_edit_msg
    monkeypatch.setattr(transfer_service, "extract_session", _extract_session)
    monkeypatch.setattr(transfer_service, "write_target_session", _write_target_session)

    ok = await handler._cb_sess_transfer_yes(
        data=f"sess_transfer_yes:{COMPLEX_SESSION_UID}:claude",
        chat_id=1,
        query=types.SimpleNamespace(message=types.SimpleNamespace(chat_id=1, message_id=10)),
        context=object(),
    )

    assert ok is True
    assert manager.lookups == [COMPLEX_SESSION_UID]
    assert calls["extract"] == ("claude", "claude-source-token", "/repo")
    assert calls["write"][1:] == ("codex", "/repo")
    assert session.resume_token == "codex-new-token"
    assert persisted == [(1, "s1")]
    assert edited["text"] == (
        "Сформирован компактный перенос из 1 сообщений claude -> codex. "
        "Сессия будет продолжена в codex."
    )


@pytest.mark.asyncio
async def test_orchestrator_callback_clears_stale_pending_when_legacy_raw_session_id_no_longer_resolves() -> None:
    session = types.SimpleNamespace(
        id="s1",
        chat_id=42,
        conversation_scope=types.SimpleNamespace(session_uid=COMPLEX_SESSION_UID),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "handoff",
                "dest": {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 101},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )
    unrelated_session = types.SimpleNamespace(
        id="shadow",
        chat_id=77,
        conversation_scope=ConversationScope.from_parts(chat_id=-100555000222, message_thread_id=202),
        modes=types.SimpleNamespace(active_mode="manager"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "other-chat-handoff",
                "dest": {"kind": "telegram", "chat_id": -100555000222, "message_thread_id": 202},
                "target_mode_id": "analyst",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )

    class _ScopeAwareManager(_StrictManager):
        def __init__(self, scoped_session, unrelated) -> None:
            super().__init__(scoped_session)
            self.sessions_by_chat = {
                42: {scoped_session.id: scoped_session},
                77: {unrelated.id: unrelated},
            }

        def get_by_uid(self, session_uid: str):
            token = str(session_uid or "")
            self.lookups.append(token)
            return None

        def get_by_scope(self, chat_id: int, message_thread_id: int | None = None):
            if int(chat_id) == -100777000111 and int(message_thread_id or 0) == 101:
                return session
            return None

    manager = _ScopeAwareManager(session, unrelated_session)
    persist_calls = []
    bot_app = types.SimpleNamespace(
        manager=manager,
        mode_session_control=types.SimpleNamespace(persist=lambda: persist_calls.append("persist")),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=(lambda *_a, **_k: asyncio.sleep(0)),
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=101))

    ok = await handler._cb_orch_transition(
        data="orch_transition:cancel:s1",
        chat_id=-100777000111,
        query=query,
        context=object(),
    )

    assert ok is True
    assert manager.lookups == ["s1"]
    assert session.orchestrator.pending_input is None
    assert unrelated_session.orchestrator.pending_input is not None
    assert persist_calls == ["persist"]


@pytest.mark.asyncio
async def test_orchestrator_callback_does_not_clear_stale_pending_in_other_chat_by_raw_session_id() -> None:
    unrelated_session = types.SimpleNamespace(
        id="legacy-raw",
        chat_id=77,
        conversation_scope=ConversationScope.from_parts(chat_id=-100555000222, message_thread_id=202),
        modes=types.SimpleNamespace(active_mode="manager"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "other-chat-handoff",
                "dest": {"kind": "telegram", "chat_id": -100555000222, "message_thread_id": 202},
                "target_mode_id": "analyst",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )

    class _CrossChatManager:
        def __init__(self) -> None:
            self.lookups: list[str] = []
            self.sessions_by_chat = {77: {unrelated_session.id: unrelated_session}}

        def get_by_uid(self, session_uid: str):
            self.lookups.append(str(session_uid or ""))
            return None

        def get_by_scope(self, chat_id: int, message_thread_id: int | None = None):
            _ = chat_id, message_thread_id
            return None

    manager = _CrossChatManager()
    persist_calls = []
    bot_app = types.SimpleNamespace(
        manager=manager,
        mode_session_control=types.SimpleNamespace(persist=lambda: persist_calls.append("persist")),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=(lambda *_a, **_k: asyncio.sleep(0)),
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=101))

    ok = await handler._cb_orch_transition(
        data="orch_transition:cancel:legacy-raw",
        chat_id=-100777000111,
        query=query,
        context=object(),
    )

    assert ok is True
    assert manager.lookups == ["legacy-raw"]
    assert unrelated_session.orchestrator.pending_input is not None
    assert persist_calls == []


@pytest.mark.asyncio
async def test_orchestrator_callback_does_not_clear_stale_pending_by_scanning_sessions_for_canonical_uid() -> None:
    session = types.SimpleNamespace(
        id="s1",
        chat_id=42,
        conversation_scope=ConversationScope.from_parts(chat_id=-100777000111, message_thread_id=101),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "handoff",
                "dest": {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 101},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )

    class _CanonicalFallbackManager:
        def __init__(self) -> None:
            self.lookups: list[str] = []
            self.sessions_by_chat = {42: {session.id: session}}

        def get_by_uid(self, session_uid: str):
            self.lookups.append(str(session_uid or ""))
            return None

        def get_by_scope(self, chat_id: int, message_thread_id: int | None = None):
            _ = chat_id, message_thread_id
            return None

    manager = _CanonicalFallbackManager()
    persist_calls = []
    bot_app = types.SimpleNamespace(
        manager=manager,
        mode_session_control=types.SimpleNamespace(persist=lambda: persist_calls.append("persist")),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=(lambda *_a, **_k: asyncio.sleep(0)),
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=101))

    ok = await handler._cb_orch_transition(
        data=f"orch_transition:cancel:{COMPLEX_SESSION_UID}",
        chat_id=-100777000111,
        query=query,
        context=object(),
    )

    assert ok is True
    assert manager.lookups == [COMPLEX_SESSION_UID, COMPLEX_SESSION_UID]
    assert session.orchestrator.pending_input is not None
    assert persist_calls == []


@pytest.mark.asyncio
async def test_orchestrator_callback_does_not_clear_stale_pending_when_scope_is_ambiguous() -> None:
    shared_scope = ConversationScope.from_parts(chat_id=-100777000111, message_thread_id=101)
    session_one = types.SimpleNamespace(
        id="s1",
        chat_id=42,
        conversation_scope=shared_scope,
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "handoff-one",
                "dest": {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 101},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )
    session_two = types.SimpleNamespace(
        id="s2",
        chat_id=42,
        conversation_scope=shared_scope,
        modes=types.SimpleNamespace(active_mode="manager"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "handoff-two",
                "dest": {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 101},
                "target_mode_id": "analyst",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )

    class _AmbiguousScopeManager:
        def __init__(self) -> None:
            self.lookups: list[str] = []
            self.sessions_by_chat = {42: {"s1": session_one, "s2": session_two}}

        def get_by_uid(self, session_uid: str):
            self.lookups.append(str(session_uid or ""))
            return None

        def get_by_scope(self, chat_id: int, message_thread_id: int | None = None):
            _ = chat_id, message_thread_id
            return None

    manager = _AmbiguousScopeManager()
    persist_calls = []
    bot_app = types.SimpleNamespace(
        manager=manager,
        mode_session_control=types.SimpleNamespace(persist=lambda: persist_calls.append("persist")),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=(lambda *_a, **_k: asyncio.sleep(0)),
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=101))

    ok = await handler._cb_orch_transition(
        data="orch_transition:cancel:s1",
        chat_id=-100777000111,
        query=query,
        context=object(),
    )

    assert ok is True
    assert manager.lookups == ["s1"]
    assert session_one.orchestrator.pending_input is not None
    assert session_two.orchestrator.pending_input is not None
    assert persist_calls == []


@pytest.mark.asyncio
async def test_orchestrator_callback_does_not_clear_stale_pending_when_scope_session_does_not_match_token() -> None:
    scoped_session = types.SimpleNamespace(
        id="scoped-s1",
        chat_id=42,
        conversation_scope=ConversationScope.from_parts(chat_id=-100777000111, message_thread_id=101),
        modes=types.SimpleNamespace(active_mode="analyst"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "scoped-handoff",
                "dest": {"kind": "telegram", "chat_id": -100777000111, "message_thread_id": 101},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )
    unrelated_session = types.SimpleNamespace(
        id="legacy-raw",
        chat_id=77,
        conversation_scope=None,
        modes=types.SimpleNamespace(active_mode="manager"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "unrelated-handoff",
                "dest": {"kind": "telegram", "chat_id": 77},
                "target_mode_id": "analyst",
                "disable_orchestrator_on_cancel": False,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )

    class _ScopeMismatchManager:
        def __init__(self) -> None:
            self.lookups: list[str] = []
            self.sessions_by_chat = {
                42: {scoped_session.id: scoped_session},
                77: {unrelated_session.id: unrelated_session},
            }

        def get_by_uid(self, session_uid: str):
            self.lookups.append(str(session_uid or ""))
            return None

        def get_by_scope(self, chat_id: int, message_thread_id: int | None = None):
            if int(chat_id) == -100777000111 and int(message_thread_id or 0) == 101:
                return scoped_session
            return None

    manager = _ScopeMismatchManager()
    persist_calls = []
    bot_app = types.SimpleNamespace(
        manager=manager,
        mode_session_control=types.SimpleNamespace(persist=lambda: persist_calls.append("persist")),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=(lambda *_a, **_k: asyncio.sleep(0)),
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=-100777000111, message_id=10, message_thread_id=101))

    ok = await handler._cb_orch_transition(
        data="orch_transition:cancel:legacy-raw",
        chat_id=-100777000111,
        query=query,
        context=object(),
    )

    assert ok is True
    assert manager.lookups == ["legacy-raw"]
    assert scoped_session.orchestrator.pending_input is not None
    assert unrelated_session.orchestrator.pending_input is not None
    assert persist_calls == []
