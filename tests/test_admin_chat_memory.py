from __future__ import annotations

import json

import pytest

from modes.admin.chat_memory import (
    ChatMemory,
    ChatMemoryError,
    ChatPendingStore,
    memory_json_path,
    memory_md_path,
)


def test_paths_under_admin_chat_dir(tmp_path):
    assert memory_json_path(str(tmp_path)).as_posix().endswith("/.cli-proxy/.admin/chat/memory.json")
    assert memory_md_path(str(tmp_path)).as_posix().endswith("/.cli-proxy/.admin/chat/MEMORY.md")


def test_append_persists_and_reloads(tmp_path):
    mem = ChatMemory(str(tmp_path))
    mem.append(role="user", text="hi")
    mem.append(role="assistant", text="hello", intent_type="answer")

    reloaded = ChatMemory(str(tmp_path)).load_messages()
    assert [m.role for m in reloaded] == ["user", "assistant"]
    assert reloaded[1].intent_type == "answer"
    assert reloaded[0].text == "hi"


def test_ring_buffer_cap(tmp_path):
    mem = ChatMemory(str(tmp_path), max_messages=5)
    for i in range(12):
        mem.append(role="user", text=f"msg-{i}")
    messages = mem.load_messages()
    assert len(messages) == 5
    assert [m.text for m in messages] == [f"msg-{i}" for i in range(7, 12)]


def test_append_rejects_empty_and_bad_role(tmp_path):
    mem = ChatMemory(str(tmp_path))
    with pytest.raises(ChatMemoryError):
        mem.append(role="user", text="   ")
    with pytest.raises(ChatMemoryError):
        mem.append(role="bogus", text="ok")


def test_memory_md_append_and_read(tmp_path):
    mem = ChatMemory(str(tmp_path))
    mem.append_memory_md("nginx restarts require maintenance window", source="chat")
    mem.append_memory_md("postgres backups run at 03:00 UTC", source="chat")
    text = mem.read_memory_md()
    assert "nginx restarts" in text
    assert "postgres backups" in text


def test_load_messages_handles_corrupted_json(tmp_path):
    ChatMemory(str(tmp_path))
    memory_json_path(str(tmp_path)).write_text("{not json", encoding="utf-8")
    assert ChatMemory(str(tmp_path)).load_messages() == []


def test_pending_store_save_get_pop(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-abc", {"intent": {"type": "propose_action", "action_id": "foo"}})
    found = store.get("chat-abc")
    assert found is not None
    assert found["intent"]["action_id"] == "foo"
    popped = store.pop("chat-abc")
    assert popped is not None
    assert store.get("chat-abc") is None


def test_pending_store_rejects_bad_id(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    with pytest.raises(ChatMemoryError):
        store.save("../evil", {"intent": {}})


def test_pending_store_list_pending(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-1", {"intent": {"type": "answer", "text": "x"}})
    store.save("chat-2", {"intent": {"type": "answer", "text": "y"}})
    assert sorted(store.list_ids()) == ["chat-1", "chat-2"]
    assert len(store.list_pending()) == 2


def test_memory_json_persisted_payload_shape(tmp_path):
    mem = ChatMemory(str(tmp_path))
    mem.append(role="user", text="hello")
    data = json.loads(memory_json_path(str(tmp_path)).read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert isinstance(data["messages"], list)
    assert data["messages"][0]["text"] == "hello"
