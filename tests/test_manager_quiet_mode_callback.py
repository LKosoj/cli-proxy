import asyncio
import tempfile
import types

from callbacks import CallbackHandler


class _FakeMessage:
    def __init__(self, chat_id: int = 100, message_id: int = 200) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.edits = []

    async def answer(self) -> None:
        return None

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append({"text": text, "reply_markup": reply_markup})


class _FakeManager:
    def __init__(self, session) -> None:
        self._session = session
        self.persist_calls = 0

    def active(self):
        return self._session

    def _persist_sessions(self) -> None:
        self.persist_calls += 1


class _FakeBotApp:
    def __init__(self, session) -> None:
        self.manager = _FakeManager(session)
        self.context_by_chat = {}
        self.manager_tasks = {}
        self.config = types.SimpleNamespace(
            defaults=types.SimpleNamespace(
                openai_api_key="test-key",
                openai_model="gpt-test",
            )
        )

    def is_allowed(self, _chat_id: int) -> bool:
        return True


def test_manager_quiet_callback_toggles_and_rerenders_menu() -> None:
    session = types.SimpleNamespace(
        id="s1",
        manager_enabled=True,
        manager_quiet_mode=False,
    )
    bot_app = _FakeBotApp(session)
    handler = CallbackHandler(bot_app)
    query = _FakeQuery("manager_quiet:toggle")
    update = types.SimpleNamespace(callback_query=query)

    asyncio.run(handler.handle_callback(update, context=object()))

    assert session.manager_quiet_mode is True
    assert session.manager_enabled is True
    assert bot_app.manager.persist_calls == 1
    assert query.edits
    assert "Тихий режим: вкл" in query.edits[-1]["text"]

    keyboard = query.edits[-1]["reply_markup"]
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    quiet_buttons = [btn for btn in buttons if btn.callback_data == "manager_quiet:toggle"]
    assert len(quiet_buttons) == 1
    assert quiet_buttons[0].text == "🔇 Тихий режим: вкл"


def test_manager_set_on_rerenders_menu_with_quiet_toggle() -> None:
    with tempfile.TemporaryDirectory() as workdir:
        session = types.SimpleNamespace(
            id="s1",
            workdir=workdir,
            manager_enabled=False,
            manager_quiet_mode=False,
            agent_enabled=True,
        )
        bot_app = _FakeBotApp(session)
        handler = CallbackHandler(bot_app)
        query = _FakeQuery("manager_set:on")
        update = types.SimpleNamespace(callback_query=query)

        asyncio.run(handler.handle_callback(update, context=object()))

    assert session.manager_enabled is True
    assert session.agent_enabled is False
    assert bot_app.manager.persist_calls == 1
    assert query.edits
    assert "Режим: включен" in query.edits[-1]["text"]

    keyboard = query.edits[-1]["reply_markup"]
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    quiet_buttons = [btn for btn in buttons if btn.callback_data == "manager_quiet:toggle"]
    assert len(quiet_buttons) == 1
