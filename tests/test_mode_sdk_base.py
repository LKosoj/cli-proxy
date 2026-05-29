import asyncio
import inspect
import logging
from types import SimpleNamespace

import pytest

from app.mode_dependencies import ModeDependencies
from app.services.session_mutation_service import SessionMutationService
from modes.sdk import (
    BaseMode,
    CallbackModel,
    DialogService,
    MessageModel,
    MessagingService,
    ModeRuntimeContext,
    ModeToolingService,
    SessionControlService,
    TaskService,
    ToolResult,
)


class _RuntimeContextMode(BaseMode):
    mode_id = "runtime_probe"

    async def handle_input(self, message: MessageModel, ctx):
        return ToolResult.ok(message.text)

    async def handle_callback(self, callback: CallbackModel, ctx):
        return ToolResult.ok(callback.action)


async def _send_message(_context, **_kwargs):
    return None


def test_messaging_service_progress_methods_have_transport_neutral_signatures():
    send_params = inspect.signature(MessagingService.send_text).parameters
    plain_params = inspect.signature(MessagingService.send_plain_text).parameters
    edit_params = inspect.signature(MessagingService.edit_text).parameters
    remove_params = inspect.signature(MessagingService.remove).parameters

    assert {"chat_id", "text"} <= set(send_params)
    assert {"chat_id", "text"} <= set(plain_params)
    assert {"chat_id", "message_id", "text"} <= set(edit_params)
    assert {"chat_id", "message_id", "message_ref"} <= set(remove_params)
    assert "query" not in send_params
    assert "query" not in plain_params
    assert "query" not in edit_params
    assert "query" not in remove_params


def test_messaging_service_progress_api_uses_transport_neutral_calls():
    async def _run():
        transport_context = object()
        calls = []

        async def _send(context, **kwargs):
            calls.append(("send", context, dict(kwargs)))
            return SimpleNamespace(message_id=7)

        async def _edit(context, **kwargs):
            calls.append(("edit", context, dict(kwargs)))
            return SimpleNamespace(message_id=kwargs["message_id"])

        async def _delete(context, **kwargs):
            calls.append(("delete", context, dict(kwargs)))
            return True

        messaging = MessagingService(
            send_message=_send,
            edit_message=_edit,
            delete_message=_delete,
            transport_context=transport_context,
        )

        sent = await messaging.send_text(123, "classify", md2=False, thread_id=55)
        plain_sent = await messaging.send_plain_text(123, "literal _*[]", message_thread_id=77)
        edited = await messaging.edit_text(123, sent.message_id, "plan", md2=True)
        removed = await messaging.remove(123, message_ref=edited)
        sent_or_edited = await messaging.send_or_edit(
            chat_id=123,
            message_ref=sent,
            text="gather",
            md2=False,
        )

        assert removed is True
        assert sent_or_edited.message_id == 7
        assert plain_sent.message_id == 7
        assert calls == [
            (
                "send",
                transport_context,
                {"chat_id": 123, "text": "classify", "md2": False, "thread_id": 55},
            ),
            (
                "send",
                transport_context,
                {"chat_id": 123, "text": "literal _*[]", "md2": False, "message_thread_id": 77},
            ),
            (
                "edit",
                transport_context,
                {"chat_id": 123, "message_id": 7, "text": "plan", "md2": True},
            ),
            (
                "delete",
                transport_context,
                {"chat_id": 123, "message_id": 7},
            ),
            (
                "edit",
                transport_context,
                {"chat_id": 123, "message_id": 7, "text": "gather", "md2": False},
            ),
        ]

    asyncio.run(_run())


def test_messaging_service_progress_api_logs_failures_without_transport_primitives(caplog):
    async def _run():
        caplog.set_level(logging.ERROR, logger="modes.sdk.services.messaging")

        async def _fail_send(_context, **_kwargs):
            raise RuntimeError("telegram Update object leaked")

        async def _fail_edit(_context, **_kwargs):
            raise RuntimeError("telegram Message object leaked")

        async def _fail_delete(_context, **_kwargs):
            raise RuntimeError("telegram CallbackQuery object leaked")

        messaging = MessagingService(
            send_message=_fail_send,
            edit_message=_fail_edit,
            delete_message=_fail_delete,
            transport_context=SimpleNamespace(secret="telegram transport context"),
        )

        message_ref = SimpleNamespace(message_id=7, raw="telegram ref")

        with pytest.raises(RuntimeError):
            await messaging.send_text(
                123,
                "do not log this text",
                md2=False,
                reply_markup="telegram markup",
            )
        with pytest.raises(RuntimeError):
            await messaging.send_plain_text(
                123,
                "do not log this plain text",
                reply_markup="telegram markup",
            )
        with pytest.raises(RuntimeError):
            await messaging.edit_text(
                123,
                7,
                "do not log this edit",
                md2=True,
                query="telegram query",
            )
        with pytest.raises(RuntimeError):
            await messaging.remove(
                123,
                message_ref=message_ref,
                query="telegram query",
            )
        with pytest.raises(RuntimeError):
            await messaging.send_or_edit(
                chat_id=123,
                text="do not log send_or_edit",
                message_ref=message_ref,
                query=SimpleNamespace(message=SimpleNamespace(chat_id=999, message_id=8)),
                reply_markup="telegram markup",
            )

    asyncio.run(_run())

    log_text = caplog.text
    assert log_text.count("messaging.progress.failure") == 5
    assert "operation=send_text" in log_text
    assert "operation=edit_text" in log_text
    assert "operation=remove" in log_text
    assert "operation=send_or_edit" in log_text
    assert "target_id=123" in log_text
    assert "message_id=7" in log_text
    assert "error_type=RuntimeError" in log_text
    assert "telegram" not in log_text.lower()
    assert "do not log" not in log_text
    assert "reply_markup" not in log_text
    assert "query" not in log_text


