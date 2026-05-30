from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from modes.sdk.runtime.manage_tasks_progress import ManageTasksProgressBridge


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[dict] = []
        self._next_message_id = 10

    async def _send_message(self, context, **kwargs):
        self.sent.append(dict(kwargs))
        self._next_message_id += 1
        return SimpleNamespace(message_id=self._next_message_id)

    async def _edit_message(self, context, **kwargs):
        self.edited.append(dict(kwargs))
        return True

    async def _delete_message(self, context, **kwargs):
        self.deleted.append(dict(kwargs))
        return True


class _FakeDesktopBot:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict]] = []

    def notify(self, event: str, **payload) -> None:
        self.notifications.append((event, dict(payload)))


def _result(tasks):
    return {
        "success": True,
        "manage_tasks": {
            "changed": True,
            "action": "update",
            "tasks": tasks,
            "progress": {"total": len(tasks), "closed": 0, "open": len(tasks)},
        },
    }


@pytest.mark.asyncio
async def test_manage_tasks_progress_uses_events_for_telegram_edit_and_delete() -> None:
    bot = _FakeTelegramBot()
    bridge = ManageTasksProgressBridge()
    ctx = {
        "bot": bot,
        "context": object(),
        "chat_id": 123,
        "manage_tasks_scope_key": "s1:manage_tasks:run-1",
    }

    await bridge.sync(
        tool_name="manage_tasks",
        result=_result([{"id": "t1", "content": "Inspect", "status": "pending"}]),
        ctx=ctx,
    )
    await bridge.sync(
        tool_name="manage_tasks",
        result=_result([{"id": "t1", "content": "Inspect", "status": "in_progress"}]),
        ctx=ctx,
    )
    await bridge.sync(
        tool_name="manage_tasks",
        result=_result([{"id": "t1", "content": "Inspect", "status": "completed"}]),
        ctx=ctx,
    )

    assert len(bot.sent) == 1
    assert "План выполнения" in bot.sent[0]["text"]
    assert len(bot.edited) == 1
    assert bot.edited[0]["message_id"] == 11
    assert bot.deleted == [{"chat_id": 123, "message_id": 11}]


@pytest.mark.asyncio
async def test_manage_tasks_progress_uses_events_for_desktop_notifications() -> None:
    bot = _FakeDesktopBot()
    bridge = ManageTasksProgressBridge()
    ctx = {
        "bot": bot,
        "context": object(),
        "chat_id": "desktop:s1",
        "manage_tasks_scope_key": "desktop:s1:manage_tasks:run-1",
    }

    await bridge.sync(
        tool_name="manage_tasks",
        result=_result([{"id": "t1", "content": "Inspect", "status": "pending"}]),
        ctx=ctx,
    )
    await bridge.sync(
        tool_name="manage_tasks",
        result=_result([{"id": "t1", "content": "Inspect", "status": "completed"}]),
        ctx=ctx,
    )

    assert bot.notifications[0][0] == "ui:manage_tasks_progress"
    assert bot.notifications[0][1]["session_id"] == "desktop:s1"
    assert bot.notifications[0][1]["tasks"] == [{"id": "t1", "content": "Inspect", "status": "pending"}]
    assert bot.notifications[0][1]["progress"] == {"total": 1, "closed": 0, "open": 1}
    assert bot.notifications[1] == ("ui:manage_tasks_progress_clear", {"session_id": "desktop:s1"})


@pytest.mark.asyncio
async def test_manage_tasks_no_crosstalk_parallel_sessions() -> None:
    """Один bridge, два ctx с разными chat_id и разными bot — нет cross-talk между сессиями."""
    bot_a = _FakeTelegramBot()
    bot_b = _FakeTelegramBot()
    bridge = ManageTasksProgressBridge()

    ctx_a = {
        "bot": bot_a,
        "context": object(),
        "chat_id": 111,
        "manage_tasks_scope_key": "s_a:manage_tasks:run-1",
    }
    ctx_b = {
        "bot": bot_b,
        "context": object(),
        "chat_id": 222,
        "manage_tasks_scope_key": "s_b:manage_tasks:run-1",
    }

    result_a = _result([{"id": "ta", "content": "Task A", "status": "pending"}])
    result_b = _result([{"id": "tb", "content": "Task B", "status": "pending"}])

    await asyncio.gather(
        bridge.sync(tool_name="manage_tasks", result=result_a, ctx=ctx_a),
        bridge.sync(tool_name="manage_tasks", result=result_b, ctx=ctx_b),
    )

    # Каждый бот получил ровно одно сообщение и только для своего chat_id
    assert len(bot_a.sent) == 1
    assert bot_a.sent[0]["chat_id"] == 111
    assert len(bot_b.sent) == 1
    assert bot_b.sent[0]["chat_id"] == 222

    # Боты не получали сообщений друг друга
    for msg in bot_a.sent:
        assert msg["chat_id"] != 222
    for msg in bot_b.sent:
        assert msg["chat_id"] != 111


@pytest.mark.asyncio
async def test_manage_tasks_renders_without_bus() -> None:
    """Бот без атрибута system_event_bus — рендер всё равно происходит."""
    bot = _FakeTelegramBot()
    # Убеждаемся, что system_event_bus отсутствует
    assert not hasattr(bot, "system_event_bus")

    bridge = ManageTasksProgressBridge()
    ctx = {
        "bot": bot,
        "context": object(),
        "chat_id": 999,
        "manage_tasks_scope_key": "s_nobus:manage_tasks:run-1",
    }

    await bridge.sync(
        tool_name="manage_tasks",
        result=_result([{"id": "t1", "content": "No bus task", "status": "pending"}]),
        ctx=ctx,
    )

    assert len(bot.sent) == 1
    assert "Plan" in bot.sent[0]["text"] or "План" in bot.sent[0]["text"]
    assert bot.sent[0]["chat_id"] == 999
