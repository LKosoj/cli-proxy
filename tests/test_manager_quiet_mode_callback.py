import asyncio
import types

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
    return BotApp(cfg)


def test_manager_quiet_callback_toggles_and_rerenders_menu_via_mode_action(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = "manager"
        session.manager_quiet_mode = False

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:quiet_toggle"))
        await handler.handle_callback(update, context=object())

        assert session.manager_quiet_mode is True
        assert edits
        assert "Тихий режим: вкл" in edits[-1]["text"]

        keyboard = edits[-1]["reply_markup"]
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        quiet_buttons = [
            btn
            for btn in buttons
            if str(btn.callback_data or "").startswith("ma:manager:quiet_toggle")
        ]
        assert len(quiet_buttons) == 1
        assert quiet_buttons[0].text == "🔇 Тихий режим: вкл"

    asyncio.run(_run())


def test_manager_set_on_rerenders_menu_with_quiet_toggle_via_mode_action(tmp_path) -> None:
    async def _run() -> None:
        app = _build_app(tmp_path)
        session = app.manager.create(1, "dummy", str(tmp_path))
        session.modes.active_mode = None
        session.manager_quiet_mode = False

        edits = []

        async def _edit_message(_ctx, *, chat_id: int, message_id: int, text: str, reply_markup=None, **_kw):
            edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
            return True

        app._edit_message = _edit_message

        handler = CallbackHandler(app)
        update = types.SimpleNamespace(callback_query=_FakeQuery("ma:manager:enable"))
        await handler.handle_callback(update, context=object())

        assert session.modes.active_mode == "manager"
        assert edits
        assert "Режим: включен" in edits[-1]["text"]

        keyboard = edits[-1]["reply_markup"]
        buttons = [btn for row in keyboard.inline_keyboard for btn in row]
        quiet_buttons = [
            btn
            for btn in buttons
            if str(btn.callback_data or "").startswith("ma:manager:quiet_toggle")
        ]
        assert len(quiet_buttons) == 1

    asyncio.run(_run())
