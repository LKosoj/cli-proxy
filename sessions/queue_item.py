from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_QUEUE_ITEM_METADATA_FIELDS = ("image_path", "image_paths", "attachments")


@dataclass(frozen=True)
class SessionQueueItem:
    text: str
    dest: dict[str, Any]
    created_at: float | None = None


def _copy_dest(dest: Mapping[str, Any] | None, *, field_name: str) -> dict[str, Any]:
    if dest is None:
        raise ValueError(f"{field_name} is required")
    if not isinstance(dest, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return dict(dest)


def _normalize_created_at(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("queue item created_at must be a number or None")
    return float(value)


def normalize_queue_item(
    raw: Any,
    fallback_dest: Mapping[str, Any] | None = None,
) -> SessionQueueItem:
    if isinstance(raw, SessionQueueItem):
        return SessionQueueItem(
            text=raw.text,
            dest=dict(raw.dest),
            created_at=raw.created_at,
        )

    if isinstance(raw, str):
        return SessionQueueItem(
            text=raw,
            dest=_copy_dest(fallback_dest, field_name="fallback_dest"),
        )

    if isinstance(raw, Mapping):
        if "text" not in raw:
            raise ValueError("queue item text is required")
        text = raw["text"]
        if not isinstance(text, str):
            raise TypeError("queue item text must be a string")

        raw_dest = raw.get("dest")
        dest = _copy_dest(
            raw_dest if raw_dest is not None else fallback_dest,
            field_name="queue item dest",
        )
        return SessionQueueItem(
            text=text,
            dest=dest,
            created_at=_normalize_created_at(raw.get("created_at")),
        )

    raise TypeError("queue item must be a SessionQueueItem, mapping, or legacy string")


def _copy_metadata_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return list(value)
    return value


def _queue_item_payload(
    item: SessionQueueItem,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": item.text,
        "dest": dict(item.dest),
    }
    if item.created_at is not None:
        payload["created_at"] = item.created_at
    if isinstance(metadata, Mapping):
        for field in _QUEUE_ITEM_METADATA_FIELDS:
            if field in metadata and metadata[field] is not None:
                payload[field] = _copy_metadata_value(metadata[field])
    return payload


def normalize_queue_item_payload(
    raw: Any,
    fallback_dest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = raw if isinstance(raw, Mapping) else None
    return _queue_item_payload(
        normalize_queue_item(raw, fallback_dest=fallback_dest),
        metadata=metadata,
    )


def append_session_queue_item(
    session: Any,
    raw: Any,
    fallback_dest: Mapping[str, Any] | None = None,
) -> bool:
    queue = getattr(session, "queue", None)
    if queue is None or not hasattr(queue, "append"):
        return False
    queue.append(normalize_queue_item_payload(raw, fallback_dest=fallback_dest))
    return True


__all__ = (
    "SessionQueueItem",
    "append_session_queue_item",
    "normalize_queue_item",
    "normalize_queue_item_payload",
)
