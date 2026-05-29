import asyncio
import types
import os

from bot import BotApp
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from modes.codebase_mapper import mode as mapper_mode_module
from modes.codebase_mapper.runtime import CodebaseMapperRuntime
from modes.sdk.services.callback_data import build_mode_action_callback_data
from session import session_runtime_uid
from tg.callbacks import CallbackHandler


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


async def _wait_until(predicate, *, timeout: float = 1.0, step: float = 0.01) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout)
    while loop.time() < deadline:
        if bool(predicate()):
            return
        await asyncio.sleep(step)
    raise AssertionError("timeout waiting for condition")


def test_codebase_mapper_callback_run_starts_background_and_auto_disables(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"
        mode = app.mode_registry.get("codebase_mapper")
        assert mode is not None

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        started = asyncio.Event()
        release = asyncio.Event()

        async def _fake_run_pipeline(**_kwargs):
            started.set()
            await release.wait()
            return "done"

        mode.run_pipeline = _fake_run_pipeline

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:run"))
        await handler.handle_callback(update, context=object())

        assert edits
        assert "Обновление карты запущено" in edits[-1][2]
        assert edits[-1][3] is None

        await asyncio.sleep(0)
        assert started.is_set()
        assert bool(session.busy) is True

        release.set()
        await _wait_until(
            lambda: (
                session.modes.active_mode is None
                and app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="codebase_mapper") == []
            )
        )

        assert session.modes.active_mode is None
        assert app.mode_tasks.list(session_uid=session_runtime_uid(session), mode_id="codebase_mapper") == []

    asyncio.run(_run())


