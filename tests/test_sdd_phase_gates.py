from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
import types
from pathlib import Path
from typing import Any, Dict, List

from modes.sdd.mode import SddMode
from modes.sdd.phases import normalize_spec_dir
from modes.sdd.state import get_sdd_state
from modes.sdk.models import CallbackModel, MessageModel
from modes.sdk.services.messaging import MessagingService
from sessions.session_state_access import get_active_mode, set_active_mode


# ---------------------------------------------------------------------------
# Canonical JSON responses for each phase
# ---------------------------------------------------------------------------

_SPEC_JSON = json.dumps({
    "feature_slug": "test-feature",
    "stories": ["As a user I want X so that Y"],
    "requirements": [{"id": "REQ-1", "text": "System shall do X"}],
    "acceptance_criteria": [
        {"req_id": "REQ-1", "ears": "WHEN user clicks X THE SYSTEM SHALL respond"}
    ],
})

_PLAN_JSON = json.dumps({
    "architecture": "Layered architecture",
    "stack": ["Python", "aiohttp"],
    "constraints": ["Must be async"],
    "risks": ["LLM latency"],
})

_TASKS_JSON = json.dumps({
    "project_goal": "Implement test feature",
    "tasks": [
        {
            "id": "TASK-1",
            "title": "Implement core logic",
            "description": "Do the work",
            "acceptance_criteria": ["WHEN done THE SYSTEM SHALL pass"],
            "covers_requirements": ["REQ-1"],
            "depends_on": [],
        }
    ],
})

_PHASE_RESPONSES: Dict[str, str] = {
    "specify": _SPEC_JSON,
    "plan": _PLAN_JSON,
    "tasks": _TASKS_JSON,
}


# ---------------------------------------------------------------------------
# Fake tasks service
# ---------------------------------------------------------------------------

class _FakeTasksService:
    """Immediately schedules coroutines as asyncio tasks."""

    def __init__(self) -> None:
        self._launched: List[str] = []
        self._tasks: List[asyncio.Task] = []
        self.active_names: List[str] = []

    def create(self, *, session_uid: str, mode_id: str, coro: Any, name: str) -> None:
        self._launched.append(name)
        self._tasks.append(asyncio.ensure_future(coro))

    def list(self, *, session_uid: str, mode_id: str) -> List[str]:
        return list(self.active_names)


# ---------------------------------------------------------------------------
# Fake runtime for LLM seam injection
# ---------------------------------------------------------------------------

class _FakeSddRuntime:
    """Returns canned JSON based on which phase is active in the system prompt."""

    async def chat_completion(self, config: Any, system: str, user: str, **_kw) -> str:
        # Detect phase from system prompt content
        if "feature_slug" in system and "acceptance_criteria" in system:
            return _SPEC_JSON
        if "architecture" in system and "stack" in system:
            return _PLAN_JSON
        if "project_goal" in system and "tasks" in system:
            return _TASKS_JSON
        return "{}"


class _FailingPlanRuntime(_FakeSddRuntime):
    async def chat_completion(self, config: Any, system: str, user: str, **kw) -> str:
        if "architecture" in system and "stack" in system:
            return "{not-json"
        return await super().chat_completion(config, system, user, **kw)


# ---------------------------------------------------------------------------
# Fake messaging service
# ---------------------------------------------------------------------------

class _FakeMessagingService(MessagingService):
    def __init__(self) -> None:
        super().__init__()
        self.sent: List[Dict[str, Any]] = []

    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, "markup": kwargs.get("reply_markup")})

    async def send_or_edit(self, *, chat_id: int, text: str, query: Any = None, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, "markup": kwargs.get("reply_markup")})


# ---------------------------------------------------------------------------
# Fake session_mutation_service
# ---------------------------------------------------------------------------

class _FakeSessionMutation:
    def persist_all(self) -> bool:
        return True


class _FakeSessionControl:
    async def cancel_mode(self, *, session_id: str, mode_id: str, timeout_s: float = 0.2) -> int:
        return 0


