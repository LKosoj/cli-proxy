import pytest
import types

from desktop.services.application_facade import ApplicationFacade
from app.services.config_service import ConfigProvider, ConfigService
from app.services.session_service import SessionService
from app.services.task_service import TaskService
from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
from modes.registry import ModeRegistry
from modes.sdk import BaseMode, ToolResult
from modes.sdk.services.mode_registry import ModeRegistryService
from modes.sdd.mode import SddMode
from session import SddState
from session import SessionManager, session_runtime_uid


class _InMemoryConfigProvider(ConfigProvider):
    def __init__(self, config: AppConfig):
        self.config = config

    async def load(self) -> AppConfig:
        return self.config

    async def get(self, key: str, default=None):
        current = self.config
        for part in str(key or "").split("."):
            token = part.strip()
            if not token:
                continue
            if isinstance(current, dict):
                if token not in current:
                    return default
                current = current[token]
                continue
            if not hasattr(current, token):
                return default
            current = getattr(current, token)
        return current


def _build_config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={
            "dummy": ToolConfig(
                name="dummy",
                mode="headless",
                cmd=["bash", "-lc", "cat"],
            )
        },
        defaults=DefaultsConfig(
            workdir=str(tmp_path / "workdir"),
            state_path=str(tmp_path / "runtime" / "state.json"),
            toolhelp_path=str(tmp_path / "runtime" / "toolhelp.json"),
            log_path=str(tmp_path / "logs" / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(),
    )


@pytest.mark.asyncio
async def test_mode_menu_delivered_as_ui_mode_menu_event(tmp_path) -> None:
    cfg = _build_config(tmp_path)
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    registry = ModeRegistry()

    class MenuMode(BaseMode):
        mode_id = "menu"

        async def handle_input(self, message, ctx):
            return ToolResult.ok()

        async def handle_callback(self, callback, ctx):
            ms = self._messaging(bot_app=ctx.get("bot_app"), context=ctx.get("context"))
            # Provide Telegram-style inline keyboard.
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            kb = InlineKeyboardMarkup([[InlineKeyboardButton("OK", callback_data="ma:menu:status:{}")]])
            await ms.send_plain_text(callback.chat_id, "MENU", reply_markup=kb)
            return ToolResult.ok("sent")

        async def run_pipeline(self, *, session, user_text, bot_app, context, dest):
            return "PIPE"

    registry.register(MenuMode())
    mode_registry_service = ModeRegistryService(registry)

    facade = ApplicationFacade(
        config_service=ConfigService(_InMemoryConfigProvider(cfg)),
        session_service=SessionService(SessionManager(cfg), TaskService()),
        task_service=TaskService(),
        git_service=None,
        mode_registry_service=mode_registry_service,
    )
    facade.config = cfg

    session = facade.session_service.create_desktop_session("dummy", str(tmp_path))
    session_uid = session_runtime_uid(session)
    session.modes.active_mode = "menu"

    got = {"menu": False}

    def _sub(note):
        if note.event == "ui:mode_menu":
            got["menu"] = True
            assert "rows" in note.payload
            assert note.payload["rows"][0][0]["data"].startswith("ma:")

    facade.subscribe(_sub)

    ok = await facade.handle_mode_callback(session_uid, data="ma:menu:any:{}")
    assert ok is True
    assert got["menu"] is True


def test_desktop_extracts_sdd_project_init_button_from_mode_menu() -> None:
    session = types.SimpleNamespace(
        id="s1",
        modes=types.SimpleNamespace(active_mode="sdd"),
        sdd=SddState(),
    )

    _text, keyboard = SddMode().build_menu(session)
    rows = ApplicationFacade._extract_inline_keyboard(keyboard)

    flat = [button for row in rows for button in row]
    assert any(
        button["text"] == "🧭 Инициализировать проект"
        and button["data"].startswith("ma:sdd:init_project")
        for button in flat
    )
