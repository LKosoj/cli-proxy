from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

_LOG = logging.getLogger(__name__)

DEFAULT_MAX_MESSAGES = 50
CHAT_DIR_NAME = "chat"
MEMORY_JSON_FILE = "memory.json"
MEMORY_MD_FILE = "MEMORY.md"


@dataclass
class ChatMessage:
    role: str
    text: str
    ts: str
    intent_type: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
        }
        if self.intent_type:
            payload["intent_type"] = self.intent_type
        if self.meta:
            payload["meta"] = dict(self.meta)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        meta_raw = data.get("meta") if isinstance(data, Mapping) else None
        meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
        return cls(
            role=str(data.get("role") or ""),
            text=str(data.get("text") or ""),
            ts=str(data.get("ts") or ""),
            intent_type=(str(data.get("intent_type")) if data.get("intent_type") else None),
            meta=meta,
        )


def chat_dir(workdir: str) -> Path:
    return Path(workdir) / ".cli-proxy" / ".admin" / CHAT_DIR_NAME


def memory_json_path(workdir: str) -> Path:
    return chat_dir(workdir) / MEMORY_JSON_FILE


def memory_md_path(workdir: str) -> Path:
    return chat_dir(workdir) / MEMORY_MD_FILE


class ChatMemoryError(RuntimeError):
    pass


class ChatMemory:
    """Per-session admin chat memory: rolling message history + free-form MEMORY.md."""

    def __init__(
        self,
        workdir: str,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.workdir = str(workdir or "")
        if not self.workdir:
            raise ChatMemoryError("ChatMemory requires non-empty workdir")
        self.max_messages = max(1, int(max_messages))
        self._dir = chat_dir(self.workdir)
        self._memory_json = memory_json_path(self.workdir)
        self._memory_md = memory_md_path(self.workdir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def memory_json_path(self) -> Path:
        return self._memory_json

    @property
    def memory_md_path(self) -> Path:
        return self._memory_md

    def load_messages(self) -> List[ChatMessage]:
        if not self._memory_json.is_file():
            return []
        try:
            raw = self._memory_json.read_text(encoding="utf-8")
        except OSError as exc:
            raise ChatMemoryError(f"unable to read {self._memory_json}: {exc}") from exc
        if not raw.strip():
            return []
        try:
            data = loads_safe(raw, strict_first=True)
        except json.JSONDecodeError as exc:
            _LOG.warning("admin chat memory: corrupted json at %s: %s", self._memory_json, exc)
            return []
        messages_raw = data.get("messages") if isinstance(data, Mapping) else None
        if not isinstance(messages_raw, list):
            return []
        messages: List[ChatMessage] = []
        for item in messages_raw:
            if not isinstance(item, Mapping):
                continue
            try:
                messages.append(ChatMessage.from_dict(item))
            except Exception:  # noqa: BLE001
                continue
        return messages

    def append(
        self,
        *,
        role: str,
        text: str,
        intent_type: Optional[str] = None,
        meta: Optional[Mapping[str, Any]] = None,
        ts: Optional[str] = None,
    ) -> ChatMessage:
        role_clean = str(role or "").strip().lower()
        if role_clean not in {"user", "assistant", "system", "exec"}:
            raise ChatMemoryError(f"invalid role: {role!r}")
        body = str(text or "").strip()
        if not body:
            raise ChatMemoryError("chat message text is empty")
        message = ChatMessage(
            role=role_clean,
            text=body,
            ts=str(ts or _now_iso()),
            intent_type=(str(intent_type) if intent_type else None),
            meta=dict(meta or {}),
        )
        existing = self.load_messages()
        existing.append(message)
        if len(existing) > self.max_messages:
            existing = existing[-self.max_messages:]
        self._persist_messages(existing)
        return message

    def tail(self, n: int) -> List[ChatMessage]:
        if n <= 0:
            return []
        messages = self.load_messages()
        return messages[-n:]

    def clear(self) -> None:
        self._persist_messages([])

    def _persist_messages(self, messages: List[ChatMessage]) -> None:
        payload = {
            "version": 1,
            "max_messages": self.max_messages,
            "updated_at": _now_iso(),
            "messages": [m.as_dict() for m in messages],
        }
        self._memory_json.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._memory_json.with_suffix(self._memory_json.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._memory_json)

    # --- MEMORY.md ---

    def read_memory_md(self) -> str:
        if not self._memory_md.is_file():
            return ""
        try:
            return self._memory_md.read_text(encoding="utf-8")
        except OSError as exc:
            raise ChatMemoryError(f"unable to read {self._memory_md}: {exc}") from exc

    def append_memory_md(self, text: str, *, source: str = "assistant") -> str:
        body = str(text or "").strip()
        if not body:
            raise ChatMemoryError("memory.md append text is empty")
        header = f"## {_now_iso()} [{str(source or 'assistant').strip() or 'assistant'}]"
        block = f"{header}\n{body}\n\n"
        existing = self.read_memory_md()
        new_text = existing + block if existing else block
        self._memory_md.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._memory_md.with_suffix(self._memory_md.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, self._memory_md)
        return block

    def overwrite_memory_md(self, text: str) -> None:
        body = str(text or "")
        self._memory_md.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._memory_md.with_suffix(self._memory_md.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self._memory_md)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ChatPendingStore:
    """Per-session storage for pending chat approvals (one file per approval)."""

    def __init__(self, workdir: str) -> None:
        self.workdir = str(workdir or "")
        if not self.workdir:
            raise ChatMemoryError("ChatPendingStore requires non-empty workdir")
        self._dir = chat_dir(self.workdir) / "pending"
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def dir_path(self) -> Path:
        return self._dir

    def _path(self, approval_id: str) -> Path:
        safe = str(approval_id or "").strip()
        if not safe or "/" in safe or "\\" in safe or safe.startswith("."):
            raise ChatMemoryError(f"invalid approval_id: {approval_id!r}")
        return self._dir / f"{safe}.json"

    def save(self, approval_id: str, payload: Mapping[str, Any]) -> Path:
        path = self._path(approval_id)
        data = dict(payload or {})
        data.setdefault("approval_id", approval_id)
        data.setdefault("created_at", time.time())
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        try:
            path = self._path(approval_id)
        except ChatMemoryError:
            return None
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            data = loads_safe(raw, strict_first=True)
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("admin chat pending: unable to read %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        return data

    def pop(self, approval_id: str) -> Optional[Dict[str, Any]]:
        data = self.get(approval_id)
        if data is None:
            return None
        try:
            self._path(approval_id).unlink()
        except OSError:
            pass
        return data

    def list_ids(self) -> List[str]:
        if not self._dir.is_dir():
            return []
        ids: List[str] = []
        for item in sorted(self._dir.iterdir()):
            if item.suffix.lower() == ".json":
                ids.append(item.stem)
        return ids

    def list_pending(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for pid in self.list_ids():
            data = self.get(pid)
            if data is not None:
                items.append(data)
        return items


__all__ = [
    "CHAT_DIR_NAME",
    "ChatMemory",
    "ChatMemoryError",
    "ChatMessage",
    "ChatPendingStore",
    "DEFAULT_MAX_MESSAGES",
    "MEMORY_JSON_FILE",
    "MEMORY_MD_FILE",
    "chat_dir",
    "memory_json_path",
    "memory_md_path",
]
