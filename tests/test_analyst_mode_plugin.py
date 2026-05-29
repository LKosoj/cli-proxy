import asyncio
import time
import types

from tg.callbacks import CallbackHandler
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp
from app.services.run_artifact_store import RunArtifactStore
from modes.analyst.state_store import AnalystStateStore, build_context_key
from modes.sdk import MessageModel, encode_mode_dirs
from session import session_runtime_uid
from app.services.telegram_ui_scope import TelegramUiKey
from utils import cli_proxy_artifact_path


class _FakeMessage:
    def __init__(self, chat_id: int = 1, message_id: int = 10) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


def _build_app(tmp_path) -> BotApp:
    cfg = AppConfig(
        telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
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
            openai_api_key="k",
            openai_model="m",
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    return BotApp(cfg)


def test_analyst_mode_plugin_is_loaded(tmp_path):
    app = _build_app(tmp_path)
    assert app.mode_registry.get("analyst") is not None


def test_analyst_mode_enable_via_mode_action_callback(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        store = AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))
        context_key = build_context_key(session.chat_id, session.id)
        analyst_ctx = store.load(context_key)
        analyst_ctx.needs_clarification = True
        analyst_ctx.clarification_is_blocking = True
        analyst_ctx.clarification_topic = "scope"
        analyst_ctx.source_user_text = "старый запрос"
        analyst_ctx.clarification_answers = ["mobile"]
        analyst_ctx.last_draft = "stale draft"
        analyst_ctx.last_draft_updated_at = 11.0
        store.save(analyst_ctx)
        run_store = RunArtifactStore(app.config)
        run = run_store.start_run(
            session=session,
            mode_id="analyst",
            run_id="run_20260412T100000Z_enable_reset",
            phase="intent",
        )
        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message
        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:analyst:enable"))
        await handler.handle_callback(update, context=object())
        assert session.modes.active_mode == "analyst"
        updated_ctx = store.load(context_key)
        assert updated_ctx.needs_clarification is False
        assert updated_ctx.clarification_is_blocking is False
        assert updated_ctx.clarification_topic == ""
        assert updated_ctx.source_user_text == ""
        assert updated_ctx.clarification_answers == []
        assert updated_ctx.last_draft == ""
        assert updated_ctx.last_draft_updated_at == 0.0
        assert run_store.load_state(run)["status"] == "superseded"
        assert edits

    asyncio.run(_run())


