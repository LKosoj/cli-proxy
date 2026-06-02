from __future__ import annotations

import asyncio
import json
import os
import types
from typing import Any, Dict, List

from modes.sdd.ears import extract_clarification_questions
from modes.sdd.mode import SddMode
from modes.sdd.state import get_sdd_state
from modes.sdk.models import CallbackModel, MessageModel
from modes.sdk.services.messaging import MessagingService


# ---------------------------------------------------------------------------
# Unit tests: extract_clarification_questions
# ---------------------------------------------------------------------------

def test_extract_ac_with_marker():
    payload = {
        "acceptance_criteria": [
            {"ears": "WHEN user clicks the system SHALL respond [NEEDS CLARIFICATION]"},
        ]
    }
    result = extract_clarification_questions(payload)
    assert len(result) == 1
    assert "[NEEDS CLARIFICATION]" in result[0]


def test_extract_requirement_text_with_marker():
    payload = {
        "requirements": [
            {"id": "REQ-1", "text": "System shall do X [NEEDS CLARIFICATION]"},
        ]
    }
    result = extract_clarification_questions(payload)
    assert len(result) == 1
    assert "REQ-1" in result[0] or "NEEDS CLARIFICATION" in result[0]


def test_extract_no_markers_returns_empty():
    payload = {
        "acceptance_criteria": [
            {"ears": "WHEN user clicks the system SHALL respond"},
        ],
        "requirements": [
            {"id": "REQ-1", "text": "System shall do X"},
        ],
    }
    assert extract_clarification_questions(payload) == []


def test_extract_empty_payload_returns_empty():
    assert extract_clarification_questions({}) == []


def test_extract_requirement_marker_case_insensitive():
    payload = {
        "requirements": [
            {"id": "REQ-1", "text": "System shall do X [needs clarification]"},
        ]
    }
    result = extract_clarification_questions(payload)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Integration fakes (reuse pattern from test_sdd_phase_gates.py)
# ---------------------------------------------------------------------------

_SPEC_JSON_WITH_MARKER = json.dumps({
    "feature_slug": "test-clarify",
    "stories": ["As a user I want X"],
    "requirements": [{"id": "REQ-1", "text": "System shall do X"}],
    "acceptance_criteria": [
        {"req_id": "REQ-1", "ears": "WHEN user does X [NEEDS CLARIFICATION] the system SHALL respond"}
    ],
})

_SPEC_JSON_NO_MARKER = json.dumps({
    "feature_slug": "test-no-clarify",
    "stories": ["As a user I want Y"],
    "requirements": [{"id": "REQ-1", "text": "System shall do Y"}],
    "acceptance_criteria": [
        {"req_id": "REQ-1", "ears": "WHEN user does Y the system SHALL respond"}
    ],
})

_PLAN_JSON = json.dumps({
    "architecture": "Layered",
    "stack": ["Python"],
    "constraints": [],
    "risks": [],
})


class _FakeTasksService:
    def __init__(self) -> None:
        self._launched: List[str] = []

    def create(self, *, session_uid: str, mode_id: str, coro: Any, name: str) -> None:
        self._launched.append(name)
        asyncio.ensure_future(coro)

    def list(self, *, session_uid: str, mode_id: str) -> List[str]:
        return []


class _FakeMessagingService(MessagingService):
    def __init__(self) -> None:
        super().__init__()
        self.sent: List[Dict[str, Any]] = []

    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, "markup": kwargs.get("reply_markup")})

    async def send_or_edit(self, *, chat_id: int, text: str, query: Any = None, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text})


class _FakeSessionMutation:
    def persist_all(self) -> bool:
        return True


class _FakeSddRuntimeWithMarker:
    async def chat_completion(self, config: Any, system: str, user: str, **_kw) -> str:
        if "feature_slug" in system and "acceptance_criteria" in system:
            return _SPEC_JSON_WITH_MARKER
        if "architecture" in system and "stack" in system:
            return _PLAN_JSON
        return "{}"


class _FakeSddRuntimeNoMarker:
    async def chat_completion(self, config: Any, system: str, user: str, **_kw) -> str:
        if "feature_slug" in system and "acceptance_criteria" in system:
            return _SPEC_JSON_NO_MARKER
        if "architecture" in system and "stack" in system:
            return _PLAN_JSON
        return "{}"


def _make_session(tmp_path) -> Any:
    from session import SddState
    return types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        modes=types.SimpleNamespace(active_mode=None),
        sdd=SddState(),
    )


def _make_mode(fake_tasks: _FakeTasksService, fake_ms: _FakeMessagingService, runtime: Any) -> SddMode:
    mode = SddMode()
    mode.initialize(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m")
        ),
        services={
            "runtime_by_capability": lambda cap: runtime if cap == "sdd_chat_completion" else None,
            "tasks": fake_tasks,
            "messaging_factory": lambda ctx: fake_ms,
            "session_mutation_service": _FakeSessionMutation(),
        },
    )
    return mode


def _make_bot_app() -> Any:
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m")
        ),
    )


def _make_ctx(session: Any, bot_app: Any) -> Dict[str, Any]:
    return {
        "session": session,
        "bot_app": bot_app,
        "context": None,
        "dest": {"kind": "telegram", "chat_id": 1},
        "query": None,
    }


# ---------------------------------------------------------------------------
# Integration test: spec WITH marker → clarifications.md created, message sent
# ---------------------------------------------------------------------------

def test_clarify_file_created_and_message_sent(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, _FakeSddRuntimeWithMarker())
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Add feature with clarification", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.spec_dir is not None
        clarf_path = os.path.join(str(sdd.spec_dir), "clarifications.md")
        assert os.path.isfile(clarf_path), "clarifications.md must be created"
        content = open(clarf_path, encoding="utf-8").read()
        assert "## Questions" in content
        # Файл содержит сам вопрос (а не только заголовок) с нумерацией
        assert "[NEEDS CLARIFICATION]" in content
        assert "1." in content

        clarify_msgs = [m["text"] for m in fake_ms.sent if "уточнени" in m["text"] or "⚠️" in m["text"]]
        assert clarify_msgs, f"Expected clarification message, got: {[m['text'] for m in fake_ms.sent]}"
        # Сообщение содержит текст вопроса и нумерацию (а не просто эмодзи)
        assert "[NEEDS CLARIFICATION]" in clarify_msgs[0]
        assert "1." in clarify_msgs[0]

        # Gate message must still be sent
        assert sdd.phase == "specify"
        assert sdd.pending_gate == "specify"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Integration test: spec WITHOUT marker → clarifications.md NOT created, gate sent
# ---------------------------------------------------------------------------

def test_no_clarify_file_when_no_markers(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, _FakeSddRuntimeNoMarker())
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Add feature without clarification", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.spec_dir is not None
        clarf_path = os.path.join(str(sdd.spec_dir), "clarifications.md")
        assert not os.path.isfile(clarf_path), "clarifications.md must NOT be created when no markers"

        # Gate message must still be sent
        assert sdd.phase == "specify"
        sent_texts = [m["text"] for m in fake_ms.sent]
        assert any("Спецификация" in t or "spec" in t.lower() or "📋" in t for t in sent_texts), \
            f"Gate message must be sent, got: {sent_texts}"

        # No clarification message
        assert not any("уточнени" in t for t in sent_texts), \
            f"Unexpected clarification message in: {sent_texts}"

    asyncio.run(_run())
