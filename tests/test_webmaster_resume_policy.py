import asyncio
import types
from typing import Any

from modes.webmaster.mode import WebmasterMode
from modes.webmaster import mode as webmaster_mode_module
from modes.webmaster.models import FeedbackDecision, WebmasterContext
from modes.webmaster.state_store import build_user_key
from modes.sdk.services.tooling import ModeToolingService


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, name, args, tool_ctx):
        self.calls.append((name, dict(args), dict(tool_ctx)))
        return {"success": True, "output": "ok"}


def _setup_tooling(mode: WebmasterMode, registry: Any) -> None:
    mode.initialize(
        config=types.SimpleNamespace(defaults=types.SimpleNamespace()),
        services={
            "tooling": ModeToolingService(
                execute_tool_fn=(lambda name, args, tool_ctx: registry.execute(name, args, tool_ctx)),
                registry_provider=(lambda: registry),
            ),
        },
    )


def test_webmaster_run_use_cli_passes_fresh_flag(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        registry = _FakeRegistry()
        _setup_tooling(mode, registry)
        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42),
            ),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        dest = {"chat_id": 1, "chat_type": "private"}

        await mode._run_use_cli(bot_app, session, None, dest, "task-new", fresh_run=True)
        await mode._run_use_cli(bot_app, session, None, dest, "task-continue", fresh_run=False)

        assert registry.calls[0][1]["fresh_run"] is True
        assert registry.calls[1][1]["fresh_run"] is False
        assert registry.calls[0][2]["tool_timeouts_ms"]["use_cli"] == 42_000

    asyncio.run(_run())


def test_webmaster_cli_task_contains_run_policy_by_task_kind(tmp_path) -> None:
    mode = WebmasterMode()
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))

    ctx_new = WebmasterContext(key="k1", task_kind="new_task", goal="g")
    text_new = mode._build_cli_task(ctx_new, session=session)
    assert "Требование: запуск fresh (без resume)." in text_new

    ctx_continue = WebmasterContext(key="k2", task_kind="continue_task", goal="g")
    text_continue = mode._build_cli_task(ctx_continue, session=session)
    assert "Требование: продолжение диалога через resume текущей CLI-сессии (не fresh)." in text_continue


def test_webmaster_run_pipeline_analyze_intent_failure_returns_fallback(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]

        async def _fail_analyze(**_kwargs):
            raise RuntimeError("intent failed")

        mode._analyze_intent = _fail_analyze  # type: ignore[method-assign]
        mode._confirm_intent = lambda *_a, **_k: asyncio.sleep(0, result="Подтвердить")  # type: ignore[method-assign]

        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42)),
            _tool_registry=types.SimpleNamespace(),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        dest = {"chat_id": 1, "user_id": 7, "chat_type": "private"}

        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=bot_app,
            context=None,
            dest=dest,
        )
        assert out == "Не удалось разобрать намерение. Уточните задачу одним сообщением и попробуем снова."
        key = build_user_key(1, 7, "s1")
        saved = mode._store(session).load(key)
        assert saved.stage == "await_intent_update"

    asyncio.run(_run())


def test_webmaster_run_pipeline_invalid_intent_plugin_output_uses_graceful_fallback(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]

        class _Registry:
            async def execute(self, name, _args, _ctx):
                if name == "intent_plugin":
                    return {
                        "success": True,
                        "output": "{\"goal\":123,\"actions\":\"bad-type\",\"constraints\":[],\"acceptance_criteria\":[]}",
                    }
                return {"success": True, "output": "ok"}

        registry = _Registry()
        _setup_tooling(mode, registry)

        confirmation_called = {"value": False}

        async def _confirm(_bot_app, _session, _context, _dest, wm_ctx):
            confirmation_called["value"] = True
            assert wm_ctx.goal == "сделай задачу"
            assert wm_ctx.actions == ["сделай задачу"]
            return "Новая задача"

        mode._confirm_intent = _confirm  # type: ignore[method-assign]

        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42)),
            _tool_registry=types.SimpleNamespace(),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        dest = {"chat_id": 1, "user_id": 7, "chat_type": "private"}

        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=bot_app,
            context=None,
            dest=dest,
        )

        assert confirmation_called["value"] is True
        assert out == "Контекст очищен. Пришлите новую задачу."

    asyncio.run(_run())


def test_webmaster_invalid_intent_fallback_isolated_across_two_runs(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]

        class _Registry:
            async def execute(self, name, _args, _ctx):
                if name == "intent_plugin":
                    return {
                        "success": True,
                        "output": "{\"goal\":123,\"actions\":\"bad-type\",\"constraints\":[],\"acceptance_criteria\":[]}",
                    }
                return {"success": True, "output": "ok"}

        registry = _Registry()
        _setup_tooling(mode, registry)

        observed_goals = []

        async def _confirm(_bot_app, _session, _context, _dest, wm_ctx):
            observed_goals.append(str(wm_ctx.goal))
            return "Новая задача"

        mode._confirm_intent = _confirm  # type: ignore[method-assign]

        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42)),
            _tool_registry=types.SimpleNamespace(),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        dest = {"chat_id": 1, "user_id": 7, "chat_type": "private"}

        out1 = await mode.run_pipeline(
            session=session,
            user_text="первая задача",
            bot_app=bot_app,
            context=None,
            dest=dest,
        )
        out2 = await mode.run_pipeline(
            session=session,
            user_text="вторая задача",
            bot_app=bot_app,
            context=None,
            dest=dest,
        )

        assert out1 == "Контекст очищен. Пришлите новую задачу."
        assert out2 == "Контекст очищен. Пришлите новую задачу."
        assert observed_goals == ["первая задача", "вторая задача"]

    asyncio.run(_run())


