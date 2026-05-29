import asyncio
import types

from session import session_runtime_uid
from tg.handlers import BotHandlers


class _FakeSession:
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.interrupt_calls = 0

    def interrupt(self) -> None:
        self.interrupt_calls += 1


class _FakeManager:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def active(self, _chat_id: int):
        return self._session


class _FakeModeTasks:
    def __init__(self) -> None:
        self.calls = []

    async def cancel_session(self, *, session_id: str, timeout_s: float = 1.0) -> int:
        self.calls.append((session_id, timeout_s))
        return 1


class _FakeSessionControl:
    def __init__(self, mode_tasks: _FakeModeTasks) -> None:
        self._mode_tasks = mode_tasks

    async def cancel_session(self, *, session_id: str, timeout_s: float = 1.0) -> int:
        return await self._mode_tasks.cancel_session(session_id=session_id, timeout_s=timeout_s)


class _FakeBotApp:
    def __init__(self, session: _FakeSession) -> None:
        self.manager = _FakeManager(session)
        self.mode_tasks = _FakeModeTasks()
        self.mode_session_control = _FakeSessionControl(self.mode_tasks)
        self.messages = []

        class _APS:
            @staticmethod
            async def ensure_allowed(_chat_id, _context):
                return True

            async def require_scope_session(self, chat_id, _context, *, auto_create=False):
                _ = auto_create
                return self_outer.manager.active(chat_id)

        self_outer = self
        self.access_policy_service = _APS()

    async def _send_message(self, _context, *, chat_id: int, text: str, **_kwargs):
        self.messages.append((chat_id, text))


def test_cmd_interrupt_cancels_mode_tasks() -> None:
    async def _run() -> None:
        session = _FakeSession("s1")
        app = _FakeBotApp(session)
        handler = BotHandlers(app)

        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=100))
        await handler.cmd_interrupt(update, context=object())

        assert session.interrupt_calls == 1
        assert app.mode_tasks.calls == [(session_runtime_uid(session), 0.2)]
        assert app.messages == [(100, "Прерывание отправлено.")]

    asyncio.run(_run())
