from __future__ import annotations

import pytest

from sessions.queue_item import SessionQueueItem, normalize_queue_item


def test_normalize_queue_item_from_dict_payload_copies_dest() -> None:
    raw_dest = {"kind": "telegram", "chat_id": 10}

    item = normalize_queue_item(
        {
            "text": "next prompt",
            "dest": raw_dest,
            "created_at": 123,
        },
        fallback_dest={"kind": "desktop"},
    )

    assert item == SessionQueueItem(
        text="next prompt",
        dest={"kind": "telegram", "chat_id": 10},
        created_at=123.0,
    )
    assert item.dest is not raw_dest
    raw_dest["chat_id"] = 20
    assert item.dest["chat_id"] == 10


def test_normalize_queue_item_from_dataclass_payload_copies_dest() -> None:
    raw_dest = {"kind": "desktop", "session_uid": "desktop:abc"}
    raw = SessionQueueItem(text="queued", dest=raw_dest, created_at=4.5)

    item = normalize_queue_item(raw, fallback_dest={"kind": "telegram"})

    assert item == raw
    assert item is not raw
    assert item.dest is not raw_dest
    raw_dest["session_uid"] = "changed"
    assert item.dest["session_uid"] == "desktop:abc"


def test_normalize_queue_item_from_legacy_string_requires_fallback_dest() -> None:
    fallback_dest = {"kind": "telegram", "chat_id": 42}

    item = normalize_queue_item("legacy prompt", fallback_dest)

    assert item == SessionQueueItem(text="legacy prompt", dest={"kind": "telegram", "chat_id": 42})
    assert item.dest is not fallback_dest
    fallback_dest["chat_id"] = 100
    assert item.dest["chat_id"] == 42
    with pytest.raises(ValueError, match="fallback_dest"):
        normalize_queue_item("legacy prompt")


def test_normalize_queue_item_rejects_missing_text() -> None:
    with pytest.raises(ValueError, match="text"):
        normalize_queue_item({"dest": {"kind": "telegram"}}, fallback_dest={"kind": "desktop"})


def test_normalize_queue_item_uses_fallback_dest_copy_for_dict_without_dest() -> None:
    fallback_dest = {"kind": "telegram", "chat_id": 7}

    item = normalize_queue_item({"text": "queued"}, fallback_dest=fallback_dest)

    assert item == SessionQueueItem(text="queued", dest={"kind": "telegram", "chat_id": 7})
    assert item.dest is not fallback_dest
    fallback_dest["chat_id"] = 8
    assert item.dest["chat_id"] == 7
