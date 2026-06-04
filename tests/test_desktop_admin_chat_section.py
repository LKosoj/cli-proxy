from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from desktop.widgets.admin_chat_section import AdminChatSection
from i18n import t


def _make_facade(
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
    pending: Optional[List[Dict[str, Any]]] = None,
    memory_text: str = "",
    post_result: Optional[Dict[str, Any]] = None,
    approve_result: Optional[Dict[str, Any]] = None,
    reject_result: Optional[Dict[str, Any]] = None,
    save_result: Optional[Dict[str, Any]] = None,
) -> SimpleNamespace:
    calls: Dict[str, Any] = {
        "post_calls": [],
        "approve_calls": [],
        "reject_calls": [],
        "save_calls": [],
    }

    def get_messages(_uid):
        return {"ok": True, "messages": list(messages or [])}

    def get_pending(_uid):
        return {"ok": True, "items": list(pending or [])}

    def get_memory(_uid):
        return {"ok": True, "text": memory_text}

    def save_memory(_uid, *, text):
        calls["save_calls"].append({"uid": _uid, "text": text})
        return save_result or {"ok": True}

    def reject(_uid, *, approval_id):
        calls["reject_calls"].append({"uid": _uid, "approval_id": approval_id})
        return reject_result or {"ok": True}

    async def post(_uid, *, text):
        calls["post_calls"].append({"uid": _uid, "text": text})
        return post_result or {"ok": True, "reply_text": "hi", "intent": {"type": "answer"}}

    async def approve(_uid, *, approval_id):
        calls["approve_calls"].append({"uid": _uid, "approval_id": approval_id})
        return approve_result or {
            "ok": True,
            "exit_code": 0,
            "target_kind": "local",
            "duration_ms": 5,
        }

    facade = SimpleNamespace(
        get_admin_chat_messages=get_messages,
        get_admin_chat_pending=get_pending,
        get_admin_chat_memory_md=get_memory,
        save_admin_chat_memory_md=save_memory,
        reject_admin_chat_pending=reject,
        post_admin_chat_message=post,
        approve_admin_chat_pending=approve,
        logger=logging.getLogger("test_admin_chat_section"),
        ui_language="ru",
    )
    facade._calls = calls
    return facade


def test_renders_messages_and_pending(qtbot) -> None:
    facade = _make_facade(
        messages=[
            {"role": "user", "text": "hi", "ts": "2025-01-01T00:00:00Z"},
            {"role": "assistant", "text": "hello!", "ts": "2025-01-01T00:00:01Z"},
        ],
        pending=[
            {
                "approval_id": "chat-x",
                "intent": {"type": "propose_action", "action_id": "check_disk", "target": "local"},
            }
        ],
        memory_text="note",
    )
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section._reload_messages()
    section._reload_pending()
    section._reload_memory()

    text = section.messages_view.toPlainText()
    assert "user: hi" in text
    assert "assistant: hello!" in text
    assert section.pending_list.count() == 1
    assert section.pending_label.text() == t("desktop.adminchat.pending_label", "ru", count=1)
    assert section.memory_edit.toPlainText() == "note"


def test_polling_triggers_only_on_counter_change(qtbot) -> None:
    facade = _make_facade(
        messages=[{"role": "user", "text": "m1", "ts": "t1"}],
        pending=[],
    )
    counters = {"messages_count": 0, "pending_count": 0, "last_message_ts": ""}
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {"chat": counters},
    )
    qtbot.addWidget(section)

    section._poll_tick(force=True)
    assert section.messages_view.toPlainText() != ""
    section.messages_view.clear()

    # Stable counters → no reload
    section._poll_tick()
    assert section.messages_view.toPlainText() == ""

    # Counter bumps → reload
    counters["messages_count"] = 1
    counters["last_message_ts"] = "t1"
    section._poll_tick()
    assert "user: m1" in section.messages_view.toPlainText()