def test_codebase_mapper_interrupt_cancels_active_build(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"
        mode = app.mode_registry.get("codebase_mapper")
        assert mode is not None

        cancelled = {"value": False}

        async def _fake_run_pipeline(**_kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled["value"] = True
                raise

        mode.run_pipeline = _fake_run_pipeline

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:run"))
        await handler.handle_callback(update, context=object())
        await asyncio.sleep(0)

        session_uid = session_runtime_uid(session)
        assert app.mode_tasks.list(session_uid=session_uid, mode_id="codebase_mapper")

        session.interrupt()
        await app.mode_session_control.cancel_session(session_id=session_uid, timeout_s=0.2)
        await asyncio.sleep(0)

        assert cancelled["value"] is True
        assert app.mode_tasks.list(session_uid=session_uid, mode_id="codebase_mapper") == []
        assert session.modes.active_mode is None

    asyncio.run(_run())


def test_codebase_mapper_init_when_graph_exists_shows_choice_keyboard(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"
        runtime = CodebaseMapperRuntime()

        map_dir = tmp_path / ".cli-proxy/.codebase_map"
        os.makedirs(map_dir, exist_ok=True)
        (map_dir / "INDEX.md").write_text("# index\n", encoding="utf-8")
        runtime.write_graph_state(
            workdir=str(tmp_path),
            state={"state": "ready", "tree": [".cli-proxy/.codebase_map/", "  INDEX.md"]},
        )
        (map_dir / "graph.json").write_text('{"nodes":[{"id":"node:workspace"}]}', encoding="utf-8")
        (map_dir / "meta.json").write_text('{"generated_at":"2026-01-01T00:00:00Z"}', encoding="utf-8")

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:init"))
        await handler.handle_callback(update, context=object())

        assert edits
        _chat_id, _msg_id, text, keyboard = edits[-1]
        assert "Граф уже инициализирован" in text
        assert keyboard is not None
        rows = list(getattr(keyboard, "inline_keyboard", []) or [])
        callbacks = [btn.callback_data for row in rows for btn in row]
        assert build_mode_action_callback_data("codebase_mapper", "init_choice", session=session, payload="full") in callbacks
        assert build_mode_action_callback_data("codebase_mapper", "init_choice", session=session, payload="verify") in callbacks
        assert build_mode_action_callback_data("codebase_mapper", "init_choice", session=session, payload="cancel") in callbacks

    asyncio.run(_run())


def test_codebase_mapper_review_shows_paginated_list_and_confirm(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"
        runtime = CodebaseMapperRuntime()

        map_dir = tmp_path / ".cli-proxy/.codebase_map"
        nodes_dir = map_dir / "nodes"
        os.makedirs(nodes_dir, exist_ok=True)
        for i in range(3):
            (nodes_dir / f"n{i}.md").write_text(f"# n{i}\n", encoding="utf-8")
        (map_dir / "INDEX.md").write_text("# index\n", encoding="utf-8")
        (map_dir / "graph.json").write_text(
            '{"nodes":[{"path":"nodes/n0.md"},{"path":"nodes/n1.md"},{"path":"nodes/n2.md"}]}',
            encoding="utf-8",
        )
        state_payload = {
            "state": "needs_review",
            "review_items": ["nodes/n0.md", "nodes/n1.md", "nodes/n2.md"],
            "needs_review": ["nodes/n0.md", "nodes/n1.md", "nodes/n2.md"],
            "reviewed": {},
        }
        runtime.write_graph_state(
            workdir=str(tmp_path),
            state=state_payload,
        )
        (map_dir / "meta.json").write_text('{"generated_at":"2026-01-01T00:00:00Z"}', encoding="utf-8")

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:review"))
        await handler.handle_callback(update, context=object())
        assert edits
        _chat_id, _msg_id, text, keyboard = edits[-1]
        assert "Ревью графа" in text
        callbacks = [btn.callback_data for row in list(getattr(keyboard, "inline_keyboard", []) or []) for btn in row]
        confirm_zero = build_mode_action_callback_data("codebase_mapper", "review_confirm", session=session, payload="0")
        assert confirm_zero in callbacks

        update_confirm = types.SimpleNamespace(callback_query=_FakeQuery(confirm_zero))
        await handler.handle_callback(update_confirm, context=object())
        state = runtime.read_graph_state(workdir=str(tmp_path))
        reviewed = dict(state.get("reviewed") or {})
        assert reviewed.get("nodes/n0.md") is True

    asyncio.run(_run())


def test_codebase_mapper_enable_clears_cli_work_type(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"
        session.cli_work_type = "analytics"

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:enable"))
        await handler.handle_callback(update, context=object())

        assert session.cli_work_type is None

    asyncio.run(_run())


def test_codebase_mapper_menu_hides_validate_button(tmp_path):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    session.modes.active_mode = "codebase_mapper"
    mode = app.mode_registry.get("codebase_mapper")
    assert mode is not None

    _text, keyboard = mode.build_menu(session)
    callbacks = [btn.callback_data for row in list(getattr(keyboard, "inline_keyboard", []) or []) for btn in row]
    assert "ma:codebase_mapper:validate" not in callbacks
    assert build_mode_action_callback_data("codebase_mapper", "repair", session=session) in callbacks


def test_codebase_mapper_validate_and_repair_callbacks_start_validate_plus_repair(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"

        edits = []
        prompts = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        async def _fake_start_background_pipeline(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))

        app._edit_message = _edit_message
        mode = app.mode_registry.get("codebase_mapper")
        mode._start_background_pipeline = _fake_start_background_pipeline

        handler = CallbackHandler(app)
        update_validate = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:validate"))
        await handler.handle_callback(update_validate, context=object())
        update_repair = types.SimpleNamespace(callback_query=_FakeQuery("ma:codebase_mapper:repair"))
        await handler.handle_callback(update_repair, context=object())

        assert prompts == ["repair", "repair"]
        assert all("Validate + Repair запущен" in row[2] for row in edits)

    asyncio.run(_run())


def test_codebase_mapper_launch_callbacks_blocked_by_all_busy_signals_and_recover(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "codebase_mapper"
        mode = app.mode_registry.get("codebase_mapper")
        assert mode is not None

        prompts = []
        edits = []
        tick_state = {"active": False}
        session.is_active_by_tick = lambda: bool(tick_state["active"])

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        async def _fake_start_background_pipeline(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))

        app._edit_message = _edit_message
        mode._start_background_pipeline = _fake_start_background_pipeline

        handler = CallbackHandler(app)
        actions = [
            ("ma:codebase_mapper:run", "run"),
            ("ma:codebase_mapper:refresh", "force"),
            ("ma:codebase_mapper:validate", "repair"),
            ("ma:codebase_mapper:repair", "repair"),
            ("ma:codebase_mapper:init", "init"),
            ("ma:codebase_mapper:init_choice:full", "init_full"),
            ("ma:codebase_mapper:init_choice:verify", "verify"),
        ]

        for action, expected_prompt in actions:
            # busy=true
            session.busy = True
            blocked_before = len(prompts)
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery(action)),
                context=object(),
            )
            assert len(prompts) == blocked_before
            assert "уже выполняется" in str(edits[-1][2] or "").lower()

            session.busy = False
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery(action)),
                context=object(),
            )
            assert prompts[-1] == expected_prompt

            # run_lock.locked()=true
            await session.run_lock.acquire()
            try:
                blocked_before = len(prompts)
                await handler.handle_callback(
                    types.SimpleNamespace(callback_query=_FakeQuery(action)),
                    context=object(),
                )
                assert len(prompts) == blocked_before
                assert "уже выполняется" in str(edits[-1][2] or "").lower()
            finally:
                session.run_lock.release()

            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery(action)),
                context=object(),
            )
            assert prompts[-1] == expected_prompt

            # tick-active=true
            tick_state["active"] = True
            blocked_before = len(prompts)
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery(action)),
                context=object(),
            )
            assert len(prompts) == blocked_before
            assert "уже выполняется" in str(edits[-1][2] or "").lower()

            tick_state["active"] = False
            await handler.handle_callback(
                types.SimpleNamespace(callback_query=_FakeQuery(action)),
                context=object(),
            )
            assert prompts[-1] == expected_prompt

    asyncio.run(_run())


