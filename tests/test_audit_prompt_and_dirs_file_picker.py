import asyncio

from agent.analyst_prompts import build_analyst_prompt
from app.services.dirs_service import DirsService
from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState
from tg.handlers import BotHandlers
from modes.analyst.mode import AnalystMode
from modes.registry import ModeRegistry
from modes.sdk import ModeRegistryService, encode_mode_dirs


def test_build_analyst_prompt_uses_audit_wording_when_template_is_audit() -> None:
    tmpl = {"_id": "audit", "required_sections": ["S1"]}
    out = build_analyst_prompt("goal", tmpl)
    assert "аудитор" in out.lower()
    assert "- S1" in out


def test_dirs_menu_includes_files_in_analyst_audit_mode(tmp_path) -> None:
    base = tmp_path / "root"
    base.mkdir()
    (base / "subdir").mkdir()
    (base / "file.txt").write_text("x", encoding="utf-8")

    class _FakeBotApp:
        def __init__(self):
            ui_key = TelegramUiKey.from_parts(100, None)
            self.ui_state = ChatUiState()
            self.ui_state.dirs_root[ui_key] = str(base)
            self.ui_state.dirs_mode[ui_key] = encode_mode_dirs("analyst", "audit")
            self._sent = []
            self.mode_registry = ModeRegistry()
            self.mode_registry_service = ModeRegistryService(self.mode_registry)
            self.mode_registry.register(AnalystMode())
            self.dirs_service = DirsService(self)
            self.access_policy_service = type(
                "_APS",
                (),
                {"ensure_allowed": staticmethod(lambda _chat_id, _context: asyncio.sleep(0, result=True))},
            )()

        @staticmethod
        def telegram_ui_key(chat_id: int, message_thread_id=None):
            return TelegramUiKey.from_parts(int(chat_id), message_thread_id)

        def _short_label(self, s: str) -> str:
            return s

        async def _send_message(self, _context, *, chat_id: int, text: str, reply_markup=None, **_kwargs):
            self._sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    bot_app = _FakeBotApp()
    h = BotHandlers(bot_app)
    asyncio.run(h._send_dirs_menu(100, context=object(), base=str(base)))

    items = bot_app.ui_state.dirs_menu.get(TelegramUiKey.from_parts(100, None)) or []
    assert str(base / "subdir") in items
    assert str(base / "file.txt") in items
    assert bot_app._sent and "файл" in bot_app._sent[-1]["text"].lower()