@pytest.mark.asyncio
async def test_send_posts_message_and_refreshes(qtbot) -> None:
    facade = _make_facade(
        messages=[{"role": "user", "text": "hello", "ts": "t1"}],
        post_result={"ok": True, "reply_text": "hi", "intent": {"type": "answer"}},
    )
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section.input_edit.setText("hello")

    section._on_send_clicked()
    # Drain the async task
    tasks = list(section._background_tasks)
    for task in tasks:
        await task

    assert facade._calls["post_calls"] == [{"uid": "sess", "text": "hello"}]
    assert "user: hello" in section.messages_view.toPlainText()


def test_reject_calls_facade(qtbot) -> None:
    facade = _make_facade(
        pending=[{"approval_id": "chat-r", "intent": {"type": "propose_action"}}],
    )
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section._reload_pending()
    section.pending_list.setCurrentRow(0)

    section._on_reject_clicked()

    assert facade._calls["reject_calls"] == [{"uid": "sess", "approval_id": "chat-r"}]


def test_save_memory_calls_facade(qtbot) -> None:
    facade = _make_facade()
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section.memory_edit.setPlainText("new note")

    section._on_save_memory_clicked()

    assert facade._calls["save_calls"] == [{"uid": "sess", "text": "new note"}]
    assert section._memory_dirty is False


def test_stop_and_start_toggle_timer(qtbot) -> None:
    facade = _make_facade()
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: None,
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    assert not section._poll_timer.isActive()
    section.start()
    assert section._poll_timer.isActive()
    section.stop()
    assert not section._poll_timer.isActive()


def test_autopilot_messages_render_markers(qtbot) -> None:
    facade = _make_facade(
        messages=[
            {
                "role": "system",
                "text": "autopilot executed: propose_action",
                "ts": "t1",
                "intent_type": "intent_autopilot_executed",
            },
            {
                "role": "system",
                "text": "autopilot blocked: propose_action (action 'x' not in auto_exec_actions)",
                "ts": "t2",
                "intent_type": "intent_autopilot_blocked",
            },
        ],
    )
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section._reload_messages()
    text = section.messages_view.toPlainText()
    assert "[autopilot ✓]" in text
    assert "[autopilot ⚠]" in text


def test_pending_row_shows_autopilot_blocked_reason(qtbot) -> None:
    facade = _make_facade(
        pending=[
            {
                "approval_id": "chat-p",
                "intent": {"type": "propose_action", "action_id": "x", "target": "local"},
                "autopilot_blocked": "action 'x' not in auto_exec_actions",
            }
        ],
    )
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section._reload_pending()
    assert section.pending_list.count() == 1
    label = section.pending_list.item(0).text()
    assert "auto-exec blocked" in label
    assert "action 'x' not in auto_exec_actions" in label


def test_handle_send_result_auto_exec_status(qtbot) -> None:
    facade = _make_facade()
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section._handle_send_result({
        "ok": True,
        "auto_exec": True,
        "exec_result": {"target_kind": "local", "exit_code": 0},
        "intent": {"type": "propose_action"},
    })
    assert t("desktop.adminchat.status_autopilot_executed", "ru", kind="local") in section.status_label.text()
    assert "exit=0" in section.status_label.text()


def test_handle_send_result_blocked_status(qtbot) -> None:
    facade = _make_facade()
    section = AdminChatSection(
        facade,
        get_session_uid=lambda: "sess",
        get_status_payload=lambda: {},
    )
    qtbot.addWidget(section)
    section._handle_send_result({
        "ok": True,
        "auto_exec": False,
        "autopilot_blocked": "action 'x' not in auto_exec_actions",
        "intent": {"type": "propose_action"},
    })
    assert t(
        "desktop.adminchat.status_autopilot_blocked", "ru",
        reason="action 'x' not in auto_exec_actions",
    ) in section.status_label.text()
    assert "action 'x'" in section.status_label.text()