class _SleepingAnalystPipeline:
    async def run_mode_pipeline(self, session: Any, *_args: Any, **_kwargs: Any) -> None:
        set_active_mode(session, "analyst")
        await asyncio.sleep(60)


class _SleepingManagerPipeline:
    async def run_mode_pipeline(self, session: Any, *_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(60)


class _FailingManagerPipeline:
    async def run_mode_pipeline(self, session: Any, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("manager failed")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_session(tmp_path) -> Any:
    from session import SddState
    s = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        modes=types.SimpleNamespace(active_mode=None),
        sdd=SddState(),
        run_lock=asyncio.Lock(),
        queue=[],
    )
    return s


def _make_mode(
    fake_tasks: _FakeTasksService,
    fake_ms: _FakeMessagingService,
    runtime: Any = None,
    pipeline: Any = None,
) -> SddMode:
    fake_runtime = runtime or _FakeSddRuntime()
    services = {
        "runtime_by_capability": lambda cap: fake_runtime if cap == "sdd_chat_completion" else None,
        "tasks": fake_tasks,
        "messaging_factory": lambda ctx: fake_ms,
        "session_mutation_service": _FakeSessionMutation(),
        "session_control": _FakeSessionControl(),
    }
    if pipeline is not None:
        services["pipeline"] = pipeline
    mode = SddMode()
    mode.initialize(
        config=types.SimpleNamespace(
            defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m")
        ),
        services=services,
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
# Tests: full three-phase flow (specify → plan → tasks)
# ---------------------------------------------------------------------------


def test_handle_input_starts_specify_phase(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        msg = MessageModel(text="Add user authentication feature", chat_id=1)
        result = await mode.handle_input(msg, ctx)

        assert result.success
        sdd = get_sdd_state(session)
        assert sdd.source_intent == "Add user authentication feature"
        # T6: handle_input теперь показывает fork-меню; specify запускается через fork_direct
        assert not any("specify" in n for n in fake_tasks._launched)

        # Выбираем путь "Сразу SDD"
        cb = CallbackModel(action="fork_direct", chat_id=1)
        await mode.handle_callback(cb, ctx)

        assert sdd.feature_slug is not None
        assert any("specify" in n for n in fake_tasks._launched)

        # Allow scheduled coro to complete
        await asyncio.sleep(0.1)

        # After specify phase, pending_gate == "specify", phase == "specify"
        assert sdd.phase == "specify"
        assert sdd.pending_gate == "specify"
        # spec.md should be written
        assert os.path.isfile(os.path.join(str(sdd.spec_dir), "spec.md"))

    asyncio.run(_run())


def test_gate_accept_specify_starts_plan(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, pipeline=_SleepingManagerPipeline())
        ctx = _make_ctx(session, bot_app)

        # Start specify phase (T6: нужен fork_direct после handle_input)
        msg = MessageModel(text="Add user auth", chat_id=1)
        await mode.handle_input(msg, ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.pending_gate == "specify"

        # Accept gate → should trigger plan phase
        cb = CallbackModel(action="gate_accept", chat_id=1)
        result = await mode.handle_callback(cb, ctx)

        assert result.success
        assert sdd.pending_gate is None  # cleared after accept
        assert any("plan" in n for n in fake_tasks._launched)

        # Allow plan coro to complete
        await asyncio.sleep(0.1)

        assert sdd.phase == "plan"
        assert sdd.pending_gate == "plan"
        assert os.path.isfile(os.path.join(str(sdd.spec_dir), "plan.md"))

    asyncio.run(_run())


def test_gate_accept_restores_gate_when_next_phase_fails(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, _FailingPlanRuntime())
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Add user auth", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.phase == "specify"
        assert sdd.pending_gate == "specify"

        result = await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)

        assert result.success
        await asyncio.sleep(0.1)

        assert sdd.phase == "specify"
        assert sdd.pending_gate == "specify"
        assert sdd.last_action == "gate_revise"
        assert any("Ошибка при генерации фазы" in item["text"] for item in fake_ms.sent)

    asyncio.run(_run())


def test_gate_accept_restores_gate_when_next_phase_rejects_spec_dir(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)
        sdd = get_sdd_state(session)
        sdd.phase = "specify"
        sdd.pending_gate = "specify"
        sdd.spec_dir = "../outside"

        result = await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)

        assert result.success
        await asyncio.sleep(0.1)

        assert sdd.phase == "specify"
        assert sdd.pending_gate == "specify"
        assert sdd.last_action == "gate_revise"
        assert any("небезопас" in item["text"] for item in fake_ms.sent)

    asyncio.run(_run())


def test_gate_accept_plan_starts_tasks(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        # Run through specify and plan (T6: нужен fork_direct после handle_input)
        await mode.handle_input(MessageModel(text="Add payments", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.pending_gate == "plan"

        # Accept plan gate → should trigger tasks phase
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        assert sdd.phase == "tasks"
        assert sdd.pending_gate == "tasks"
        assert os.path.isfile(os.path.join(str(sdd.spec_dir), "tasks.md"))

    asyncio.run(_run())


def test_gate_accept_tasks_sets_handoff(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        # Run all three phases (T6: нужен fork_direct после handle_input)
        await mode.handle_input(MessageModel(text="Build dashboard", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)

        sdd = get_sdd_state(session)
        assert sdd.phase == "handoff"
        assert sdd.pending_gate is None

        # Confirm handoff message was sent (проверяем именно handoff-уведомление,
        # а не gate-сообщение фазы tasks, которое тоже содержит "tasks.md").
        sent_texts = [m["text"] for m in fake_ms.sent]
        assert any("Менеджер" in t or "Передаю" in t for t in sent_texts)
        fake_tasks._tasks[-1].cancel()
        with suppress(asyncio.CancelledError):
            await fake_tasks._tasks[-1]

    asyncio.run(_run())


def test_gate_accept_tasks_restores_gate_when_spec_dir_invalid(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        sdd = get_sdd_state(session)
        sdd.phase = "tasks"
        sdd.pending_gate = "tasks"
        sdd.last_action = ""
        sdd.spec_dir = str(tmp_path.parent / "outside-spec")

        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)

        assert sdd.phase == "tasks"
        assert sdd.pending_gate == "tasks"
        assert sdd.last_action == "gate_revise"
        assert "sdd_handoff_manager" not in fake_tasks._launched
        assert any(
            "не задан или небезопасен" in m["text"] and m.get("markup") is not None
            for m in fake_ms.sent
        )

    asyncio.run(_run())


def test_gate_accept_tasks_restores_gate_when_tasks_md_missing(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        spec_dir = tmp_path / "specs" / "001-missing-tasks"
        spec_dir.mkdir(parents=True)
        sdd = get_sdd_state(session)
        sdd.phase = "tasks"
        sdd.pending_gate = "tasks"
        sdd.last_action = ""
        sdd.spec_dir = str(spec_dir)

        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await fake_tasks._tasks[-1]

        assert sdd.phase == "tasks"
        assert sdd.pending_gate == "tasks"
        assert sdd.last_action == "gate_revise"
        assert "sdd_handoff_manager" in fake_tasks._launched
        assert any(
            "Не удалось подготовить план" in m["text"] and m.get("markup") is not None
            for m in fake_ms.sent
        )

    asyncio.run(_run())


def test_gate_accept_tasks_restores_gate_when_tasks_md_invalid(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        spec_dir = tmp_path / "specs" / "001-invalid-tasks"
        spec_dir.mkdir(parents=True)
        (spec_dir / "tasks.md").write_text("not a tasks document\n", encoding="utf-8")
        sdd = get_sdd_state(session)
        sdd.phase = "tasks"
        sdd.pending_gate = "tasks"
        sdd.last_action = ""
        sdd.spec_dir = str(spec_dir)

        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await fake_tasks._tasks[-1]

        assert sdd.phase == "tasks"
        assert sdd.pending_gate == "tasks"
        assert sdd.last_action == "gate_revise"
        assert any(
            "Не удалось подготовить план" in m["text"] and m.get("markup") is not None
            for m in fake_ms.sent
        )

    asyncio.run(_run())


def test_gate_accept_tasks_restores_gate_and_keyboard_when_manager_fails(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, pipeline=_FailingManagerPipeline())
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Build dashboard", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        await asyncio.sleep(0.1)
        await mode.handle_callback(CallbackModel(action="gate_accept", chat_id=1), ctx)
        with suppress(RuntimeError):
            await fake_tasks._tasks[-1]

        sdd = get_sdd_state(session)
        assert sdd.phase == "tasks"
        assert sdd.pending_gate == "tasks"
        assert sdd.last_action == "gate_revise"
        assert any(
            "Не удалось передать задачи" in m["text"] and m.get("markup") is not None
            for m in fake_ms.sent
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# gate_revise
# ---------------------------------------------------------------------------


def test_gate_revise_keeps_gate_sets_last_action(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Feature X", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.pending_gate == "specify"

        await mode.handle_callback(CallbackModel(action="gate_revise", chat_id=1), ctx)

        # pending_gate is kept (points to current phase for revise flow)
        assert sdd.pending_gate == "specify"
        assert sdd.phase == "specify"
        # last_action is persisted so revise survives restart
        assert sdd.last_action == "gate_revise"

    asyncio.run(_run())


def test_gate_revise_then_text_triggers_regeneration(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Feature X", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        await mode.handle_callback(CallbackModel(action="gate_revise", chat_id=1), ctx)
        # Send revision text
        launched_before = len(fake_tasks._launched)
        await mode.handle_input(MessageModel(text="Make it shorter", chat_id=1), ctx)

        # Should have scheduled another phase run
        assert len(fake_tasks._launched) > launched_before

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# gate_stop
# ---------------------------------------------------------------------------


def test_gate_stop_clears_gate_does_not_deactivate(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Feature X", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        await asyncio.sleep(0.1)

        sdd = get_sdd_state(session)
        assert sdd.pending_gate == "specify"

        await mode.handle_callback(CallbackModel(action="gate_stop", chat_id=1), ctx)

        # Gate cleared
        assert sdd.pending_gate is None
        # Mode NOT deactivated (active_mode not changed by stop)
        assert session.modes.active_mode is None  # was never set to sdd in this test

        # Stop message was sent
        sent_texts = [m["text"] for m in fake_ms.sent]
        assert any("остановлен" in t.lower() or "стоп" in t.lower() or "stop" in t.lower() for t in sent_texts)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# revise-after-restart: SddState constructed directly with persisted fields
# ---------------------------------------------------------------------------


def test_revise_after_restart_triggers_regeneration_not_new_feature(tmp_path) -> None:
    """Simulates a restart: SddState pre-populated with pending_gate+last_action.
    Sending text must trigger regeneration of current phase, not start a new feature.
    source_intent must not be overwritten by revision text."""
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        mode = _make_mode(fake_tasks, fake_ms)
        bot_app = _make_bot_app()

        from session import SddState

        # Simulate restored state after restart
        spec_dir = str(tmp_path / "specs" / "001-my-feature")
        os.makedirs(spec_dir, exist_ok=True)
        # Write a stub spec.md so _run_phase doesn't fail on missing file
        with open(os.path.join(spec_dir, "spec.md"), "w") as fh:
            fh.write("# stub spec\n")

        import types
        session = types.SimpleNamespace(
            id="s-restart",
            workdir=str(tmp_path),
            modes=types.SimpleNamespace(active_mode="sdd"),
            sdd=SddState(
                feature_slug="my-feature",
                spec_dir=spec_dir,
                phase="specify",
                pending_gate="specify",
                source_intent="original feature intent",
                last_action="gate_revise",
            ),
        )
        ctx = _make_ctx(session, bot_app)

        launched_before = list(fake_tasks._launched)
        result = await mode.handle_input(
            MessageModel(text="please make it shorter", chat_id=1), ctx
        )

        assert result.success
        # A phase task must have been scheduled (regeneration)
        assert len(fake_tasks._launched) > len(launched_before)
        assert any("specify" in n for n in fake_tasks._launched)
        # source_intent must NOT be overwritten with revision text
        assert session.sdd.source_intent == "original feature intent"
        # last_action must be cleared after handling revision
        assert session.sdd.last_action == ""

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# invalid JSON from model: user gets error message, no silent swallow
# ---------------------------------------------------------------------------


def test_invalid_json_from_model_sends_error_message(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()

        class _BrokenRuntime:
            async def chat_completion(self, config: Any, system: str, user: str, **_kw) -> str:
                return "this is not json at all!!!"

        import types
        mode = SddMode()
        mode.initialize(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m")
            ),
            services={
                "runtime_by_capability": lambda cap: _BrokenRuntime() if cap == "sdd_chat_completion" else None,
                "tasks": fake_tasks,
                "messaging_factory": lambda ctx: fake_ms,
                "session_mutation_service": _FakeSessionMutation(),
            },
        )
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Add feature", chat_id=1), ctx)
        # T6: нужен fork_direct для запуска specify
        await mode.handle_callback(CallbackModel(action="fork_direct", chat_id=1), ctx)
        # Allow the phase coro to run and fail
        await asyncio.sleep(0.1)

        sent_texts = [m["text"] for m in fake_ms.sent]
        # Should have sent an error message (not silently swallowed)
        assert any("❌" in t or "ошибк" in t.lower() or "error" in t.lower() for t in sent_texts)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# gate_accept guard: stale button (pending_gate != phase) must not advance
# ---------------------------------------------------------------------------


def test_gate_accept_stale_button_does_not_advance(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        # Manually set state to simulate a stale button scenario:
        # phase has advanced but old button still sends gate_accept
        sdd = get_sdd_state(session)
        sdd.phase = "plan"
        sdd.pending_gate = None  # gate already cleared (phase advanced)

        launched_before = list(fake_tasks._launched)
        cb = CallbackModel(action="gate_accept", chat_id=1)
        result = await mode.handle_callback(cb, ctx)

        assert result.success
        # No new phase task should have been launched
        assert fake_tasks._launched == launched_before
        # An informational message should be sent
        sent_texts = [m["text"] for m in fake_ms.sent]
        assert any("гейт" in t.lower() or "нет" in t.lower() or "подтверж" in t.lower() for t in sent_texts)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# menu / lifecycle
# ---------------------------------------------------------------------------


def test_sdd_build_menu_reflects_enabled_state(tmp_path) -> None:
    fake_tasks = _FakeTasksService()
    fake_ms = _FakeMessagingService()
    session = _make_session(tmp_path)
    mode = _make_mode(fake_tasks, fake_ms)

    text, keyboard = mode.build_menu(session)
    assert "Режим: выключен" in text
    assert keyboard.inline_keyboard[0][0].text == "🟢 Включить SDD"

    session.modes.active_mode = "sdd"

    text, keyboard = mode.build_menu(session)
    assert "Режим: включен" in text
    assert keyboard.inline_keyboard[0][0].text == "🔴 Выключить SDD"
    assert keyboard.inline_keyboard[1][0].text == "🧭 Инициализировать проект"
    assert "init_project" in keyboard.inline_keyboard[1][0].callback_data


def test_sdd_normalize_spec_dir_rejects_paths_outside_specs(tmp_path: Path) -> None:
    safe = normalize_spec_dir(str(tmp_path), "001-feature")

    assert safe == str((tmp_path / "specs" / "001-feature").resolve())
    assert normalize_spec_dir(str(tmp_path), "../outside") is None
    assert normalize_spec_dir(str(tmp_path), str(tmp_path / "outside")) is None


def test_sdd_run_phase_rejects_restored_spec_dir_outside_workdir(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        session.sdd.source_intent = "Unsafe path feature"
        session.sdd.spec_dir = "../outside"
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)

        await mode._run_phase(
            session=session,
            bot_app=bot_app,
            context=None,
            dest={"kind": "telegram", "chat_id": 1},
            phase="specify",
        )

        assert not (tmp_path / "outside" / "spec.md").exists()
        assert fake_ms.sent
        assert "небезопас" in fake_ms.sent[-1]["text"]

    asyncio.run(_run())


def test_sdd_reset_blocks_running_sdd_task(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_tasks.active_names = ["sdd_phase_specify"]
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        session.modes.active_mode = "sdd"
        session.sdd.feature_slug = "feature"
        session.sdd.phase = "specify"
        session.sdd.pending_gate = "specify"
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        result = await mode.handle_callback(CallbackModel(action="reset", chat_id=1), ctx)

        assert result.success
        assert session.sdd.feature_slug == "feature"
        assert session.sdd.phase == "specify"
        assert session.sdd.pending_gate == "specify"
        assert fake_ms.sent
        assert "выполняет задачу" in fake_ms.sent[-1]["text"]

    asyncio.run(_run())


def test_sdd_on_off_callback_aliases_toggle_active_mode(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        result = await mode.handle_callback(CallbackModel(action="on", chat_id=1), ctx)

        assert result.success
        assert session.modes.active_mode == "sdd"

        result = await mode.handle_callback(CallbackModel(action="off", chat_id=1), ctx)

        assert result.success
        assert session.modes.active_mode is None

    asyncio.run(_run())


def test_sdd_analyst_fork_cancel_does_not_restore_sdd_after_disable(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, pipeline=_SleepingAnalystPipeline())
        set_active_mode(session, "sdd")
        session.sdd.last_action = "fork_analyst_running"

        task = asyncio.create_task(
            mode._run_analyst_then_specify(
                session,
                bot_app,
                None,
                {"kind": "telegram", "chat_id": 1},
                "Analyze then specify",
            )
        )
        await asyncio.sleep(0.05)
        assert get_active_mode(session, "") == "analyst"

        set_active_mode(session, None)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert get_active_mode(session, "") in ("", None)
        assert session.sdd.last_action == ""

    asyncio.run(_run())


def test_sdd_init_project_confirm_requires_confirming_state(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        session.modes.active_mode = "sdd"
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        result = await mode.handle_callback(CallbackModel(action="init_project_confirm", chat_id=1), ctx)

        assert result.success
        assert "sdd_init_project" not in fake_tasks._launched
        assert session.sdd.project_init_status == "idle"
        assert fake_ms.sent
        assert "устарел" in fake_ms.sent[-1]["text"]

    asyncio.run(_run())


def test_sdd_init_project_confirm_blocks_busy_session(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        session.busy = True
        session.modes.active_mode = "sdd"
        session.sdd.project_init_status = "confirming"
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        result = await mode.handle_callback(CallbackModel(action="init_project_confirm", chat_id=1), ctx)

        assert result.success
        assert "sdd_init_project" not in fake_tasks._launched
        assert fake_ms.sent
        assert "Сессия занята" in fake_ms.sent[-1]["text"]

    asyncio.run(_run())


def test_sdd_init_project_blocks_locked_run_lock(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        session.modes.active_mode = "sdd"
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        await session.run_lock.acquire()
        try:
            result = await mode.handle_callback(CallbackModel(action="init_project", chat_id=1), ctx)
        finally:
            session.run_lock.release()

        assert result.success
        assert session.sdd.project_init_status == "idle"
        assert fake_ms.sent
        assert "Сессия занята" in fake_ms.sent[-1]["text"]

    asyncio.run(_run())


def test_sdd_init_project_confirm_blocks_non_empty_queue(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        session.queue.append({"text": "queued"})
        session.modes.active_mode = "sdd"
        session.sdd.project_init_status = "confirming"
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        result = await mode.handle_callback(CallbackModel(action="init_project_confirm", chat_id=1), ctx)

        assert result.success
        assert "sdd_init_project" not in fake_tasks._launched
        assert session.sdd.project_init_status == "confirming"
        assert fake_ms.sent
        assert "Сессия занята" in fake_ms.sent[-1]["text"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# ModeLoader registration
# ---------------------------------------------------------------------------


def test_sdd_mode_registered_in_mode_loader() -> None:
    from modes.registry import ModeLoader
    modes = ModeLoader().load_all()
    ids = [m.get_mode_id() for m in modes]
    assert "sdd" in ids


def test_sdd_get_mode_id() -> None:
    mode = SddMode()
    assert mode.get_mode_id() == "sdd"
