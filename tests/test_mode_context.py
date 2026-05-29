import logging
from types import SimpleNamespace

from modes.sdk import (
    AgentRuntimeService,
    BaseMode,
    DialogService,
    DirsFlowService,
    MessageModel,
    MessagingService,
    ModeRuntimeContext,
    ModeToolingService,
    SessionControlService,
    TaskService,
    ToolResult,
    mode_runtime_context_from_legacy,
)
from modes.sdk.models import CallbackModel


class _ProbeMode(BaseMode):
    mode_id = "probe"

    async def handle_input(self, message: MessageModel, ctx):
        return ToolResult.ok(message.text)

    async def handle_callback(self, callback: CallbackModel, ctx):
        return ToolResult.ok(callback.action)


async def _send_message(_context, **_kwargs):
    return None


async def _execute_tool(_tool_name, _args, _ctx):
    return {"success": True, "output": {"selected_option": "ok"}}


def _build_mode(*, runtime):
    mode = _ProbeMode()
    mode.initialize(
        config=SimpleNamespace(name="config"),
        services={
            "tasks": TaskService(),
            "dialogs": DialogService(),
            "session_control": SessionControlService(persist_sessions=lambda: None),
            "messaging_factory": (
                lambda transport_context: MessagingService(
                    send_message=_send_message,
                    transport_context=transport_context,
                )
            ),
            "tooling": ModeToolingService(execute_tool_fn=_execute_tool),
            "runtime_by_capability": lambda capability: runtime if capability == "probe_runtime" else None,
        },
    )
    return mode


def test_mode_runtime_context_from_legacy_uses_sdk_services_without_bot_app():
    runtime = object()
    mode = _build_mode(runtime=runtime)
    session = SimpleNamespace(id="s1")
    transport_context = object()

    runtime_context = mode_runtime_context_from_legacy(
        {
            "session": session,
            "chat_id": 123,
            "user_id": 456,
            "context": transport_context,
            "dest": {"kind": "telegram", "chat_id": 123},
        },
        mode,
    )

    assert isinstance(runtime_context, ModeRuntimeContext)
    assert runtime_context.mode_id == "probe"
    assert runtime_context.session is session
    assert runtime_context.chat_id == 123
    assert runtime_context.user_id == 456
    assert runtime_context.dest == {"kind": "telegram", "chat_id": 123}
    assert runtime_context.transport_context is transport_context
    assert runtime_context.context is transport_context
    assert runtime_context.config is mode.config
    assert runtime_context.messaging.transport_context is transport_context
    assert isinstance(runtime_context.tasks, TaskService)
    assert isinstance(runtime_context.dialogs, DialogService)
    assert isinstance(runtime_context.session_control, SessionControlService)
    assert isinstance(runtime_context.tooling, ModeToolingService)
    assert runtime_context.runtime_by_capability("probe_runtime") is runtime


def test_mode_runtime_context_preserves_legacy_bot_app_key_for_old_modes():
    mode = _build_mode(runtime=object())
    legacy_bot_app = object()
    legacy_ctx = {
        "bot_app": legacy_bot_app,
        "session": SimpleNamespace(id="s1"),
        "chat_id": 123,
        "context": object(),
    }

    runtime_context = mode_runtime_context_from_legacy(legacy_ctx, mode)

    assert runtime_context.session is legacy_ctx["session"]
    assert legacy_ctx["bot_app"] is legacy_bot_app


def test_sdk_state_services_cover_agent_read_only_state_with_fake_sessions(caplog):
    pending_questions = {
        "q2": {
            "session_id": "s1",
            "chat_id": "1",
            "awaiting_custom": True,
            "created_at": 20.0,
        },
        "q1": {
            "session_id": "s1",
            "chat_id": 1,
            "awaiting_custom": False,
            "created_at": 10.0,
        },
        "q_other_chat": {
            "session_id": "s1",
            "chat_id": 2,
            "awaiting_custom": True,
            "created_at": 30.0,
        },
        "q_fake": {
            "session_id": "fake-session",
            "awaiting_custom": False,
            "created_at": 5.0,
        },
    }
    dialogs = DialogService(pending_questions_provider=lambda: pending_questions)
    session = SimpleNamespace(
        id="s1",
        conversation_scope=SimpleNamespace(chat_id=1),
    )

    items = dialogs.pending_questions_list(session=session)

    assert [item["question_id"] for item in items] == ["q1", "q2"]
    assert dialogs.pending_questions_count(session=session) == 2
    assert dialogs.pending_questions_summary(session=session) == {
        "count": 2,
        "awaiting_custom": True,
        "active_question_id": "q2",
    }
    assert dialogs.pending_questions_count(session=SimpleNamespace(id="fake-session")) == 1

    dirs = DirsFlowService(
        get_mode_token_fn=(
            lambda chat_id, message_thread_id=None: f"agent:{chat_id}:{message_thread_id}"
        ),
    )
    assert dirs.active_token(1, message_thread_id=7) == "agent:1:7"

    uid_session = SimpleNamespace(id="uid-session")
    short_session = SimpleNamespace(id="short-session")
    sessions_by_uid = {"chat:1:s1": uid_session}
    sessions_by_chat = {1: {"short": short_session}}
    runtime = AgentRuntimeService(
        get_session_by_uid_fn=(
            lambda session_uid, chat_id=None: sessions_by_uid.get(session_uid)
            or sessions_by_chat.get(int(chat_id or 0), {}).get(session_uid)
        ),
    )

    assert runtime.get_session_by_uid("chat:1:s1") is uid_session
    assert runtime.get_session_by_uid("short", chat_id=1) is short_session

    caplog.set_level(logging.WARNING)
    assert DialogService().pending_questions_count(session=session) == 0
    assert DirsFlowService().active_token(1) == ""
    assert AgentRuntimeService().get_session_by_uid("missing") is None
    assert "pending questions backend unavailable" in caplog.text
    assert "dirs flow token backend unavailable" in caplog.text
    assert "agent runtime session lookup backend unavailable" in caplog.text
