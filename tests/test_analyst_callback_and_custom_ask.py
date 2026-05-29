import asyncio
import json
import tempfile
import types
from pathlib import Path

from app.services.telegram_ui_scope import TelegramUiKey
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig
from modes.analyst.ui import build_analyst_menu
from modes.analyst.mode import AnalystMode
from modes.analyst.runner_service import AnalystModeRunnerService
from modes.analyst.state_store import AnalystContext, AnalystStateStore, build_context_key
from modes.registry import ModeRegistry
from modes.sdk.orchestrator_runner import OrchestratorRunner
from modes.sdk.runtime.contracts import ExecutorResponse, PlanStep
from modes.sdk.runtime.heuristics import needs_clarification
from modes.sdk import MessagingService, ModeCallbackRouterService, ModePipelineService, ModeRegistryService, SessionControlService
from tg.callbacks import CallbackHandler
from utils import cli_proxy_artifact_path


class _FakeMessage:
    def __init__(self, chat_id: int = 100, message_id: int = 200) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


class _FakeManager:
    def __init__(self, session) -> None:
        self._session = session
        self.persist_calls = 0

    def active(self, _chat_id: int):
        return self._session

    def _persist_sessions(self) -> None:
        self.persist_calls += 1

    def get(self, _chat_id: int, session_id: str):
        if str(getattr(self._session, "id", "") or "") == str(session_id or ""):
            return self._session
        return None


class _FakeAgent:
    def __init__(self) -> None:
        self.answers = []

    def resolve_question(self, question_id: str, answer: str) -> bool:
        self.answers.append((question_id, answer))
        return True


class _FakeModeTasks:
    async def cancel_all(self, **_kwargs):
        return 0

    async def cancel_session(self, **_kwargs):
        return 0

    def list(self, **_kwargs):
        return []


class _ProbeLock:
    def __init__(self, locked: bool = False) -> None:
        self._locked = bool(locked)

    def set_locked(self, value: bool) -> None:
        self._locked = bool(value)

    def locked(self) -> bool:
        return bool(self._locked)


class _FakeBotApp:
    def __init__(self, session) -> None:
        if not hasattr(session, "modes"):
            session.modes = types.SimpleNamespace(
                active_mode=getattr(session, "active_mode", None),
                analyst_mode=str(getattr(session, "analyst_mode", "spec") or "spec"),
            )
        self.manager = _FakeManager(session)
        self.mode_tasks = _FakeModeTasks()
        self.ui_state = types.SimpleNamespace(
            pending_questions={},
            active_ask_question_by_chat={},
            context_by_chat={},
        )
        self._agent_runtime = _FakeAgent()
        self.get_runtime_by_capability = (
            lambda cap: self._agent_runtime if str(cap) == "resolve_question" else None
        )
        self.edits = []
        self.documents = []
        self.mode_registry = ModeRegistry()
        self.mode_registry_service = ModeRegistryService(self.mode_registry)
        mode = AnalystMode()
        self.mode_registry.register(mode)
        self.config = types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="test-key",
                openai_model="gpt-test",
                workdir=getattr(session, "workdir", "/tmp"),
            )
        )
        mode.initialize(
            config=self.config,
            services={
                "tasks": self.mode_tasks,
                "dialogs": types.SimpleNamespace(
                    is_active=(lambda **_k: False),
                    start=(lambda **_k: None),
                    end=(lambda **_k: None),
                ),
                "session_control": SessionControlService(
                    persist_sessions=self.manager._persist_sessions,
                    cancel_mode_tasks=(lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)),
                    cancel_session_tasks=(lambda _sid, _timeout: asyncio.sleep(0, result=0)),
                ),
                "pipeline": ModePipelineService(
                    run_mode_pipeline_fn=(lambda *_a, **_k: asyncio.sleep(0)),
                ),
                "messaging_factory": (lambda ctx: MessagingService(
                    send_message=self._send_message,
                    edit_message=self._edit_message,
                    send_document=self._send_document,
                    transport_context=ctx,
                )),
            },
        )
        self.mode_callback_router = ModeCallbackRouterService(
            mode_registry=self.mode_registry_service,
            dialogs=getattr(mode, "_dialogs", None),
            send_message=self._send_message,
            get_session=lambda chat_id: self.manager.active(chat_id),
            get_dirs_mode_token=lambda _chat_id, _message_thread_id=None: "",
            clear_dirs_mode_token=lambda _chat_id, _message_thread_id=None: None,
        )
        self.access_policy_service = types.SimpleNamespace(
            ensure_allowed=(lambda _chat_id, _context: asyncio.sleep(0, result=True)),
            is_admin=(lambda _chat_id, scope="generic": True),
            callback_admin_scope=(lambda _chat_id, _data, **_kwargs: ""),
            admin_denied_text=(lambda scope="generic": "denied"),
            is_mode_allowed_for_chat=(lambda _chat_id, _mode_id: True),
        )

    @staticmethod
    def telegram_ui_key(chat_id: int, message_thread_id=None) -> TelegramUiKey:
        return TelegramUiKey.from_parts(chat_id, message_thread_id)

    @staticmethod
    def telegram_ui_key_from_query(query):
        return TelegramUiKey.from_query(query)

    def is_allowed(self, _chat_id: int) -> bool:
        return True

    def is_admin(self, _chat_id: int) -> bool:
        return True

    def _short_label(self, text: str, max_len: int = 40) -> str:
        return text if len(text) <= max_len else (text[: max_len - 3] + "...")

    async def _edit_message(self, _context, *, chat_id: int, message_id: int, text: str, reply_markup=None, md2: bool = True):
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "md2": md2,
            }
        )
        return True

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.edits.append({"chat_id": chat_id, "text": text})

    async def _send_document(self, _context, *, chat_id: int, document):
        self.documents.append({"chat_id": chat_id, "name": getattr(document, "name", "")})
        return True

    def _clear_pending_question(self, question_id: str) -> bool:
        qid = str(question_id or "").strip()
        meta = self.ui_state.pending_questions.pop(qid, None)
        active = self.ui_state.active_ask_question_by_chat
        if isinstance(active, dict) and isinstance(meta, dict):
            ui_key = TelegramUiKey.from_parts(meta.get("chat_id") or 0, meta.get("message_thread_id"))
            if int(ui_key.chat_id) > 0 and str(active.get(ui_key) or "") == qid:
                active.pop(ui_key, None)
        return bool(meta is not None)


