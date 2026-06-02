from __future__ import annotations

import asyncio
import types
from typing import Any, Dict, List, Optional

from modes.sdd.mode import SddMode
from modes.sdd.state import get_sdd_state
from modes.sdk.models import CallbackModel, MessageModel
from modes.sdk.services.messaging import MessagingService
from sessions.session_state_access import (
    get_active_mode,
    is_orchestrator_enabled,
    set_active_mode,
    set_orchestrator_enabled,
    set_orchestrator_last_mode_output,
)


# ---------------------------------------------------------------------------
# Fakes (переиспользуем паттерн из test_sdd_phase_gates.py)
# ---------------------------------------------------------------------------

class _FakeTasksService:
    def __init__(self) -> None:
        self._launched: List[str] = []

    def create(self, *, session_uid: str, mode_id: str, coro: Any, name: str) -> None:
        self._launched.append(name)
        asyncio.ensure_future(coro)

    def list(self, *, session_uid: str, mode_id: str) -> List[str]:
        return []


class _FakeSddRuntime:
    async def chat_completion(self, config: Any, system: str, user: str, **_kw) -> str:
        import json
        if "feature_slug" in system and "acceptance_criteria" in system:
            return json.dumps({
                "feature_slug": "test-feature",
                "stories": [],
                "requirements": [{"id": "REQ-1", "text": "do X"}],
                "acceptance_criteria": [{"req_id": "REQ-1", "ears": "WHEN X THE SYSTEM SHALL Y"}],
            })
        if "architecture" in system and "stack" in system:
            return json.dumps({"architecture": "layered", "stack": ["Python"], "constraints": [], "risks": []})
        if "project_goal" in system and "tasks" in system:
            return json.dumps({
                "project_goal": "goal",
                "tasks": [{
                    "id": "T-1", "title": "t", "description": "d",
                    "acceptance_criteria": [], "covers_requirements": [], "depends_on": [],
                }],
            })
        return "{}"


class _FakeMessagingService(MessagingService):
    def __init__(self) -> None:
        super().__init__()
        self.sent: List[Dict[str, Any]] = []

    async def send_text(self, chat_id: int, text: str, *, md2: bool = True, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, "markup": kwargs.get("reply_markup")})

    async def send_or_edit(self, *, chat_id: int, text: str, query: Any = None, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, "markup": kwargs.get("reply_markup")})


class _FakeSessionMutation:
    def persist_all(self) -> bool:
        return True


