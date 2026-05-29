import asyncio
import shutil
import types
from unittest.mock import Mock

import yaml

from modes.webmaster.mode import WebmasterMode
from tg.callbacks import CallbackHandler
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp


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
    app = BotApp(cfg)
    app._tool_registry = types.SimpleNamespace(
        execute=lambda *_a, **_k: None
    )
    return app


def test_webmaster_mode_plugin_is_loaded(tmp_path) -> None:
    app = _build_app(tmp_path)
    assert app.mode_registry.get("webmaster") is not None


def test_webmaster_run_pipeline_accepts_desktop_session_uid_chat_id(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))

        async def _resume_interrupted_pipeline(**_kwargs):
            return "desktop-ok"

        mode._resume_interrupted_pipeline = _resume_interrupted_pipeline  # type: ignore[method-assign]

        result = await mode.run_pipeline(
            session=session,
            user_text="build",
            bot_app=types.SimpleNamespace(),
            context=object(),
            dest={"kind": "desktop", "chat_id": "desktop:s1", "session_uid": "desktop:s1"},
        )

        assert result == "desktop-ok"

    asyncio.run(_run())


def test_webmaster_on_enable_accepts_desktop_session_uid_chat_id(tmp_path) -> None:
    async def _run() -> None:
        mode = WebmasterMode()
        session = types.SimpleNamespace(id="s1", workdir=str(tmp_path))
        calls = []

        async def _ensure_project_prompts_ready(**kwargs):
            calls.append(kwargs)
            return True

        async def _activate_mode(**_kwargs):
            return None

        mode._ensure_project_prompts_ready = _ensure_project_prompts_ready  # type: ignore[method-assign]
        mode._activate_mode = _activate_mode  # type: ignore[method-assign]

        result = await mode.on_enable({
            "session": session,
            "bot_app": types.SimpleNamespace(),
            "dest": {"kind": "desktop", "chat_id": "desktop:s1", "session_uid": "desktop:s1"},
        })

        assert result is None
        assert calls[0]["chat_id"] is None

    asyncio.run(_run())


def test_webmaster_mode_enable_via_mode_action_callback(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:webmaster:enable"))
        await handler.handle_callback(update, context=object())
        assert session.modes.active_mode == "webmaster"
        assert edits

    asyncio.run(_run())


def test_webmaster_mode_enable_recreates_project_prompts_when_missing(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        prompt_dir = tmp_path / ".cli-proxy" / ".webmaster" / "prompt"
        prompts_path = prompt_dir / "prompts.yaml"
        learning_path = prompt_dir / "learning.yaml"
        if prompts_path.exists():
            prompts_path.unlink()
        if learning_path.exists():
            learning_path.unlink()
        if prompt_dir.exists():
            shutil.rmtree(prompt_dir)

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:webmaster:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode == "webmaster"
        assert prompts_path.exists()
        assert learning_path.exists()
        assert edits

    asyncio.run(_run())


def test_webmaster_mode_enable_fails_on_invalid_project_prompts_yaml(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        prompts_path = tmp_path / ".cli-proxy" / ".webmaster" / "prompt" / "prompts.yaml"
        prompts_path.write_text("prompts: [broken", encoding="utf-8")
        mode = app.mode_registry.get("webmaster")
        exception_mock = Mock()
        mode._log.exception = exception_mock

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append((chat_id, message_id, text, reply_markup))
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:webmaster:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode is None
        assert edits
        assert "Не удалось включить Вебмастер" in edits[-1][2]
        assert exception_mock.called

    asyncio.run(_run())


def test_webmaster_mode_uses_updated_project_prompts_after_mode_restart(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        prompts_path = tmp_path / ".cli-proxy" / ".webmaster" / "prompt" / "prompts.yaml"
        mode = app.mode_registry.get("webmaster")

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            return True

        app._edit_message = _edit_message

        with open(prompts_path, "r", encoding="utf-8") as f:
            payload = yaml.safe_load(f) or {}
        prompts = payload.get("prompts") if isinstance(payload, dict) else {}
        if not isinstance(prompts, dict):
            prompts = {}
        prompts["confirmation"] = "Подтверждение #1"
        with open(prompts_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"prompts": prompts}, f, allow_unicode=True, sort_keys=False)

        handler = CallbackHandler(app)
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:webmaster:enable")),
            context=object(),
        )
        first = mode._load_prompts(session=session).get("confirmation")
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:webmaster:disable")),
            context=object(),
        )

        prompts["confirmation"] = "Подтверждение #2"
        with open(prompts_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"prompts": prompts}, f, allow_unicode=True, sort_keys=False)
        await handler.handle_callback(
            types.SimpleNamespace(callback_query=_FakeQuery("ma:webmaster:enable")),
            context=object(),
        )
        second = mode._load_prompts(session=session).get("confirmation")

        assert first == "Подтверждение #1"
        assert second == "Подтверждение #2"

    asyncio.run(_run())