def test_codebase_mapper_clone_prefers_qwen_when_available(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    app.config.tools["qwen"] = ToolConfig(name="qwen", mode="headless", cmd=["bash", "-lc", "cat"])
    session = app.manager.create(1, "dummy", str(tmp_path))
    mode = app.mode_registry.get("codebase_mapper")
    assert mode is not None

    monkeypatch.setattr(mapper_mode_module, "is_tool_available", lambda *_args, **_kwargs: True)
    clone = mode._clone_cli_session(base_session=session, focus="tech")
    assert clone.active_cli == "qwen"
    assert clone.tool.name == "qwen"


def test_codebase_mapper_clone_uses_active_cli_when_qwen_unavailable(tmp_path, monkeypatch):
    app = _build_app(tmp_path)
    session = app.manager.create(1, "dummy", str(tmp_path))
    mode = app.mode_registry.get("codebase_mapper")
    assert mode is not None

    monkeypatch.setattr(mapper_mode_module, "is_tool_available", lambda *_args, **_kwargs: False)
    clone = mode._clone_cli_session(base_session=session, focus="tech")
    assert clone.active_cli == session.active_cli
    assert clone.tool.name == session.tool.name


def test_codebase_mapper_run_pipeline_makes_checkpoints_before_start_and_finish(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        mode = app.mode_registry.get("codebase_mapper")
        assert mode is not None

        labels = []

        async def _fake_checkpoint(_session, label: str):
            labels.append(str(label))
            return True

        mode._silent_git_checkpoint = _fake_checkpoint
        runtime_getter = mode.require_service("runtime_by_capability")
        mapper_runtime = runtime_getter("codebase_mapper_run")
        assert mapper_runtime is not None

        async def _fake_maybe_run(**_kwargs):
            return {
                "status": "partial_updated",
                "reason": "incremental",
                "updated_docs": ["ARCHITECTURE.md"],
                "changed_files": ["modes/codebase_mapper/mode.py"],
                "graph_state": "ready",
                "graph_nodes": 1,
                "graph_tree": [],
                "map_dir": str(tmp_path / ".cli-proxy/.codebase_map"),
            }

        mapper_runtime.maybe_run = _fake_maybe_run

        out = await mode.run_pipeline(
            session=session,
            user_text="run",
            bot_app=app,
            context=object(),
            dest={"kind": "telegram", "chat_id": 1},
        )
        assert "Status: partial_updated" in out
        assert labels == ["before_start", "before_finish"]

    asyncio.run(_run())


def test_codebase_mapper_run_pipeline_makes_finish_checkpoint_on_error(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        mode = app.mode_registry.get("codebase_mapper")
        assert mode is not None

        labels = []

        async def _fake_checkpoint(_session, label: str):
            labels.append(str(label))
            return True

        mode._silent_git_checkpoint = _fake_checkpoint
        runtime_getter = mode.require_service("runtime_by_capability")
        mapper_runtime = runtime_getter("codebase_mapper_run")
        assert mapper_runtime is not None

        async def _boom(**_kwargs):
            raise RuntimeError("boom")

        mapper_runtime.maybe_run = _boom

        raised = False
        try:
            await mode.run_pipeline(
                session=session,
                user_text="run",
                bot_app=app,
                context=object(),
                dest={"kind": "telegram", "chat_id": 1},
            )
        except RuntimeError as e:
            raised = str(e) == "boom"
        assert raised is True
        assert labels == ["before_start", "before_finish"]

    asyncio.run(_run())
