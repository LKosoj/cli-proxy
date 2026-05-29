import asyncio
import types

from app.services.telegram_ui_scope import TelegramUiKey
from app.services.ui_state_models import ChatUiState
from tg.callbacks import CallbackHandler
from modes.analyst.mode import AnalystMode
from modes.registry import ModeRegistry
from modes.sdk import (
    DirsFlowService,
    MessagingService,
    ModeCallbackRouterService,
    ModePipelineService,
    ModeRegistryService,
    SessionControlService,
    TaskService,
)


class _FakeMessage:
    def __init__(self, chat_id: int = 100, message_id: int = 200) -> None:
        self.chat_id = chat_id
        self.message_id = message_id


class _FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _FakeMessage()
        self.from_user = types.SimpleNamespace(id=42)

    async def answer(self) -> None:
        return None


class _FakeManager:
    def __init__(self, session) -> None:
        self._session = session

    def active(self, _chat_id: int):
        return self._session

    def _persist_sessions(self) -> None:
        return None


class _FakeModeDialogs:
    def __init__(self) -> None:
        self.started = []

    def is_active(self, *, chat_id: int, session_id: str, mode_id: str) -> bool:
        return False

    def start(self, *, chat_id: int, session_id: str, mode_id: str, on_message, data, timeout_s: float) -> None:
        self.started.append(
            {
                "chat_id": chat_id,
                "session_id": session_id,
                "mode_id": mode_id,
                "data": data,
                "timeout_s": timeout_s,
            }
        )


def test_analyst_audit_callback_starts_dirs_flow_via_mode_action(tmp_path) -> None:
    session = types.SimpleNamespace(id="s1", workdir=str(tmp_path), active_mode="analyst")
    starts = []

    registry = ModeRegistry()
    mode = AnalystMode()
    registry.register(mode)
    ui_state = ChatUiState()

    bot_app = types.SimpleNamespace(
        manager=_FakeManager(session),
        mode_registry=registry,
        mode_dialogs=_FakeModeDialogs(),
        mode_tasks=TaskService(),
        context_by_chat={},
        ui_state=ui_state,
        ensure_allowed=(lambda _chat_id, _ctx: asyncio.sleep(0, result=True)),
        is_admin=(lambda _chat_id: True),
        config=types.SimpleNamespace(defaults=types.SimpleNamespace(openai_api_key="k", openai_model="m", workdir=str(tmp_path))),
        _send_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
        _edit_message=(lambda *_a, **_k: asyncio.sleep(0, result=True)),
        git=types.SimpleNamespace(handle_callback=(lambda *_a, **_k: asyncio.sleep(0, result=False))),
        session_ui=types.SimpleNamespace(handle_callback=(lambda *_a, **_k: asyncio.sleep(0, result=False))),
        pending={},
        telegram_ui_key=(lambda chat_id, message_thread_id=None: TelegramUiKey.from_parts(chat_id, message_thread_id)),
        telegram_ui_key_from_query=(
            lambda query: TelegramUiKey.from_parts(
                int(getattr(getattr(query, "message", None), "chat_id", 0) or 0),
                getattr(getattr(query, "message", None), "message_thread_id", None),
            )
        ),
    )
    bot_app.access_policy_service = types.SimpleNamespace(
        ensure_allowed=(lambda _chat_id, _ctx: asyncio.sleep(0, result=True)),
        is_admin=(lambda _chat_id, scope="generic": True),
        callback_admin_scope=(lambda _chat_id, _data, **_kwargs: ""),
        admin_denied_text=(lambda scope="generic": "denied"),
        is_mode_allowed_for_chat=(lambda _chat_id, _mode_id: True),
    )
    bot_app.mode_registry_service = ModeRegistryService(bot_app.mode_registry)
    bot_app.mode_callback_router = ModeCallbackRouterService(
        mode_registry=bot_app.mode_registry_service,
        dialogs=bot_app.mode_dialogs,
        send_message=bot_app._send_message,
        get_session=lambda chat_id: bot_app.manager.active(chat_id),
        get_dirs_mode_token=lambda chat_id, message_thread_id=None: str(
            bot_app.ui_state.dirs_mode.get(TelegramUiKey.from_parts(int(chat_id), message_thread_id), "") or ""
        ),
        clear_dirs_mode_token=lambda chat_id, message_thread_id=None: bot_app.ui_state.dirs_mode.pop(
            TelegramUiKey.from_parts(int(chat_id), message_thread_id),
            None,
        ),
    )
    mode.initialize(
        config=bot_app.config,
        services={
            "tasks": bot_app.mode_tasks,
            "dialogs": bot_app.mode_dialogs,
            "dirs_flow": DirsFlowService(
                start_flow_fn=(
                    lambda chat_id, context, root, mode_token: asyncio.sleep(
                        0,
                        result=starts.append(
                            {"chat_id": chat_id, "root": root, "mode_token": mode_token}
                        ),
                    )
                ),
            ),
            "session_control": SessionControlService(
                persist_sessions=bot_app.manager._persist_sessions,
                cancel_mode_tasks=(lambda _sid, _mid, _timeout: asyncio.sleep(0, result=0)),
                cancel_session_tasks=(lambda _sid, _timeout: asyncio.sleep(0, result=0)),
            ),
            "pipeline": ModePipelineService(
                run_mode_pipeline_fn=(lambda *_a, **_k: asyncio.sleep(0)),
            ),
            "messaging_factory": (lambda ctx: MessagingService(
                send_message=bot_app._send_message,
                edit_message=bot_app._edit_message,
                transport_context=ctx,
            )),
        },
    )

    handler = CallbackHandler(bot_app)
    update = types.SimpleNamespace(callback_query=_FakeQuery("ma:analyst:audit"))
    asyncio.run(handler.handle_callback(update, context=object()))

    assert starts
    started = starts[-1]
    assert started["chat_id"] == 100
    assert started["root"] == str(tmp_path)
    assert started["mode_token"] == "mode:analyst:audit"