def test_analyst_enable_sets_active_mode() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        Path(workdir).mkdir(parents=True, exist_ok=True)
        session = types.SimpleNamespace(
            id="s1",
            workdir=workdir,
            active_mode=None,
        )
        bot_app = _FakeBotApp(session)
        handler = CallbackHandler(bot_app)
        query = _FakeQuery("ma:analyst:enable")
        update = types.SimpleNamespace(callback_query=query)

        asyncio.run(handler.handle_callback(update, context=object()))

    assert session.modes.active_mode == "analyst"
    assert bot_app.manager.persist_calls >= 1
    assert bot_app.edits
    assert "Режим: включен" in bot_app.edits[-1]["text"]


def test_ask_custom_marks_pending_for_free_text() -> None:
    session = types.SimpleNamespace(id="s1")
    bot_app = _FakeBotApp(session)
    handler = CallbackHandler(bot_app)
    bot_app.ui_state.pending_questions["q1"] = {
        "chat_id": 100,
        "session_id": "s1",
        "options": ["A", "B"],
        "awaiting_custom": False,
    }
    query = _FakeQuery("ask:q1:custom")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert bot_app.ui_state.pending_questions["q1"]["awaiting_custom"] is True
    assert bot_app.ui_state.active_ask_question_by_chat.get(TelegramUiKey.from_parts(100)) == "q1"
    assert bot_app.edits
    assert "Введите свой вариант" in bot_app.edits[-1]["text"]


def test_ask_callback_rejects_question_with_missing_session() -> None:
    session = types.SimpleNamespace(id="s1")
    bot_app = _FakeBotApp(session)
    handler = CallbackHandler(bot_app)
    bot_app.ui_state.pending_questions["q1"] = {
        "chat_id": 100,
        "session_id": "missing-session",
        "options": ["A", "B"],
        "awaiting_custom": False,
    }
    bot_app.ui_state.active_ask_question_by_chat = {TelegramUiKey.from_parts(100): "q1"}
    query = _FakeQuery("ask:q1:0")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert "q1" not in bot_app.ui_state.pending_questions
    assert bot_app.ui_state.active_ask_question_by_chat.get(TelegramUiKey.from_parts(100)) is None
    assert bot_app.edits
    assert bot_app.edits[-1]["text"] == "Вопрос устарел."


