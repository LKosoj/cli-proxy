import asyncio
import json
import types

import pytest

from sessions.session_run_service import SessionRunService
from tg.callbacks import CallbackHandler


class _ModeRegistry:
    def __init__(self, mode):
        if isinstance(mode, dict):
            self._modes = {str(k): v for k, v in mode.items()}
        else:
            self._modes = {"analyst": mode}

    def get(self, mode_id):
        return self._modes.get(str(mode_id))


class _ModeRegistryService:
    def list_modes(self):
        return [("analyst", "Analyst"), ("manager", "Manager")]


class _OrchestratorStub:
    def __init__(self, target_mode_id: str = "manager") -> None:
        self._target_mode_id = str(target_mode_id)

    async def propose_transition_hybrid(self, **_kwargs):
        target = self._target_mode_id
        return types.SimpleNamespace(target_mode_id=target, target_label=str(target).title(), reason="next", confidence=0.9)

    def build_handoff_input(self, *, session, original_user_text: str) -> str:
        orchestrator = getattr(session, "orchestrator", None)
        return str(getattr(orchestrator, "last_mode_output", "") or original_user_text or "")

    def current_mode_label(self, *, session, mode_registry):
        _ = mode_registry
        modes = getattr(session, "modes", None)
        return str(getattr(modes, "active_mode", "") or "Прямой CLI")

    def build_confirm_text(self, *, current_mode_label: str, proposal):
        return f"{current_mode_label} -> {proposal.target_label}"

    def apply_mode(self, *, session, target_mode_id: str) -> None:
        session.modes.active_mode = str(target_mode_id)


class _ModeStub:
    async def run_pipeline(self, *, session, user_text: str, bot_app, context, dest):
        _ = session, user_text, bot_app, context, dest
        return "АНАЛИТИКА: нужен план исполнения"

    def framework_sends_output(self) -> bool:
        return False


class _SessionStub:
    def __init__(self, *, session_uid: str = "chat:1"):
        self.id = "s1"
        self.conversation_scope = types.SimpleNamespace(session_uid=str(session_uid))
        self.run_lock = asyncio.Lock()
        self.queue = []
        self.busy = False
        self.started_at = 0.0
        self.last_output_ts = 0.0
        self.last_tick_ts = None
        self.last_tick_value = None
        self.tick_seen = 0
        self.modes = types.SimpleNamespace(active_mode="analyst", analyst_mode="spec")
        self.orchestrator = types.SimpleNamespace(
            enabled=True,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        )
        self.state_summary = None
        self.state_updated_at = None


@pytest.mark.asyncio
async def test_session_run_service_offers_post_run_orchestrator_transition():
    mode = _ModeStub()
    sent = []
    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistry(mode),
        mode_registry_service=_ModeRegistryService(),
        advanced_orchestrator_service=_OrchestratorStub(),
        orchestrator_chat_completion=None,
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=1000)),
        send_output=(lambda *_a, **_k: asyncio.sleep(0)),
        _send_message=(lambda _ctx, **kwargs: sent.append(kwargs) or asyncio.sleep(0)),
        _handle_user_input=(lambda *_a, **_k: asyncio.sleep(0)),
    )
    svc = SessionRunService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        mode_tasks_list=lambda **_kwargs: [],
        mode_tasks_create=lambda **_kwargs: None,
        log_cli_dialog=lambda *_args, **_kwargs: None,
        reset_session_fields_like_sessions_reset=lambda *_args, **_kwargs: None,
    )
    session = _SessionStub(session_uid="forum:-100777000111:101")

    await svc.run_mode_pipeline(session, "сделай рефакторинг", {"chat_id": 1}, context=None, mode_id="analyst")

    assert isinstance(session.orchestrator.pending_input, dict)
    assert session.orchestrator.pending_input.get("target_mode_id") == "manager"
    assert session.orchestrator.pending_input.get("disable_orchestrator_on_cancel") is True
    assert sent and "reply_markup" in sent[-1]
    buttons = sent[-1]["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == "orch_transition:apply:forum:-100777000111:101:manager"
    assert buttons[1].callback_data == "orch_transition:cancel:forum:-100777000111:101"


@pytest.mark.asyncio
async def test_orch_transition_cancel_with_disable_flag_turns_orchestrator_off():
    session = types.SimpleNamespace(
        id="s1",
        conversation_scope=types.SimpleNamespace(session_uid="chat:1"),
        modes=types.SimpleNamespace(active_mode="analyst", analyst_mode="spec"),
        orchestrator=types.SimpleNamespace(
            enabled=True,
            pending_input={
                "text": "ANALYST OUT",
                "dest": {"kind": "telegram", "chat_id": 1},
                "target_mode_id": "manager",
                "disable_orchestrator_on_cancel": True,
            },
            last_mode_output=None,
            last_mode_id=None,
        ),
    )
    called = {"dispatch": 0}

    class _Manager:
        def get(self, _chat_id, _sid):
            return session

        def get_by_uid(self, session_uid):
            if str(session_uid or "") == str(session.conversation_scope.session_uid):
                return session
            return None

    bot_app = types.SimpleNamespace(
        manager=_Manager(),
        mode_session_control=types.SimpleNamespace(persist=lambda: None),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=(
                lambda *_args, **_kwargs: called.__setitem__("dispatch", called["dispatch"] + 1) or asyncio.sleep(0)
            )
        ),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=1, message_id=10))

    ok = await handler._cb_orch_transition(
        data=f"orch_transition:cancel:{session.conversation_scope.session_uid}",
        chat_id=1,
        query=query,
        context=object(),
    )

    assert ok is True
    assert session.orchestrator.enabled is False
    assert called["dispatch"] == 0


