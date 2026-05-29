import asyncio
import types

from agent.plugins.base import DialogMixin, ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec


class _ProbePlugin(DialogMixin, ToolPlugin):
    plugin_id = "plug"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="probe",
            description="probe",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, ctx):  # noqa: ANN001
        _ = args, ctx
        return {"ok": True}

    async def _on_do(self, update, context, payload):  # noqa: ANN001
        _ = context
        chat_id = int(getattr(getattr(update, "callback_query", None), "message", types.SimpleNamespace(chat_id=0)).chat_id)
        self.calls.append((str(payload or ""), chat_id))

    def callback_handlers(self):
        return {"do": self._on_do}


class _BotApp:
    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session
        self.called_scope_args: list[tuple[int, int | None, int]] = []

    def resolve_telegram_scope_session(self, *, reply_chat_id: int, message_thread_id=None, owner_chat_id=None):
        self.called_scope_args.append((int(reply_chat_id), message_thread_id, int(owner_chat_id or reply_chat_id)))
        return self._session

    @staticmethod
    def _mode_allows_plugin_ui(session) -> bool:  # noqa: ANN001
        return bool(session and str(getattr(session, "active_mode", "") or "").strip() == "agent")


class _CallbackQuery:
    def __init__(self, chat_id: int, data: str) -> None:
        self.data = str(data or "")
        self.message = types.SimpleNamespace(chat_id=int(chat_id))
        self.answers: list[tuple[tuple, dict]] = []

    async def answer(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.answers.append((args, kwargs))


class _Update:
    def __init__(self, chat_id: int, data: str) -> None:
        self.callback_query = _CallbackQuery(chat_id, data)
        self.effective_chat = types.SimpleNamespace(id=int(chat_id))


def _build_context(bot_app: _BotApp):
    return types.SimpleNamespace(
        application=types.SimpleNamespace(
            bot_data={"bot_app": bot_app},
        )
    )


def test_dialog_mixin_dispatch_callback_checks_agent_activity_by_chat_id() -> None:
    async def _run() -> None:
        plugin = _ProbePlugin()
        session = types.SimpleNamespace(active_mode="agent")
        bot_app = _BotApp(session)
        context = _build_context(bot_app)
        update = _Update(7, "cb:plug:do")

        await plugin._dispatch_callback(update, context)

        assert plugin.calls == [("", 7)]
        assert bot_app.called_scope_args == [(7, None, 7)]
        assert update.callback_query.answers

    asyncio.run(_run())


def test_dialog_mixin_dispatch_callback_rejects_when_agent_mode_is_off() -> None:
    async def _run() -> None:
        plugin = _ProbePlugin()
        session = types.SimpleNamespace(active_mode=None)
        bot_app = _BotApp(session)
        context = _build_context(bot_app)
        update = _Update(7, "cb:plug:do")

        await plugin._dispatch_callback(update, context)

        assert plugin.calls == []
        assert bot_app.called_scope_args == [(7, None, 7)]
        assert any(
            args and args[0] == "Агент не активен." and kwargs.get("show_alert") is True
            for args, kwargs in update.callback_query.answers
        )

    asyncio.run(_run())