def test_needs_clarification_ignores_generic_keywords_for_repo_grounded_analyst_context() -> None:
    cfg = types.SimpleNamespace(
        defaults=types.SimpleNamespace(
            clarification_enabled=True,
            clarification_keywords=["уточни", "уточните", "неясно"],
        )
    )
    context = (
        "executor_profile=analyst\n"
        "analyst_intent_flags:\n"
        + json.dumps(
            {
                "clarification_is_blocking": False,
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_final_repo_review": False,
                "requires_repo_audit": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    assert needs_clarification("Уточни, пожалуйста, детали ТЗ", cfg, context) is False


def test_needs_clarification_keeps_generic_keywords_for_non_repo_grounded_context() -> None:
    cfg = types.SimpleNamespace(
        defaults=types.SimpleNamespace(
            clarification_enabled=True,
            clarification_keywords=["уточни", "уточните", "неясно"],
        )
    )
    context = (
        "executor_profile=analyst\n"
        "analyst_intent_flags:\n"
        + json.dumps(
            {
                "clarification_is_blocking": False,
                "document_kind": "analysis",
                "requires_codebase_grounding": False,
                "requires_final_repo_review": False,
                "requires_repo_audit": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    assert needs_clarification("Уточни, пожалуйста, детали ТЗ", cfg, context) is True


def test_analyst_runner_service_marks_blocking_clarification_runtime_contract() -> None:
    async def _run() -> None:
        captured = {}

        async def _fake_runtime_run(session, prompt, bot_app, context, dest):
            captured["prompt"] = prompt
            captured["blocking"] = getattr(session, "analyst_blocking_clarification_runtime", None)
            captured["dest"] = dict(dest or {})
            return "ok"

        runner = AnalystModeRunnerService.__new__(AnalystModeRunnerService)
        runner._config = types.SimpleNamespace()
        runner._runtime = types.SimpleNamespace(run=_fake_runtime_run)

        session = types.SimpleNamespace(
            id="s1",
            analyst_intent_flags={"clarification_is_blocking": True},
        )
        out = await AnalystModeRunnerService.run(
            runner,
            session,
            "prompt",
            bot_app=None,
            context=None,
            dest={"kind": "telegram", "chat_id": 100},
        )

        assert out == "ok"
        assert captured["prompt"] == "prompt"
        assert captured["blocking"] is True
        assert captured["dest"] == {"kind": "telegram", "chat_id": 100}

    asyncio.run(_run())


def test_blocking_clarification_keeps_automatic_replan_after_answer(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
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
        orch = OrchestratorRunner(cfg)
        calls = {"n": 0}
        executed = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            return [
                PlanStep(
                    id="ask1",
                    title="Уточнить обязательный параметр",
                    instruction="ask",
                    step_type="ask_user",
                    ask_question="Какой вариант нужен?",
                    ask_options=["A", "B"],
                ),
                PlanStep(id="final", title="Продолжить работу", instruction="do final"),
                PlanStep(
                    id="use_cli_repo_grounding",
                    title="Repo grounding",
                    instruction=f"ground in {tmp_path}",
                    step_type="use_cli",
                ),
            ]

        async def _fake_execute_step(
            step,
            session,
            bot,
            context,
            dest,
            orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            executed.append(step.id)
            if step.id == "ask1":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Ответ пользователя получен",
                    outputs=[{"type": "text", "content": "User selected: B"}],
                    tool_calls=[{"tool": "ask_user"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done final",
                outputs=[{"type": "text", "content": "final"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = types.SimpleNamespace(
            id="s1",
            workdir=str(tmp_path),
            analyst_intent_flags={
                "clarification_is_blocking": True,
                "document_kind": "spec",
                "requires_codebase_grounding": True,
                "requires_final_repo_review": False,
                "requires_repo_audit": False,
            },
        )
        out = await orch.run(
            session,
            "Нужно уточнение перед продолжением",
            bot=None,
            context=None,
            dest={"kind": "telegram", "chat_id": 1, "chat_type": "private"},
        )

        assert "Автоматическое продолжение остановлено" not in out
        assert "done final" in out
        assert calls["n"] == 2
        assert "ask1" in executed
        assert "final" in executed
        assert "use_cli_repo_grounding" in executed

    asyncio.run(_run())


def test_non_blocking_clarification_keeps_automatic_replan(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[]),
            tools={},
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
        orch = OrchestratorRunner(cfg)
        calls = {"n": 0}
        executed = []

        async def _fake_plan_steps(_cfg, _user_text, _ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    PlanStep(
                        id="ask1",
                        title="Уточнить обязательный параметр",
                        instruction="ask",
                        step_type="ask_user",
                        ask_question="Какой вариант нужен?",
                        ask_options=["A", "B"],
                    )
                ]
            return [PlanStep(id="final", title="Продолжить работу", instruction="do final")]

        async def _fake_execute_step(
            step,
            session,
            bot,
            context,
            dest,
            orchestrator_context,
            *,
            current_user_text="",
            constraints=None,
        ):
            executed.append(step.id)
            if step.id == "ask1":
                return ExecutorResponse(
                    task_id=step.id,
                    status="ok",
                    summary="Ответ пользователя получен",
                    outputs=[{"type": "text", "content": "User selected: A"}],
                    tool_calls=[{"tool": "ask_user"}],
                    next_questions=[],
                )
            return ExecutorResponse(
                task_id=step.id,
                status="ok",
                summary="done final",
                outputs=[{"type": "text", "content": "final"}],
                tool_calls=[{"tool": "fake"}],
                next_questions=[],
            )

        monkeypatch.setattr(orch._deps, "plan_steps", _fake_plan_steps)
        monkeypatch.setattr(orch, "_execute_step", _fake_execute_step)
        monkeypatch.setattr(orch, "_maybe_update_memory", lambda *args, **kwargs: asyncio.sleep(0))

        session = types.SimpleNamespace(
            id="s1",
            analyst_intent_flags={
                "clarification_is_blocking": False,
                "document_kind": "analysis",
                "requires_codebase_grounding": False,
                "requires_final_repo_review": False,
                "requires_repo_audit": False,
            },
        )
        out = await orch.run(
            session,
            "Нужно уточнение перед продолжением",
            bot=None,
            context=None,
            dest={"kind": "telegram", "chat_id": 1, "chat_type": "private"},
        )

        assert "Автоматическое продолжение остановлено" not in out
        assert "done final" in out
        assert calls["n"] == 2
        assert "ask1" in executed
        assert "final" in executed

    asyncio.run(_run())


def test_build_analyst_menu_shows_status_button_when_enabled() -> None:
    session = types.SimpleNamespace(active_mode="analyst")
    _text, keyboard = build_analyst_menu(session, back_callback="x", back_text="Назад")
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert "ma:analyst:status" in callbacks
    assert "ma:analyst:download" in callbacks
    assert "ma:analyst:audit" in callbacks
    assert "ma:analyst:template:val=default" in callbacks
    assert "ma:analyst:template:val=audit" in callbacks


def test_build_analyst_menu_uses_audit_specific_download_label() -> None:
    session = types.SimpleNamespace(
        active_mode="analyst",
        analyst_template_id="audit",
        modes=types.SimpleNamespace(active_mode="analyst", analyst_mode="audit", analyst_template_id="audit"),
    )
    analyst_context = types.SimpleNamespace(
        mode="audit",
        active_flow="audit",
        runtime_template_id="audit",
        effective_template_id="audit",
        document_kind="audit",
    )

    _text, keyboard = build_analyst_menu(
        session,
        back_callback="x",
        back_text="Назад",
        analyst_context=analyst_context,
    )
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]

    assert "📥 Скачать черновик аудита" in labels


def test_analyst_template_callback_updates_session_template_id(tmp_path, monkeypatch) -> None:
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        active_mode="analyst",
        analyst_template_id="default",
        analyst_mode="spec",
    )
    bot_app = _FakeBotApp(session)
    yaml_path = tmp_path / "analyst_config.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                "templates:",
                "  default:",
                "    name: Default",
                "    description: Default template",
                "    required_sections: [\"S1\"]",
                "    system_prompt_addition: \"\"",
                "    qa_prompt: QA",
                "  audit:",
                "    name: Audit",
                "    description: Audit template",
                "    required_sections: [\"A1\"]",
                "    system_prompt_addition: \"\"",
                "    qa_prompt: QA",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(yaml_path))
    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:template:audit")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert session.analyst_template_id == "audit"
    assert session.modes.analyst_mode == "spec"
    assert bot_app.manager.persist_calls == 1


def test_analyst_template_callback_invalid_template_returns_safe_message(tmp_path, monkeypatch) -> None:
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        active_mode="analyst",
        analyst_template_id="default",
        analyst_mode="spec",
    )
    bot_app = _FakeBotApp(session)
    yaml_path = tmp_path / "analyst_config.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                "templates:",
                "  default:",
                "    name: Default",
                "    description: Default template",
                "    required_sections: [\"S1\"]",
                "    system_prompt_addition: \"\"",
                "    qa_prompt: QA",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_TEMPLATES_PATH", str(yaml_path))
    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:template:broken")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert session.analyst_template_id == "default"
    assert bot_app.edits
    assert bot_app.edits[-1]["text"] == "Шаблон недоступен."


def test_analyst_status_shows_waiting_for_user_stage(tmp_path) -> None:
    state_store = AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))
    session = types.SimpleNamespace(
        id="s1",
        chat_id=100,
        name=None,
        tool=types.SimpleNamespace(name="dummy"),
        workdir=str(tmp_path),
        active_mode="analyst",
        busy=False,
        started_at=None,
        last_output_ts=None,
        last_tick_ts=None,
        tick_seen=0,
        queue=[{"text": "queued analyst task", "dest": {"kind": "telegram", "chat_id": 100}}],
        analyst_template_id="default",
    )
    analyst_ctx = state_store.load(build_context_key(session.chat_id, session.id))
    analyst_ctx.active_flow = "audit"
    analyst_ctx.runtime_template_id = "audit"
    analyst_ctx.effective_template_id = "audit"
    state_store.save(analyst_ctx)

    bot_app = _FakeBotApp(session)
    handler = CallbackHandler(bot_app)
    bot_app.ui_state.pending_questions["q1"] = {
        "chat_id": 100,
        "session_id": "s1",
        "awaiting_custom": False,
    }
    query = _FakeQuery("ma:analyst:status")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert bot_app.edits
    text = bot_app.edits[-1]["text"]
    assert "🧠 Статус Аналитика" in text
    assert "Стадия: ожидает ваш ответ" in text
    assert "Ожидание ответа: 1" in text
    assert "Pending questions: 1; active=q1; custom=нет" in text
    assert "Active plugin flow: audit" in text
    assert "Template/override: selected=default | runtime=audit | effective=audit" in text
    assert "Queue origin: telegram | chat=100 | text=queued analyst task" in text


def test_analyst_download_sends_draft_document(tmp_path) -> None:
    session = types.SimpleNamespace(
        id="s1",
        name=None,
        tool=types.SimpleNamespace(name="dummy"),
        workdir=str(tmp_path),
        active_mode="analyst",
        state_summary="Черновой текст ТЗ",
    )
    bot_app = _FakeBotApp(session)
    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:download")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert bot_app.documents
    assert bot_app.edits
    assert "Черновик ТЗ отправлен файлом." in bot_app.edits[-1]["text"]


def test_analyst_download_uses_analysis_specific_notification_and_document_title(tmp_path) -> None:
    session = types.SimpleNamespace(
        id="s1",
        name=None,
        tool=types.SimpleNamespace(name="dummy"),
        workdir=str(tmp_path),
        active_mode="analyst",
        analyst_mode="analysis",
    )
    bot_app = _FakeBotApp(session)
    mode = bot_app.mode_registry.get("analyst")
    assert mode is not None
    mode._store(session).save(
        AnalystContext(
            key=mode._context_key(session),
            mode="analysis",
            document_kind="analysis",
            last_draft="Итоговый аналитический черновик",
        )
    )
    query = _FakeQuery("ma:analyst:download")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(CallbackHandler(bot_app).handle_callback(update, context=object()))

    assert bot_app.documents
    assert bot_app.edits
    assert "Черновик аналитической записки отправлен файлом." in bot_app.edits[-1]["text"]


def test_analyst_audit_dirs_flow_failure_rolls_back_state(tmp_path) -> None:
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        active_mode="analyst",
        analyst_mode="spec",
        analyst_active_flow="",
        analyst_runtime_template_id="",
    )
    bot_app = _FakeBotApp(session)
    mode = bot_app.mode_registry.get("analyst")
    assert mode is not None

    async def _fail_start_flow(*_args, **_kwargs):
        raise RuntimeError("dirs unavailable")

    if mode.mode_dependencies is not None:
        mode.mode_dependencies = mode.mode_dependencies.with_overrides(
            dirs_flow=types.SimpleNamespace(start_flow=_fail_start_flow),
        )
    else:
        mode._extra_services["dirs_flow"] = types.SimpleNamespace(start_flow=_fail_start_flow)

    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:audit")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert session.modes.analyst_mode == "spec"
    assert session.analyst_active_flow == ""
    assert session.analyst_runtime_template_id == ""
    assert bot_app.edits
    assert "Не удалось запустить выбор пути для аудита" in bot_app.edits[-1]["text"]


def test_analyst_audit_callback_is_blocked_while_session_busy(tmp_path) -> None:
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        active_mode="analyst",
        busy=True,
        analyst_mode="spec",
        analyst_active_flow="",
        analyst_runtime_template_id="",
    )
    bot_app = _FakeBotApp(session)
    mode = bot_app.mode_registry.get("analyst")
    assert mode is not None

    starts = {"count": 0}

    async def _start_flow(*_args, **_kwargs):
        starts["count"] += 1

    if mode.mode_dependencies is not None:
        mode.mode_dependencies = mode.mode_dependencies.with_overrides(
            dirs_flow=types.SimpleNamespace(start_flow=_start_flow),
        )
    else:
        mode._extra_services["dirs_flow"] = types.SimpleNamespace(start_flow=_start_flow)

    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:audit")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert starts["count"] == 0
    assert bot_app.edits
    assert "Сессия занята" in str(bot_app.edits[-1]["text"] or "")

    session.busy = False
    query2 = _FakeQuery("ma:analyst:audit")
    update2 = types.SimpleNamespace(callback_query=query2)
    asyncio.run(handler.handle_callback(update2, context=object()))
    assert starts["count"] == 1


def test_analyst_audit_callback_is_blocked_while_run_lock_locked_and_recovers(tmp_path) -> None:
    run_lock = _ProbeLock(locked=True)
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        active_mode="analyst",
        busy=False,
        run_lock=run_lock,
        is_active_by_tick=lambda: False,
        analyst_mode="spec",
        analyst_active_flow="",
        analyst_runtime_template_id="",
    )
    bot_app = _FakeBotApp(session)
    mode = bot_app.mode_registry.get("analyst")
    assert mode is not None

    starts = {"count": 0}

    async def _start_flow(*_args, **_kwargs):
        starts["count"] += 1

    if mode.mode_dependencies is not None:
        mode.mode_dependencies = mode.mode_dependencies.with_overrides(
            dirs_flow=types.SimpleNamespace(start_flow=_start_flow),
        )
    else:
        mode._extra_services["dirs_flow"] = types.SimpleNamespace(start_flow=_start_flow)

    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:audit")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))
    assert starts["count"] == 0
    assert bot_app.edits
    assert "Сессия занята" in str(bot_app.edits[-1]["text"] or "")

    run_lock.set_locked(False)
    query2 = _FakeQuery("ma:analyst:audit")
    update2 = types.SimpleNamespace(callback_query=query2)
    asyncio.run(handler.handle_callback(update2, context=object()))
    assert starts["count"] == 1


def test_analyst_audit_callback_is_blocked_while_tick_active_and_recovers(tmp_path) -> None:
    tick_state = {"active": True}
    session = types.SimpleNamespace(
        id="s1",
        workdir=str(tmp_path),
        active_mode="analyst",
        busy=False,
        run_lock=_ProbeLock(locked=False),
        is_active_by_tick=lambda: bool(tick_state["active"]),
        analyst_mode="spec",
        analyst_active_flow="",
        analyst_runtime_template_id="",
    )
    bot_app = _FakeBotApp(session)
    mode = bot_app.mode_registry.get("analyst")
    assert mode is not None

    starts = {"count": 0}

    async def _start_flow(*_args, **_kwargs):
        starts["count"] += 1

    if mode.mode_dependencies is not None:
        mode.mode_dependencies = mode.mode_dependencies.with_overrides(
            dirs_flow=types.SimpleNamespace(start_flow=_start_flow),
        )
    else:
        mode._extra_services["dirs_flow"] = types.SimpleNamespace(start_flow=_start_flow)

    handler = CallbackHandler(bot_app)
    query = _FakeQuery("ma:analyst:audit")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))
    assert starts["count"] == 0
    assert bot_app.edits
    assert "Сессия занята" in str(bot_app.edits[-1]["text"] or "")

    tick_state["active"] = False
    query2 = _FakeQuery("ma:analyst:audit")
    update2 = types.SimpleNamespace(callback_query=query2)
    asyncio.run(handler.handle_callback(update2, context=object()))
    assert starts["count"] == 1