class _FakePipelineService:
    """Фейк pipeline: записывает вызовы, опционально выставляет last_mode_output."""

    def __init__(self, analyst_output: Optional[str] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._analyst_output = analyst_output

    async def run_mode_pipeline(
        self,
        session: Any,
        prompt: str,
        dest: Dict[str, Any],
        context: Any,
        *,
        mode_id: str,
    ) -> None:
        self.calls.append({"mode_id": mode_id, "prompt": prompt})
        if mode_id == "analyst" and self._analyst_output is not None:
            set_orchestrator_last_mode_output(session, self._analyst_output)


class _FailingPipelineService:
    """Имитирует production-эффект run_mode_pipeline: выставляет active_mode='analyst'
    (как preserve_mode_id-reset), затем падает — чтобы проверить восстановление режима."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def run_mode_pipeline(
        self,
        session: Any,
        prompt: str,
        dest: Dict[str, Any],
        context: Any,
        *,
        mode_id: str,
    ) -> None:
        self.calls.append({"mode_id": mode_id, "prompt": prompt})
        set_active_mode(session, "analyst")
        raise RuntimeError("analyst pipeline failed")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_session(tmp_path: Any) -> Any:
    from session import SddState
    return types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        modes=types.SimpleNamespace(active_mode=None),
        sdd=SddState(),
    )


def _make_mode(
    fake_tasks: _FakeTasksService,
    fake_ms: _FakeMessagingService,
    fake_pipeline: Optional[_FakePipelineService] = None,
) -> SddMode:
    fake_runtime = _FakeSddRuntime()
    mode = SddMode()
    services: Dict[str, Any] = {
        "runtime_by_capability": lambda cap: fake_runtime if cap == "sdd_chat_completion" else None,
        "tasks": fake_tasks,
        "messaging_factory": lambda ctx: fake_ms,
        "session_mutation_service": _FakeSessionMutation(),
    }
    if fake_pipeline is not None:
        services["pipeline"] = fake_pipeline
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
# Сценарий 1: handle_input показывает fork-меню
# ---------------------------------------------------------------------------

def test_handle_input_shows_fork_menu(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        msg = MessageModel(text="Добавить авторизацию", chat_id=1)
        result = await mode.handle_input(msg, ctx)

        assert result.success

        sdd = get_sdd_state(session)
        # source_intent сохранён
        assert sdd.source_intent == "Добавить авторизацию"
        # фаза сброшена в idle, гейт снят
        assert sdd.phase == "idle"
        assert sdd.pending_gate is None

        # fork-меню отправлено — должна быть кнопочная клавиатура
        assert fake_ms.sent, "Ожидалось сообщение с fork-меню"
        last = fake_ms.sent[-1]
        markup = last.get("markup")
        assert markup is not None, "Ожидалась InlineKeyboardMarkup в fork-меню"

        # Кнопки должны содержать fork_analyst и fork_direct
        buttons_text = []
        for row in markup.inline_keyboard:
            for btn in row:
                buttons_text.append(btn.callback_data or "")
        assert any("fork_analyst" in cb for cb in buttons_text)
        assert any("fork_direct" in cb for cb in buttons_text)

        # Specify НЕ должен запускаться
        assert not any("specify" in n for n in fake_tasks._launched)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Сценарий 2: fork_direct запускает specify
# ---------------------------------------------------------------------------

def test_fork_direct_starts_specify(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        # Сначала вводим интент (показывает меню)
        await mode.handle_input(MessageModel(text="Новая фича", chat_id=1), ctx)
        fake_ms.sent.clear()

        # Выбираем "Сразу SDD"
        cb = CallbackModel(action="fork_direct", chat_id=1)
        result = await mode.handle_callback(cb, ctx)
        assert result.success

        # Задача specify должна быть запущена
        assert any("specify" in n for n in fake_tasks._launched)

        # Ждём выполнения корутины
        await asyncio.sleep(0.2)

        sdd = get_sdd_state(session)
        assert sdd.phase == "specify"
        assert sdd.pending_gate == "specify"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Сценарий 3: fork_analyst запускает аналитика, подхватывает его вывод → specify
# ---------------------------------------------------------------------------

def test_fork_analyst_runs_analyst_then_specify(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        fake_pipeline = _FakePipelineService(analyst_output="ТЗ от аналитика")
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, fake_pipeline)
        ctx = _make_ctx(session, bot_app)

        # Вводим интент
        await mode.handle_input(MessageModel(text="Исходный интент", chat_id=1), ctx)

        # Выбираем "Через Аналитика"
        cb = CallbackModel(action="fork_analyst", chat_id=1)
        result = await mode.handle_callback(cb, ctx)
        assert result.success

        # Задача запущена
        assert any("sdd_fork_analyst" in n for n in fake_tasks._launched)

        # Ждём завершения фоновой корутины
        await asyncio.sleep(0.2)

        sdd = get_sdd_state(session)
        # source_intent заменён выводом аналитика
        assert sdd.source_intent == "ТЗ от аналитика"
        # active_mode восстановлен в sdd
        assert get_active_mode(session) == "sdd"
        # specify запущена (last_action сброшен)
        assert sdd.last_action == ""
        assert any("specify" in n for n in fake_tasks._launched)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Сценарий 4: guard — новый handle_input во время работы аналитика
# ---------------------------------------------------------------------------

def test_handle_input_blocked_while_analyst_running(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms)
        ctx = _make_ctx(session, bot_app)

        # Имитируем состояние "аналитик работает"
        sdd = get_sdd_state(session)
        sdd.last_action = "fork_analyst_running"
        sdd.source_intent = "исходный"

        fake_ms.sent.clear()
        launched_before = list(fake_tasks._launched)

        result = await mode.handle_input(MessageModel(text="новый интент", chat_id=1), ctx)
        assert result.success

        # Никаких новых задач не запущено
        assert fake_tasks._launched == launched_before
        # Сообщение "аналитик работает" отправлено
        assert fake_ms.sent
        assert any("Аналитик работает" in m["text"] for m in fake_ms.sent)
        # source_intent не перезаписан
        assert sdd.source_intent == "исходный"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Сценарий 5: пустой вывод аналитика → source_intent остаётся исходным
# ---------------------------------------------------------------------------

def test_fork_analyst_empty_output_keeps_original_intent(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        # Аналитик возвращает пустую строку
        fake_pipeline = _FakePipelineService(analyst_output="")
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, fake_pipeline)
        ctx = _make_ctx(session, bot_app)

        await mode.handle_input(MessageModel(text="Исходный интент", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_analyst", chat_id=1), ctx)
        await asyncio.sleep(0.2)

        sdd = get_sdd_state(session)
        # Пустой вывод → source_intent не перезаписан
        assert sdd.source_intent == "Исходный интент"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Сценарий 6: is_orchestrator_enabled восстанавливается после fork_analyst
# ---------------------------------------------------------------------------

def test_fork_analyst_restores_orchestrator_enabled(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        fake_pipeline = _FakePipelineService(analyst_output="ТЗ")
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, fake_pipeline)
        ctx = _make_ctx(session, bot_app)

        # Устанавливаем orchestrator.enabled = True
        set_orchestrator_enabled(session, True)
        assert is_orchestrator_enabled(session) is True

        await mode.handle_input(MessageModel(text="фича", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_analyst", chat_id=1), ctx)
        await asyncio.sleep(0.2)

        # После завершения значение должно восстановиться в True
        assert is_orchestrator_enabled(session) is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Сценарий 7: исключение в analyst-пайплайне → режим/флаги восстановлены (BLOCKER-регрессия)
# ---------------------------------------------------------------------------

def test_fork_analyst_exception_restores_mode_and_flags(tmp_path) -> None:
    async def _run() -> None:
        fake_tasks = _FakeTasksService()
        fake_ms = _FakeMessagingService()
        fake_pipeline = _FailingPipelineService()
        session = _make_session(tmp_path)
        bot_app = _make_bot_app()
        mode = _make_mode(fake_tasks, fake_ms, fake_pipeline)
        ctx = _make_ctx(session, bot_app)

        set_orchestrator_enabled(session, True)

        await mode.handle_input(MessageModel(text="фича", chat_id=1), ctx)
        await mode.handle_callback(CallbackModel(action="fork_analyst", chat_id=1), ctx)
        await asyncio.sleep(0.2)

        sdd = get_sdd_state(session)
        # active_mode восстановлен в sdd, несмотря на падение analyst (а не залип в "analyst")
        assert get_active_mode(session) == "sdd"
        # сторожевой флаг снят — пользователь не залипнет в "Аналитик работает"
        assert sdd.last_action == ""
        # orchestrator.enabled восстановлен
        assert is_orchestrator_enabled(session) is True
        # specify НЕ запущена (аналитик упал)
        assert not any("specify" in n for n in fake_tasks._launched)
        # пользователь уведомлён об ошибке
        assert any("Ошибк" in m["text"] for m in fake_ms.sent)

    asyncio.run(_run())