@pytest.mark.asyncio
async def test_session_run_service_post_run_allows_back_transition_with_user_confirmation():
    mode = _ModeStub()
    sent = []
    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistry(mode),
        mode_registry_service=_ModeRegistryService(),
        advanced_orchestrator_service=_OrchestratorStub(target_mode_id="analyst"),
        orchestrator_chat_completion=None,
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=1000)),
        send_output=(lambda *_a, **_k: asyncio.sleep(0)),
        _send_message=(lambda _ctx, **kwargs: sent.append(kwargs) or asyncio.sleep(0)),
        _handle_user_input=(lambda *_a, **_k: asyncio.sleep(0)),
    )
    svc = SessionRunService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        mode_tasks_list=lambda **_kwargs: [],
        mode_tasks_create=lambda **_kwargs: None,
        log_cli_dialog=lambda *_args, **_kwargs: None,
        reset_session_fields_like_sessions_reset=lambda *_args, **_kwargs: None,
    )
    session = _SessionStub()
    session._orchestrator_prev_mode_id = "analyst"

    await svc.run_mode_pipeline(session, "сделай рефакторинг", {"chat_id": 1}, context=None, mode_id="analyst")

    assert isinstance(session.orchestrator.pending_input, dict)
    assert session.orchestrator.pending_input.get("target_mode_id") == "analyst"
    assert sent


@pytest.mark.asyncio
async def test_session_run_service_manager_handoff_keeps_full_report_and_parses_it():
    full_report = json.dumps(
        {
            "status": "ok",
            "summary": "Менеджер завершил задачу",
            "acceptance_criteria_report": [
                {
                    "criterion": "Все пункты выполнены",
                    "status": "done",
                    "evidence": "pytest -q",
                }
            ],
            "checklist_table": [
                {
                    "item": "Пункт чеклиста",
                    "status": "done",
                    "how": "Проверено тестом",
                    "why_not": "",
                }
            ],
            "tests": [{"command": ".venv/bin/pytest -q", "result": "passed", "details": "ok"}],
            "lint": [{"command": ".venv/bin/flake8", "result": "passed", "details": "ok"}],
        },
        ensure_ascii=False,
    )

    class _ManagerModeStub:
        async def run_pipeline(self, *, session, user_text: str, bot_app, context, dest):
            _ = session, user_text, bot_app, context, dest
            return full_report

        def framework_sends_output(self) -> bool:
            return False

    mode = _ManagerModeStub()
    sent = []
    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistry({"manager": mode}),
        mode_registry_service=_ModeRegistryService(),
        advanced_orchestrator_service=_OrchestratorStub(target_mode_id="analyst"),
        orchestrator_chat_completion=None,
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=1000)),
        send_output=(lambda *_a, **_k: asyncio.sleep(0)),
        _send_message=(lambda _ctx, **kwargs: sent.append(kwargs) or asyncio.sleep(0)),
        _handle_user_input=(lambda *_a, **_k: asyncio.sleep(0)),
    )
    svc = SessionRunService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        mode_tasks_list=lambda **_kwargs: [],
        mode_tasks_create=lambda **_kwargs: None,
        log_cli_dialog=lambda *_args, **_kwargs: None,
        reset_session_fields_like_sessions_reset=lambda *_args, **_kwargs: None,
    )
    session = _SessionStub()
    session.modes.active_mode = "manager"

    await svc.run_mode_pipeline(session, "сделай handoff", {"chat_id": 1}, context=None, mode_id="manager")

    assert session.orchestrator.last_mode_output == full_report
    assert isinstance(session.orchestrator.pending_input, dict)
    assert session.orchestrator.pending_input.get("text") == full_report
    payload = json.loads(str(session.orchestrator.pending_input.get("text") or ""))
    assert payload.get("status") == "ok"
    assert payload.get("summary") == "Менеджер завершил задачу"
    assert sent and "reply_markup" in sent[-1]


