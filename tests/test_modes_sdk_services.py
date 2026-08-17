import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from modes.sdk import (
    DialogService,
    MessageModel,
    ModeContext,
    ModeInputRoutingService,
    ModeRegistryService,
    StorageService,
    TaskService,
    ToolResult,
)


def test_storage_service_namespaces_by_mode_id(tmp_path):
    path = str(tmp_path / "state.json")
    s1 = StorageService(path=path, mode_id="mode_a")
    s2 = StorageService(path=path, mode_id="mode_b")

    s1.set("k", "v1")
    s2.set("k", "v2")

    assert s1.get("k") == "v1"
    assert s2.get("k") == "v2"

    # Extra namespace level stays isolated too.
    s1.set("k", "v1x", namespace="ns")
    assert s1.get("k") == "v1"
    assert s1.get("k", namespace="ns") == "v1x"


def test_storage_service_for_session_uses_cli_proxy_artifacts_dir(tmp_path):
    storage = StorageService.for_session(
        root_dir=str(tmp_path),
        session_id="sess-1",
        mode_id="mode_a",
    )
    assert storage.path == str(tmp_path / ".cli-proxy" / ".modes" / "state" / "sess-1.json")


def test_task_service_groups_by_session_and_mode():
    async def _run():
        svc = TaskService()
        seen = {"a": 0, "b": 0}

        async def _job(key: str):
            try:
                while True:
                    seen[key] += 1
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                return

        svc.create(session_id="s1", mode_id="m1", coro=_job("a"), name="A")
        svc.create(session_id="s1", mode_id="m2", coro=_job("b"), name="B")

        await asyncio.sleep(0.03)
        assert svc.list(session_id="s1", mode_id="m1") == ["A"]
        assert svc.list(session_id="s1", mode_id="m2") == ["B"]

        cancelled = await svc.cancel_all(session_id="s1", mode_id="m1", timeout_s=0.5)
        assert cancelled == 1

        # m2 task should still be running.
        await asyncio.sleep(0.02)
        assert svc.list(session_id="s1", mode_id="m2") == ["B"]
        await svc.cancel_all(session_id="s1", mode_id="m2", timeout_s=0.5)

    asyncio.run(_run())


def test_task_service_cancel_session_cancels_all_modes_for_session():
    async def _run():
        svc = TaskService()

        async def _job():
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                return

        svc.create(session_id="s1", mode_id="m1", coro=_job(), name="A")
        svc.create(session_id="s1", mode_id="m2", coro=_job(), name="B")
        svc.create(session_id="s2", mode_id="m1", coro=_job(), name="C")

        await asyncio.sleep(0.03)
        cancelled = await svc.cancel_session(session_id="s1", timeout_s=0.5)
        assert cancelled == 2

        # Tasks for another session must remain alive.
        assert svc.list(session_id="s2", mode_id="m1") == ["C"]
        await svc.cancel_session(session_id="s2", timeout_s=0.5)

    asyncio.run(_run())


def test_task_service_segregates_same_raw_session_id_by_session_uid():
    async def _run():
        svc = TaskService()

        async def _job():
            try:
                while True:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                return

        svc.create(session_id="s1", session_uid="thread:1:101", mode_id="m1", coro=_job(), name="A")
        svc.create(session_id="s1", session_uid="thread:1:202", mode_id="m1", coro=_job(), name="B")

        await asyncio.sleep(0.03)
        assert svc.list(session_uid="thread:1:101", mode_id="m1") == ["A"]
        assert svc.list(session_uid="thread:1:202", mode_id="m1") == ["B"]
        assert svc.list(session_id="s1", mode_id="m1") == []

        cancelled = await svc.cancel_session(session_uid="thread:1:101", timeout_s=0.5)
        assert cancelled == 1
        assert svc.list(session_uid="thread:1:202", mode_id="m1") == ["B"]

        await svc.cancel_session(session_uid="thread:1:202", timeout_s=0.5)

    asyncio.run(_run())


def test_dialog_service_routes_to_active_handler():
    async def _run():
        dialogs = DialogService()

        async def _on_msg(msg: MessageModel, ctx):
            assert ctx["dialog"]["step"] == "wait"
            return ToolResult.ok("got:" + msg.text)

        dialogs.start(chat_id=1, session_id="s1", mode_id="m1", on_message=_on_msg, data={"step": "wait"}, timeout_s=10)
        out = await dialogs.route_message(MessageModel(text="hi", chat_id=1), {}, session_id="s1", mode_id="m1")
        assert out.success is True
        assert out.output == "got:hi"

        dialogs.end(chat_id=1, session_id="s1", mode_id="m1")
        out2 = await dialogs.route_message(MessageModel(text="x", chat_id=1), {}, session_id="s1", mode_id="m1")
        assert out2.success is False
        assert out2.error == "no_active_dialog"

    asyncio.run(_run())


def test_mode_context_exposes_services(tmp_path):
    storage = StorageService(path=str(tmp_path / "state.json"), mode_id="m1")
    tasks = TaskService()
    dialogs = DialogService()

    ctx = ModeContext(mode_id="m1", session_id="s1", chat_id=1, storage=storage, tasks=tasks, dialogs=dialogs)

    assert ctx.require_storage() is storage
    assert ctx.require_tasks() is tasks
    assert ctx.require_dialogs() is dialogs

    with pytest.raises(RuntimeError):
        ModeContext(mode_id="m1", session_id="s1", chat_id=1).require_storage()


def test_input_routing_send_output_fallback_uses_plain_text_helper():
    async def _run():
        class EchoMode:
            async def handle_input(self, message, ctx):
                _ = message, ctx
                return ToolResult.ok("literal _*[]")

        sent = []

        async def _send_output(*_args, **_kwargs):
            raise RuntimeError("force fallback")

        async def _send_message(_context, **kwargs):
            sent.append(dict(kwargs))
            return None

        async def _cli_fallback(*_args, **_kwargs):
            raise AssertionError("active mode output must not fall back to CLI")

        router = ModeInputRoutingService(
            mode_registry=ModeRegistryService(
                SimpleNamespace(get=lambda _mode_id: EchoMode())
            ),
            send_message=_send_message,
            send_output=_send_output,
        )

        await router.route_mode_or_cli(
            bot_app=SimpleNamespace(),
            session=SimpleNamespace(id="s1", active_mode="echo"),
            text="hi",
            chat_id=123,
            context=object(),
            dest={"message_thread_id": 456},
            user_id=7,
            cli_fallback=_cli_fallback,
        )

        assert sent == [
            {
                "chat_id": 123,
                "text": "literal _*[]",
                "md2": False,
                "message_thread_id": 456,
            }
        ]

    asyncio.run(_run())


