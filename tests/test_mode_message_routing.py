import asyncio
import types

from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig
from bot import BotApp
from modes.sdk import BaseMode, ToolResult


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
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    )
    app = BotApp(cfg)
    # Avoid background mode tasks in tests.
    app._start_mode_task = lambda *_a, **_k: None
    return app


def test_routes_to_active_mode_plugin_when_set(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "echo"

        called = {"n": 0}
        sent = {"text": None}

        class EchoMode(BaseMode):
            mode_id = "echo"

            async def handle_input(self, message, ctx):
                called["n"] += 1
                assert message.text == "hi"
                return ToolResult.ok("PLUGIN:" + message.text)

            async def handle_callback(self, callback, ctx):
                return ToolResult.ok("x")

        app.mode_registry.register(EchoMode())

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kwargs):
            sent["text"] = text
            return True

        app._send_message = _send_message

        # Ensure fallback branches are not used.
        async def _nope(*_a, **_k):
            raise AssertionError("fallback branch called")

        app._handle_cli_input = _nope
        await app._handle_user_input(session, "hi", 1, context=object())
        assert called["n"] == 1
        assert sent["text"] == "PLUGIN:hi"

    asyncio.run(_run())


def test_migrated_active_mode_manager_routes_to_manager_plugin(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        # Simulate migrated state.
        session.modes.active_mode = "manager"

        called = {"plugin": 0}

        plugin = app.mode_registry.get("manager")
        assert plugin is not None

        async def _plugin_handle_input(message, ctx):
            called["plugin"] += 1
            assert message.text == "hi"
            assert ctx.get("session") is session
            return ToolResult.ok()

        plugin.handle_input = _plugin_handle_input  # type: ignore[assignment]
        app._handle_cli_input = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cli called"))

        await app._handle_user_input(session, "hi", 1, context=object())
        assert called["plugin"] == 1

    asyncio.run(_run())


def test_unknown_active_mode_falls_back_to_cli(tmp_path):
    async def _run():
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "unknown"
        called = {"cli": 0}

        async def _cli(*_a, **_k):
            called["cli"] += 1
            return None

        app._handle_cli_input = _cli

        await app._handle_user_input(session, "hi", 1, context=object())
        assert called["cli"] == 1

    asyncio.run(_run())


def test_active_mode_with_images_uses_fallback_image_cli_and_restores_active_cli(tmp_path):
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
            ),
            mcp=MCPConfig(enabled=False),
            mcp_clients=[],
            presets=[],
            path=str(tmp_path / "config.yaml"),
        )
        app = BotApp(cfg)
        app._start_mode_task = lambda *_a, **_k: None
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "echo"

        analyzed = {"text": None}
        cli_calls = []
        sent = {"text": None}

        class EchoMode(BaseMode):
            mode_id = "echo"

            async def handle_input(self, message, ctx):
                _ = ctx
                analyzed["text"] = message.text
                return ToolResult.ok("PLUGIN:" + message.text)

            async def handle_callback(self, callback, ctx):
                _ = (callback, ctx)
                return ToolResult.ok("x")

        app.mode_registry.register(EchoMode())

        async def _send_message(_ctx, *, chat_id: int, text: str, **_kwargs):
            _ = chat_id
            sent["text"] = text
            return True

        async def _fake_run_prompt(self, prompt: str, *args, **kwargs):
            cli_calls.append(
                {
                    "tool": self.tool.name,
                    "prompt": prompt,
                    "image_paths": list(kwargs.get("image_paths") or []),
                }
            )
            return f"ANALYSIS_FROM_{self.tool.name}"

        app._send_message = _send_message
        session.run_prompt = types.MethodType(_fake_run_prompt, session)

        await app._handle_user_input(
            session,
            "photo-caption",
            1,
            context=object(),
            dest={"kind": "telegram", "chat_id": 1, "image_paths": ["/tmp/a.png"]},
        )

        assert cli_calls == [
            {
                "tool": "gemini",
                "prompt": (
                    "Проанализируй приложенные изображения для последующей передачи в активный режим. "
                    "Верни короткую фактическую сводку: что изображено, какой текст виден, какие есть "
                    "ключевые объекты, структура, ошибки, диаграммы и детали, полезные для дальнейшей обработки.\n\n"
                    "Подпись или запрос пользователя:\nphoto-caption"
                ),
                "image_paths": ["/tmp/a.png"],
            }
        ]
        assert "Подпись пользователя:\nphoto-caption" in str(analyzed["text"] or "")
        assert "Анализ изображений через CLI gemini:\nANALYSIS_FROM_gemini" in str(analyzed["text"] or "")
        assert session.active_cli == "dummy"
        assert session.tool.name == "dummy"
        assert str(sent["text"] or "").startswith("PLUGIN:Пользователь приложил изображения")

    asyncio.run(_run())