@pytest.mark.asyncio
async def test_orchestrator_handoff_isolated_for_two_sequential_manager_runs():
    full_report_first = json.dumps(
        {
            "status": "ok",
            "summary": "Полный отчет #1",
            "_plan_summary": "Коротко #1",
            "intent": "first_intent",
            "acceptance_criteria_report": [
                {"criterion": "first", "status": "done", "evidence": "e1"},
            ],
            "checklist_table": [
                {"item": "row1", "status": "done", "how": "h1", "why_not": ""},
            ],
            "tests": [{"command": "pytest", "result": "passed", "details": "first"}],
            "lint": [{"command": "flake8", "result": "passed", "details": "first"}],
        },
        ensure_ascii=False,
    )
    full_report_second = json.dumps(
        {
            "status": "ok",
            "summary": "Полный отчет #2",
            "_plan_summary": "Коротко #2",
            "intent": "second_intent",
            "acceptance_criteria_report": [
                {"criterion": "second", "status": "done", "evidence": "e2"},
            ],
            "checklist_table": [
                {"item": "row2", "status": "done", "how": "h2", "why_not": ""},
            ],
            "tests": [{"command": "pytest", "result": "passed", "details": "second"}],
            "lint": [{"command": "flake8", "result": "passed", "details": "second"}],
        },
        ensure_ascii=False,
    )
    manager_reports = [full_report_first, full_report_second]

    class _SequentialManagerMode:
        def __init__(self, reports):
            self._reports = list(reports)
            self.calls = []

        async def run_pipeline(self, *, session, user_text: str, bot_app, context, dest):
            _ = session, bot_app, context, dest
            self.calls.append(str(user_text))
            idx = len(self.calls) - 1
            return self._reports[idx]

        def framework_sends_output(self) -> bool:
            return False

    mode = _SequentialManagerMode(manager_reports)
    sent = []
    edited = []
    handed_off = []

    async def _send_output(*_args, **_kwargs):
        return None

    async def _send_message(_ctx, **kwargs):
        sent.append(dict(kwargs))
        return None

    async def _edit_message(_ctx, **kwargs):
        edited.append(dict(kwargs))
        return True

    async def _dispatch_no_orch(session, text, chat_id, context, *, dest=None):
        _ = chat_id, context
        modes = getattr(session, "modes", None)
        handed_off.append(
            {
                "mode_after_apply": str(getattr(modes, "active_mode", "") or ""),
                "text": str(text or ""),
                "dest": dict(dest or {}),
            }
        )
        return None

    session = _SessionStub()
    session.modes.active_mode = "manager"

    class _Manager:
        def get(self, _chat_id, sid):
            if str(sid or "") == str(session.id or ""):
                return session
            return None

        def get_by_uid(self, session_uid):
            if str(session_uid or "") == str(session.conversation_scope.session_uid):
                return session
            return None

    bot_app = types.SimpleNamespace(
        mode_registry=_ModeRegistry({"manager": mode}),
        mode_registry_service=_ModeRegistryService(),
        advanced_orchestrator_service=_OrchestratorStub(target_mode_id="analyst"),
        orchestrator_chat_completion=None,
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(summary_max_chars=1000)),
        send_output=_send_output,
        _send_message=_send_message,
        _edit_message=_edit_message,
        _handle_user_input=(lambda *_a, **_k: asyncio.sleep(0)),
        manager=_Manager(),
        mode_session_control=types.SimpleNamespace(persist=lambda: None),
        input_dispatch_service=types.SimpleNamespace(
            handle_user_input_no_orchestration=_dispatch_no_orch,
        ),
    )
    svc = SessionRunService(
        bot_app=bot_app,
        persist_sessions=lambda: None,
        mode_tasks_list=lambda **_kwargs: [],
        mode_tasks_create=lambda **_kwargs: None,
        log_cli_dialog=lambda *_args, **_kwargs: None,
        reset_session_fields_like_sessions_reset=lambda *_args, **_kwargs: None,
    )
    handler = CallbackHandler(bot_app)
    query = types.SimpleNamespace(message=types.SimpleNamespace(chat_id=1, message_id=10))

    intents = ["intent:first", "intent:second"]
    expected_intents = ["first_intent", "second_intent"]

    for idx, intent in enumerate(intents):
        session.modes.active_mode = "manager"
        await svc.run_mode_pipeline(session, intent, {"chat_id": 1}, context=None, mode_id="manager")

        expected_report = manager_reports[idx]
        expected_payload = json.loads(expected_report)

        assert session.orchestrator.last_mode_output == expected_report
        assert isinstance(session.orchestrator.pending_input, dict)
        assert session.orchestrator.pending_input.get("text") == expected_report
        assert json.loads(str(session.orchestrator.pending_input.get("text") or "")) == expected_payload

        ok = await handler._cb_orch_transition(
            data=f"orch_transition:apply:{session.conversation_scope.session_uid}:analyst",
            chat_id=1,
            query=query,
            context=object(),
        )
        assert ok is True
        assert session.modes.active_mode == "analyst"
        assert session.orchestrator.pending_input is None

        assert handed_off[-1]["text"] == expected_report
        dispatched_payload = json.loads(handed_off[-1]["text"])
        assert dispatched_payload.get("summary") == expected_payload.get("summary")
        assert dispatched_payload.get("intent") == expected_intents[idx]
        assert handed_off[-1]["text"] != dispatched_payload.get("_plan_summary")

    assert mode.calls == intents
    assert len(handed_off) == 2
    assert handed_off[0]["text"] != handed_off[1]["text"]
    assert [json.loads(item["text"]).get("intent") for item in handed_off] == expected_intents
    assert sent and all("reply_markup" in item for item in sent)
    assert edited and len(edited) == 2