async def _execute_tool(_tool_name, _args, _ctx):
    return {"success": True, "output": {"selected_option": "ok"}}


def test_base_mode_runtime_context_returns_typed_context_without_bot_app():
    runtime = object()
    mode = _RuntimeContextMode()
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
            "runtime_by_capability": lambda capability: runtime if capability == "runtime_probe" else None,
        },
    )
    session = SimpleNamespace(id="s1")
    transport_context = object()

    runtime_context = mode._runtime_context(
        {
            "session": session,
            "chat_id": 123,
            "user_id": 456,
            "transport_context": transport_context,
            "dest": {"kind": "desktop"},
        }
    )

    assert isinstance(runtime_context, ModeRuntimeContext)
    assert runtime_context.mode_id == "runtime_probe"
    assert runtime_context.session is session
    assert runtime_context.messaging.transport_context is transport_context
    assert runtime_context.tasks is mode.get_service("tasks")
    assert runtime_context.dialogs is mode.get_service("dialogs")
    assert runtime_context.session_control is mode.get_service("session_control")
    assert runtime_context.tooling is mode.get_service("tooling")
    assert runtime_context.runtime_by_capability("runtime_probe") is runtime


def test_base_mode_exposes_session_mutation_service_from_dependencies():
    service = SessionMutationService()
    deps = ModeDependencies(
        session_manager=SimpleNamespace(),
        registry=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        session_mutation_service=service,
    )
    mode = _RuntimeContextMode(dependencies=deps)
    overridden = deps.with_overrides(tasks=TaskService())

    assert mode.get_service("session_mutation_service") is service
    assert mode.require_service("session_mutation_service") is service
    assert overridden.session_mutation_service is service


def test_base_mode_allows_session_mutation_service_override_from_services_map():
    original = SessionMutationService()
    replacement = SessionMutationService()
    deps = ModeDependencies(
        session_manager=SimpleNamespace(),
        registry=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        session_mutation_service=original,
    )
    mode = _RuntimeContextMode(dependencies=deps)

    mode.initialize(services={"session_mutation_service": replacement})

    assert mode.mode_dependencies is not None
    assert mode.mode_dependencies.session_mutation_service is replacement
    assert mode.get_service("session_mutation_service") is replacement
    assert "session_mutation_service" not in mode._extra_services


def test_base_mode_exposes_mode_run_lifecycle_from_dependencies():
    service = SimpleNamespace(start=lambda **_kwargs: None)
    deps = ModeDependencies(
        session_manager=SimpleNamespace(),
        registry=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        mode_run_lifecycle=service,
    )
    mode = _RuntimeContextMode(dependencies=deps)
    overridden = deps.with_overrides(tasks=TaskService())

    assert mode.get_service("mode_run_lifecycle") is service
    assert mode.require_service("mode_run_lifecycle") is service
    assert mode._optional_mode_run_lifecycle() is service
    assert mode._mode_run_lifecycle() is service
    assert overridden.mode_run_lifecycle is service


def test_base_mode_allows_mode_run_lifecycle_override_from_services_map():
    original = SimpleNamespace(name="original")
    replacement = SimpleNamespace(name="replacement")
    deps = ModeDependencies(
        session_manager=SimpleNamespace(),
        registry=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        mode_run_lifecycle=original,
    )
    mode = _RuntimeContextMode(dependencies=deps)

    mode.initialize(services={"mode_run_lifecycle": replacement})

    assert mode.mode_dependencies is not None
    assert mode.mode_dependencies.mode_run_lifecycle is replacement
    assert mode.get_service("mode_run_lifecycle") is replacement
    assert "mode_run_lifecycle" not in mode._extra_services


def test_base_mode_exposes_optional_skill_runtime_from_dependencies():
    skill_runtime = SimpleNamespace(promote_run_skills=lambda **_kwargs: {"status": "ok"})
    deps = ModeDependencies(
        session_manager=SimpleNamespace(),
        registry=SimpleNamespace(),
        pipeline=SimpleNamespace(),
        skill_runtime=skill_runtime,
    )
    mode = _RuntimeContextMode(dependencies=deps)

    assert mode.get_service("skill_runtime") is skill_runtime
    assert mode._optional_skill_runtime() is skill_runtime