def test_webmaster_run_pipeline_without_user_id_uses_chat_scoped_key(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]

        async def _fail_analyze(**_kwargs):
            raise RuntimeError("intent failed")

        mode._analyze_intent = _fail_analyze  # type: ignore[method-assign]
        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42)),
            _tool_registry=types.SimpleNamespace(),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))

        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=bot_app,
            context=None,
            dest={"chat_id": 55, "chat_type": "group"},
        )
        assert out == "Не удалось разобрать намерение. Уточните задачу одним сообщением и попробуем снова."
        saved = mode._store(session).load(build_user_key(55, 55, "s1"))
        assert saved.stage == "await_intent_update"

    asyncio.run(_run())


def test_webmaster_run_pipeline_confirm_failure_returns_fallback(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = lambda **_k: asyncio.sleep(0, result={"goal": "g", "actions": ["a"]})  # type: ignore[method-assign]

        async def _fail_confirm(*_args, **_kwargs):
            raise RuntimeError("ask_user failed")

        mode._confirm_intent = _fail_confirm  # type: ignore[method-assign]

        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42)),
            _tool_registry=types.SimpleNamespace(),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        dest = {"chat_id": 1, "user_id": 9, "chat_type": "private"}

        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=bot_app,
            context=None,
            dest=dest,
        )
        assert out == "Не удалось запросить подтверждение. Уточните задачу и повторим."
        key = build_user_key(1, 9, "s1")
        saved = mode._store(session).load(key)
        assert saved.stage == "await_intent_update"

    asyncio.run(_run())


def test_webmaster_confirm_intent_uses_strict_buttons(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]
        mode._prompts = {"confirmation": "Проверь намерения"}
        captured = {}

        class _Registry:
            async def execute(self, name, args, _ctx):
                captured["name"] = name
                captured["args"] = dict(args)
                return {"success": True, "output": "User selected: Подтвердить"}

        registry = _Registry()
        _setup_tooling(mode, registry)
        bot_app = types.SimpleNamespace()
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        wm_ctx = WebmasterContext(key="k", goal="g", actions=["a"], acceptance_criteria=["ok"])

        selected = await mode._confirm_intent(
            bot_app=bot_app,
            session=session,
            context=object(),
            dest={"kind": "telegram", "chat_id": 1, "user_id": 2},
            wm_ctx=wm_ctx,
        )

        assert selected == "Подтвердить"
        assert captured["name"] == "ask_user"
        assert captured["args"]["options"] == ["Подтвердить", "Уточнить", "Новая задача"]
        assert captured["args"]["allow_custom"] is False
        assert captured["args"]["system_options"] is False

    asyncio.run(_run())


def test_webmaster_confirm_intent_rejects_non_button_answer(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()

        class _Registry:
            async def execute(self, _name, _args, _ctx):
                return {"success": True, "output": "User selected: Что-то другое"}

        registry = _Registry()
        _setup_tooling(mode, registry)
        bot_app = types.SimpleNamespace()
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        wm_ctx = WebmasterContext(key="k", goal="g")

        try:
            await mode._confirm_intent(
                bot_app=bot_app,
                session=session,
                context=object(),
                dest={"kind": "telegram", "chat_id": 1, "user_id": 2},
                wm_ctx=wm_ctx,
            )
            assert False, "expected RuntimeError for invalid selection"
        except RuntimeError as exc:
            assert "invalid selection" in str(exc)

    asyncio.run(_run())


def test_webmaster_run_pipeline_invalid_confirm_selection_requests_button_press(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        mode._mode_root = lambda: str(tmp_path)  # type: ignore[method-assign]

        async def _classify(*_a, **_k):
            return FeedbackDecision(kind="new_task", reason="r")

        mode._classify_feedback_llm = _classify  # type: ignore[method-assign]
        mode._analyze_intent = lambda **_k: asyncio.sleep(0, result={"goal": "g", "actions": ["a"]})  # type: ignore[method-assign]

        async def _invalid_confirm(*_args, **_kwargs):
            raise webmaster_mode_module._InvalidConfirmationSelection("ask_user returned invalid selection: free text")

        mode._confirm_intent = _invalid_confirm  # type: ignore[method-assign]

        bot_app = types.SimpleNamespace(
            config=types.SimpleNamespace(defaults=types.SimpleNamespace(webmaster_use_cli_timeout_sec=42)),
            _tool_registry=types.SimpleNamespace(),
        )
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        dest = {"chat_id": 1, "user_id": 9, "chat_type": "private"}

        out = await mode.run_pipeline(
            session=session,
            user_text="сделай задачу",
            bot_app=bot_app,
            context=None,
            dest=dest,
        )
        assert out == "Нужно выбрать один из вариантов кнопкой: Подтвердить, Уточнить или Новая задача."
        key = build_user_key(1, 9, "s1")
        saved = mode._store(session).load(key)
        assert saved.stage == "await_user_confirmation"

    asyncio.run(_run())