def test_analyst_enable_clears_stale_state_before_next_user_input(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        mode = app.mode_registry.get("analyst")
        assert mode is not None

        store = AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))
        context_key = build_context_key(session.chat_id, session.id)
        analyst_ctx = store.load(context_key)
        analyst_ctx.needs_clarification = True
        analyst_ctx.clarification_is_blocking = True
        analyst_ctx.source_user_text = "старый запрос"
        analyst_ctx.clarification_answers = ["mobile"]
        store.save(analyst_ctx)

        run_store = RunArtifactStore(app.config)
        run_store.start_run(
            session=session,
            mode_id="analyst",
            run_id="run_20260412T100100Z_enable_followup",
            phase="intent",
        )

        sent = []
        captured = {"prompts": []}

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            _ = chat_id
            sent.append(text)
            return True

        async def _fake_run_pipeline(**kwargs):
            captured["prompts"].append(kwargs.get("user_text", ""))
            return ""

        app._send_message = _send_message
        mode.run_pipeline = _fake_run_pipeline

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:analyst:enable"))
        await handler.handle_callback(update, context=object())

        await mode.handle_input(
            MessageModel(text="новый запрос", chat_id=1),
            {
                "bot_app": app,
                "session": session,
                "chat_id": 1,
                "context": object(),
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured["prompts"] == ["новый запрос"]
        assert not any("Не удалось восстановить запрос для продолжения анализа" in text for text in sent)

    asyncio.run(_run())


def test_analyst_image_input_clears_transient_tick_state_before_pipeline(tmp_path):
    async def _run():
        cfg = AppConfig(
            telegram=TelegramConfig(token="", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
            tools={
                "dummy": ToolConfig(
                    name="dummy",
                    mode="headless",
                    cmd=["bash", "-lc", "cat"],
                ),
                "gemini": ToolConfig(
                    name="gemini",
                    mode="headless",
                    cmd=["bash", "-lc", "cat"],
                ),
            },
            defaults=DefaultsConfig(
                workdir=str(tmp_path),
                state_path=str(tmp_path / "state.json"),
                toolhelp_path=str(tmp_path / "toolhelp.json"),
                log_path=str(tmp_path / "bot.log"),
                openai_api_key="k",
                openai_model="m",
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        app = BotApp(cfg)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        mode = app.mode_registry.get("analyst")
        assert mode is not None

        captured = {"prompt": None, "tick_active": None}
        started_tasks = []

        async def _fake_run_mode_pipeline(_session, prompt, dest, _context, mode_id):
            assert mode_id == "analyst"
            assert int(dest.get("chat_id")) == 1
            captured["prompt"] = prompt
            captured["tick_active"] = session.is_active_by_tick()
            return None

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            _ = (prompt, args, kwargs)
            self.last_tick_ts = time.time()
            self.last_tick_value = "image-analysis"
            self.tick_seen = 1
            return "ANALYSIS_FROM_gemini"

        app.mode_pipeline.run_mode_pipeline_fn = _fake_run_mode_pipeline
        session.run_prompt = types.MethodType(_fake_run_prompt, session)
        mode._start_mode_task = (  # type: ignore[method-assign]
            lambda *, bot_app, session, coro, name: started_tasks.append(asyncio.create_task(coro))
        )

        await app._handle_user_input(
            session,
            "photo-caption",
            1,
            context=object(),
            dest={"kind": "telegram", "chat_id": 1, "image_paths": ["/tmp/a.png"]},
        )
        assert started_tasks
        await asyncio.gather(*started_tasks)

        assert "Подпись пользователя:\nphoto-caption" in str(captured["prompt"] or "")
        assert "Анализ изображений через CLI gemini:\nANALYSIS_FROM_gemini" in str(captured["prompt"] or "")
        assert captured["tick_active"] is False
        assert list(session.queue) == []

    asyncio.run(_run())


def test_analyst_audit_flow_uses_dirs_flow_and_selection_starts_audit(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        session.analyst_template_id = "default"
        session.modes.analyst_mode = "spec"

        ran = {"n": 0, "prompt": None}
        mode = app.mode_registry.get("analyst")
        assert mode is not None

        async def _fake_run_mode_pipeline(_session, prompt, dest, _context, *, mode_id):
            assert mode_id == "analyst"
            ran["n"] += 1
            ran["prompt"] = prompt
            assert dest.get("chat_id") == 1
            return None

        app.session_management.run_mode_pipeline = _fake_run_mode_pipeline

        sent = []

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kw):
            sent.append(text)
            return True

        app._send_message = _send_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:analyst:audit"))
        await handler.handle_callback(update, context=object())

        # Dirs flow should be active now; picking a path triggers audit run.
        ui_key = TelegramUiKey.from_parts(1, None)
        assert app.ui_state.dirs_mode.get(ui_key) == "mode:analyst:audit"
        p = tmp_path / "x"
        p.mkdir()
        await handler._dispatch_mode_dirs_event(
            chat_id=1,
            context=object(),
            event="pick",
            path=str(p),
        )
        await asyncio.sleep(0)
        assert ran["n"] == 1
        assert "Audit" in (ran["prompt"] or "")
        assert session.analyst_template_id == "default"
        assert session.modes.analyst_mode == "spec"

    asyncio.run(_run())


def test_analyst_dirs_selection_cancelled_is_noop(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        session.analyst_template_id = "default"
        session.modes.analyst_mode = "spec"
        mode = app.mode_registry.get("analyst")
        assert mode is not None

        result = await mode.handle_dirs_selection(
            flow="audit",
            event="cancelled",
            path="",
            ctx={"bot_app": app, "session": session, "chat_id": 1, "context": object()},
        )

        assert result is not None
        assert bool(result.ok) is True
        assert "отменен" in str(result.output or "").lower()
        assert session.analyst_template_id == "default"
        assert session.modes.analyst_mode == "spec"
        assert app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="analyst") == []

    asyncio.run(_run())


def test_analyst_mode_handle_input_passes_user_id_into_dest(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        mode = app.mode_registry.get("analyst")
        assert mode is not None

        captured = {"dest": None}

        async def _fake_run_mode_pipeline(_session, _prompt, dest, _context, *, mode_id):
            assert mode_id == "analyst"
            captured["dest"] = dict(dest or {})
            return None

        app.session_management.run_mode_pipeline = _fake_run_mode_pipeline

        await mode.handle_input(
            MessageModel(text="hi", chat_id=1, user_id=88),
            {
                "bot_app": app,
                "session": session,
                "chat_id": 1,
                "context": object(),
            },
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert captured["dest"] is not None
        assert captured["dest"].get("user_id") == 88

    asyncio.run(_run())


def test_analyst_disable_clears_audit_dirs_flow_and_state(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "analyst"
        session.modes.analyst_mode = "awaiting_input"
        session.analyst_runtime_template_id = "audit"

        ui_key = TelegramUiKey.from_parts(1, None)
        app.ui_state.dirs_mode[ui_key] = encode_mode_dirs("analyst", "audit")
        app.ui_state.dirs_root[ui_key] = str(tmp_path)
        app.ui_state.dirs_menu[ui_key] = [str(tmp_path)]
        app.ui_state.dirs_base[ui_key] = str(tmp_path)
        app.ui_state.dirs_page[ui_key] = 0

        store = AnalystStateStore(cli_proxy_artifact_path(str(tmp_path), ".analyst_data"))
        context_key = build_context_key(session.chat_id, session.id)
        ctx = store.load(context_key)
        ctx.mode = "awaiting_input"
        ctx.active_flow = "audit"
        ctx.runtime_template_id = "audit"
        store.save(ctx)

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:analyst:disable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode is None
        assert session.modes.analyst_mode == "spec"
        assert session.analyst_runtime_template_id == ""
        assert ui_key not in app.ui_state.dirs_mode
        assert ui_key not in app.ui_state.dirs_root
        assert ui_key not in app.ui_state.dirs_menu
        assert ui_key not in app.ui_state.dirs_base
        assert ui_key not in app.ui_state.dirs_page

        updated_ctx = store.load(context_key)
        assert updated_ctx.mode == "spec"
        assert updated_ctx.active_flow == ""
        assert updated_ctx.runtime_template_id == ""

    asyncio.run(_run())
