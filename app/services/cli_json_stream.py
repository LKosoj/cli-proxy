from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from modes.sdk.runtime.json_normalizer import loads_safe

logger = logging.getLogger(__name__)

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CLAUDE_TASK_NOTIFICATION_AGENT_RE = re.compile(r'background agent "([^"]+)"')


def _safe_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return "unknown"
    return _SAFE_TOKEN_RE.sub("_", token)


def cli_json_stream_archive_enabled(config: Any) -> bool:
    defaults = getattr(config, "defaults", None)
    return bool(getattr(defaults, "cli_json_stream_archive_enabled", False))


def _truncate(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _short_path(path: Any, *, keep_parts: int = 3) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return text
    if len(parts) <= keep_parts:
        return "/".join(parts)
    return "/".join(parts[-keep_parts:])


@dataclass(frozen=True)
class CliJsonStreamEvent:
    kind: str
    cli_name: str
    payload: dict[str, Any]
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    text: Optional[str] = None
    is_terminal: bool = False
    ts: float = field(default_factory=time.time)

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cli_name": self.cli_name,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "text": self.text,
            "is_terminal": self.is_terminal,
            "ts": self.ts,
            "payload": dict(self.payload or {}),
        }


class BaseCliJsonStreamAdapter:
    cli_name = ""

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        raise NotImplementedError

    def final_output_text(self) -> str:
        return ""

    @property
    def session_id(self) -> Optional[str]:
        return None

    @property
    def completed(self) -> bool:
        return False


class CodexJsonStreamAdapter(BaseCliJsonStreamAdapter):
    cli_name = "codex"

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._final_output_text = ""
        self._completed = False

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def completed(self) -> bool:
        return self._completed

    def final_output_text(self) -> str:
        return self._final_output_text

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            raise ValueError(f"CLI JSON stream line must decode to object, got {type(payload)!r}")

        event_type = str(payload.get("type") or "").strip()
        if not event_type:
            raise ValueError("CLI JSON stream event is missing type")

        if event_type == "thread.started":
            session_id = str(payload.get("thread_id") or "").strip() or None
            self._session_id = session_id
            return [
                CliJsonStreamEvent(
                    kind="session_started",
                    cli_name=self.cli_name,
                    session_id=session_id,
                    payload=payload,
                )
            ]

        if event_type == "turn.started":
            return [
                CliJsonStreamEvent(
                    kind="progress",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    text="Codex turn started",
                    payload=payload,
                )
            ]

        if event_type == "item.completed":
            item = payload.get("item")
            if not isinstance(item, dict):
                return []
            item_type = str(item.get("type") or "").strip()
            if item_type == "agent_message":
                text = str(item.get("text") or "")
                self._final_output_text = text
                return [
                    CliJsonStreamEvent(
                        kind="assistant_text",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        turn_id=str(item.get("id") or "").strip() or None,
                        text=text,
                        payload=payload,
                    )
                ]
            summary = _summarize_codex_item(item)
            return [
                CliJsonStreamEvent(
                    kind="tool_event",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    turn_id=str(item.get("id") or "").strip() or None,
                    text=summary,
                    payload=payload,
                )
            ]

        if event_type == "item.started":
            item = payload.get("item")
            if not isinstance(item, dict):
                return []
            return [
                CliJsonStreamEvent(
                    kind="tool_event",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    turn_id=str(item.get("id") or "").strip() or None,
                    text=_summarize_codex_item(item),
                    payload=payload,
                )
            ]

        if event_type == "turn.completed":
            self._completed = True
            return [
                CliJsonStreamEvent(
                    kind="completed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    payload=payload,
                )
            ]

        if event_type.endswith(".failed") or event_type == "error":
            return [
                CliJsonStreamEvent(
                    kind="failed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=str(payload.get("message") or "") or None,
                    payload=payload,
                )
            ]

        return [
            CliJsonStreamEvent(
                kind="raw_event",
                cli_name=self.cli_name,
                session_id=self._session_id,
                payload=payload,
            )
        ]


def _summarize_codex_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "").strip()
    if not item_type:
        return "codex item"
    command = str(
        item.get("command")
        or item.get("command_line")
        or item.get("title")
        or ""
    ).strip()
    if command:
        return f"{item_type}: {command}"
    return item_type.replace("_", " ")


def _coerce_message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _coerce_message_text(item)
            if text:
                parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            text = _coerce_message_text(value.get(key))
            if text:
                return text
        return ""
    return ""


def _tool_args_preview(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    file_path = str(
        payload.get("absolute_path")
        or payload.get("file_path")
        or payload.get("filePath")
        or payload.get("path")
        or payload.get("dir_path")
        or ""
    ).strip()
    if file_path:
        return _truncate(_short_path(file_path, keep_parts=1), 120)
    pattern = str(payload.get("pattern") or "").strip()
    if pattern:
        return _truncate(pattern, 120)
    command = str(payload.get("command") or "").strip()
    if command:
        return _truncate(command, 120)
    description = str(payload.get("description") or "").strip()
    if description:
        return _truncate(description, 120)
    return ""


def _tool_label(tool_name: Any, payload: Any) -> str:
    name = str(tool_name or "").strip() or "tool"
    preview = _tool_args_preview(payload)
    if preview:
        return f"{name}: {preview}"
    return name


def _kimi_tool_arguments(value: Any) -> Any:
    """Kimi отдаёт аргументы инструмента JSON-строкой, как в OpenAI tool_calls."""

    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return loads_safe(text)
    except Exception:
        return None


def _tool_result_detail(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("output", "resultDisplay", "result", "description", "message", "content"):
            text = _coerce_message_text(payload.get(key))
            if text:
                return _truncate(text, 180)
    text = _coerce_message_text(payload)
    if text:
        return _truncate(text, 180)
    return ""


def _tool_result_text(tool_label: str, *, detail: str, is_error: bool) -> str:
    label = str(tool_label or "").strip() or "tool"
    if detail:
        if is_error:
            return f"{label} failed: {detail}"
        return f"{label} result: {detail}"
    if is_error:
        return f"{label} failed"
    return f"{label} completed"


def _claude_task_notification_text(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip().lower().replace("_", " ")
    status = status or "updated"
    summary = str(payload.get("summary") or "").strip()
    match = _CLAUDE_TASK_NOTIFICATION_AGENT_RE.search(summary)
    agent_name = match.group(1).strip() if match else ""
    if agent_name:
        return _truncate(f"Claude background task {status}: {agent_name}", 180)
    task_id = str(payload.get("task_id") or "").strip()
    if task_id:
        return _truncate(f"Claude background task {status}: {task_id}", 180)
    return f"Claude background task {status}"


class GeminiJsonStreamAdapter(BaseCliJsonStreamAdapter):
    cli_name = "gemini"

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._final_output_text = ""
        self._completed = False
        self._tool_labels: dict[str, str] = {}

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def completed(self) -> bool:
        return self._completed

    def final_output_text(self) -> str:
        return self._final_output_text

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            raise ValueError(f"CLI JSON stream line must decode to object, got {type(payload)!r}")

        event_type = str(payload.get("type") or "").strip().lower()
        if not event_type:
            raise ValueError("CLI JSON stream event is missing type")

        if event_type == "init":
            session_id = str(payload.get("session_id") or "").strip() or None
            self._session_id = session_id
            return [
                CliJsonStreamEvent(
                    kind="session_started",
                    cli_name=self.cli_name,
                    session_id=session_id,
                    payload=payload,
                )
            ]

        if event_type == "message":
            role = str(payload.get("role") or "").strip().lower()
            if role != "assistant":
                return []
            text = _coerce_message_text(payload.get("content"))
            if not text:
                return []
            if payload.get("delta") is True:
                self._final_output_text += text
                event_text = text
            else:
                self._final_output_text = text
                event_text = text
            return [
                CliJsonStreamEvent(
                    kind="assistant_text",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    text=event_text,
                    payload=payload,
                )
            ]

        if event_type == "tool_use":
            tool_id = str(payload.get("tool_id") or payload.get("toolId") or "").strip()
            tool_label = _tool_label(
                payload.get("tool_name") or payload.get("name"),
                payload.get("parameters"),
            )
            if tool_id:
                self._tool_labels[tool_id] = tool_label
            return [
                CliJsonStreamEvent(
                    kind="tool_event",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    turn_id=tool_id or None,
                    text=tool_label,
                    payload=payload,
                )
            ]

        if event_type == "tool_result":
            tool_id = str(payload.get("tool_id") or payload.get("toolId") or "").strip()
            status = str(payload.get("status") or "").strip().lower()
            is_error = status in {"error", "failed", "failure"}
            tool_label = self._tool_labels.get(tool_id) or (tool_id or "tool")
            return [
                CliJsonStreamEvent(
                    kind="tool_event",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    turn_id=tool_id or None,
                    text=_tool_result_text(
                        tool_label,
                        detail=_tool_result_detail(payload),
                        is_error=is_error,
                    ),
                    payload=payload,
                )
            ]

        if event_type == "result":
            status = str(payload.get("status") or "").strip().lower()
            terminal_kind = "completed" if status in {"success", "completed", "done"} else "failed"
            if terminal_kind == "completed":
                self._completed = True
            return [
                CliJsonStreamEvent(
                    kind=terminal_kind,
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=None if terminal_kind == "completed" else (status or None),
                    payload=payload,
                )
            ]

        if event_type.endswith(".failed") or event_type == "error":
            return [
                CliJsonStreamEvent(
                    kind="failed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=str(payload.get("message") or "") or None,
                    payload=payload,
                )
            ]

        return [
            CliJsonStreamEvent(
                kind="raw_event",
                cli_name=self.cli_name,
                session_id=self._session_id,
                payload=payload,
            )
        ]


class QwenJsonStreamAdapter(BaseCliJsonStreamAdapter):
    cli_name = "qwen"

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._final_output_text = ""
        self._completed = False
        self._tool_labels: dict[str, str] = {}

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def completed(self) -> bool:
        return self._completed

    def final_output_text(self) -> str:
        return self._final_output_text

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            raise ValueError(f"CLI JSON stream line must decode to object, got {type(payload)!r}")

        event_type = str(payload.get("type") or "").strip().lower()
        if not event_type:
            raise ValueError("CLI JSON stream event is missing type")

        session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip() or None
        if session_id:
            self._session_id = session_id

        if event_type == "system" and str(payload.get("subtype") or "").strip().lower() == "init":
            return [
                CliJsonStreamEvent(
                    kind="session_started",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    payload=payload,
                )
            ]

        if event_type == "assistant":
            message = payload.get("message")
            if not isinstance(message, dict):
                return []
            content = message.get("content")
            if not isinstance(content, list):
                return []
            events: list[CliJsonStreamEvent] = []
            text_parts: list[str] = []
            thought_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "tool_use":
                    tool_id = str(item.get("id") or "").strip()
                    tool_label = _tool_label(item.get("name"), item.get("input"))
                    if tool_id:
                        self._tool_labels[tool_id] = tool_label
                    events.append(
                        CliJsonStreamEvent(
                            kind="tool_event",
                            cli_name=self.cli_name,
                            session_id=self._session_id,
                            turn_id=tool_id or None,
                            text=tool_label,
                            payload=payload,
                        )
                    )
                    continue
                if item_type == "text":
                    text = str(item.get("text") or "")
                    if text:
                        text_parts.append(text)
                    continue
                if item_type == "thinking":
                    thought = str(item.get("thinking") or item.get("text") or "")
                    if thought:
                        thought_parts.append(thought)
            if thought_parts:
                events.append(
                    CliJsonStreamEvent(
                        kind="thinking",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        text="\n\n".join(thought_parts),
                        payload=payload,
                    )
                )
            text = "".join(text_parts)
            if text:
                self._final_output_text = text
                events.append(
                    CliJsonStreamEvent(
                        kind="assistant_text",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        text=text,
                        payload=payload,
                    )
                )
            if events:
                return events
            return []

        if event_type == "user":
            message = payload.get("message")
            if not isinstance(message, dict):
                return []
            content = message.get("content")
            if not isinstance(content, list):
                return []
            events: list[CliJsonStreamEvent] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "tool_result":
                    continue
                tool_id = str(item.get("tool_use_id") or item.get("toolUseId") or "").strip()
                tool_label = self._tool_labels.get(tool_id) or (tool_id or "tool")
                events.append(
                    CliJsonStreamEvent(
                        kind="tool_event",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        turn_id=tool_id or None,
                        text=_tool_result_text(
                            tool_label,
                            detail=_tool_result_detail(item.get("content")),
                            is_error=bool(item.get("is_error")),
                        ),
                        payload=payload,
                    )
                )
            return events

        if event_type == "result":
            result_text = str(payload.get("result") or "")
            if result_text:
                self._final_output_text = result_text
            is_error = bool(payload.get("is_error"))
            terminal_kind = "failed" if is_error else "completed"
            if terminal_kind == "completed":
                self._completed = True
            return [
                CliJsonStreamEvent(
                    kind=terminal_kind,
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=(result_text or None) if terminal_kind == "failed" else None,
                    payload=payload,
                )
            ]

        if event_type.endswith(".failed") or event_type == "error":
            return [
                CliJsonStreamEvent(
                    kind="failed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=str(payload.get("message") or payload.get("result") or "") or None,
                    payload=payload,
                )
            ]

        return [
            CliJsonStreamEvent(
                kind="raw_event",
                cli_name=self.cli_name,
                session_id=self._session_id,
                payload=payload,
            )
        ]


class ClaudeJsonStreamAdapter(BaseCliJsonStreamAdapter):
    cli_name = "claude"

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._final_output_text = ""
        self._completed = False
        self._tool_labels: dict[str, str] = {}

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def completed(self) -> bool:
        return self._completed

    def final_output_text(self) -> str:
        return self._final_output_text

    def _build_rate_limit_event(self, payload: dict[str, Any]) -> CliJsonStreamEvent:
        raw_usage = payload.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        rl_payload = {
            "input_tokens": usage.get("input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "raw": payload,
        }
        return CliJsonStreamEvent(
            kind="rate_limit",
            cli_name=self.cli_name,
            session_id=self._session_id,
            payload=rl_payload,
        )

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            raise ValueError(f"CLI JSON stream line must decode to object, got {type(payload)!r}")

        event_type = str(payload.get("type") or "").strip().lower()
        if not event_type:
            raise ValueError("CLI JSON stream event is missing type")

        session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip() or None
        if session_id:
            self._session_id = session_id

        if event_type == "system" and str(payload.get("subtype") or "").strip().lower() == "init":
            return [
                CliJsonStreamEvent(
                    kind="session_started",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    payload=payload,
                )
            ]

        if event_type == "assistant":
            message = payload.get("message")
            if not isinstance(message, dict):
                return []
            content = message.get("content")
            if not isinstance(content, list):
                return []
            events: list[CliJsonStreamEvent] = []
            text_parts: list[str] = []
            thought_parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "tool_use":
                    tool_id = str(item.get("id") or "").strip()
                    tool_label = _tool_label(item.get("name"), item.get("input"))
                    if tool_id:
                        self._tool_labels[tool_id] = tool_label
                    events.append(
                        CliJsonStreamEvent(
                            kind="tool_event",
                            cli_name=self.cli_name,
                            session_id=self._session_id,
                            turn_id=tool_id or None,
                            text=tool_label,
                            payload=payload,
                        )
                    )
                    continue
                if item_type == "text":
                    text = str(item.get("text") or "")
                    if text:
                        text_parts.append(text)
                    continue
                if item_type == "thinking":
                    thought = str(item.get("thinking") or item.get("text") or "")
                    if thought:
                        thought_parts.append(thought)
            if thought_parts:
                events.append(
                    CliJsonStreamEvent(
                        kind="thinking",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        text="\n\n".join(thought_parts),
                        payload=payload,
                    )
                )
            text = "".join(text_parts)
            if text:
                self._final_output_text = text
                events.append(
                    CliJsonStreamEvent(
                        kind="assistant_text",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        text=text,
                        payload=payload,
                    )
                )
            if events:
                return events
            return []

        if event_type == "user":
            message = payload.get("message")
            if not isinstance(message, dict):
                return []
            content = message.get("content")
            if not isinstance(content, list):
                return []
            events: list[CliJsonStreamEvent] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "tool_result":
                    continue
                tool_id = str(item.get("tool_use_id") or item.get("toolUseId") or "").strip()
                tool_label = self._tool_labels.get(tool_id) or (tool_id or "tool")
                events.append(
                    CliJsonStreamEvent(
                        kind="tool_event",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        turn_id=tool_id or None,
                        text=_tool_result_text(
                            tool_label,
                            detail=_tool_result_detail(item.get("content")),
                            is_error=bool(item.get("is_error")),
                        ),
                        payload=payload,
                    )
                )
            return events

        if event_type == "system":
            subtype = str(payload.get("subtype") or "").strip().lower()
            if subtype == "init":
                return [
                    CliJsonStreamEvent(
                        kind="session_started",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        payload=payload,
                    )
                ]
            if subtype in {"task_started", "task_progress"}:
                description = _truncate(payload.get("description") or subtype.replace("_", " "), 160)
                if description:
                    return [
                        CliJsonStreamEvent(
                            kind="progress",
                            cli_name=self.cli_name,
                            session_id=self._session_id,
                            text=f"{subtype}: {description}",
                            payload=payload,
                        )
                    ]
                return []
            if subtype == "task_notification":
                task_id = str(payload.get("task_id") or "").strip() or None
                return [
                    CliJsonStreamEvent(
                        kind="progress",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        turn_id=task_id,
                        text=_claude_task_notification_text(payload),
                        payload=payload,
                    )
                ]
            if subtype == "rate_limit_event":
                return [self._build_rate_limit_event(payload)]

        if event_type == "rate_limit_event":
            return [self._build_rate_limit_event(payload)]

        if event_type == "result":
            origin = payload.get("origin")
            if isinstance(origin, dict) and str(origin.get("kind") or "").strip().lower() == "task-notification":
                return []
            result_text = str(payload.get("result") or "")
            if result_text:
                self._final_output_text = result_text
            is_error = bool(payload.get("is_error"))
            terminal_kind = "failed" if is_error else "completed"
            if terminal_kind == "completed":
                self._completed = True
            return [
                CliJsonStreamEvent(
                    kind=terminal_kind,
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=(result_text or None) if terminal_kind == "failed" else None,
                    payload=payload,
                )
            ]

        if event_type.endswith(".failed") or event_type == "error":
            return [
                CliJsonStreamEvent(
                    kind="failed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=str(payload.get("message") or payload.get("result") or "") or None,
                    payload=payload,
                )
            ]

        return [
            CliJsonStreamEvent(
                kind="raw_event",
                cli_name=self.cli_name,
                session_id=self._session_id,
                payload=payload,
            )
        ]


class GrokJsonStreamAdapter(BaseCliJsonStreamAdapter):
    cli_name = "grok"

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._final_output_text = ""
        self._completed = False

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def completed(self) -> bool:
        return self._completed

    def final_output_text(self) -> str:
        return self._final_output_text

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            raise ValueError(f"CLI JSON stream line must decode to object, got {type(payload)!r}")

        event_type = str(payload.get("type") or "").strip().lower()
        if not event_type:
            raise ValueError("CLI JSON stream event is missing type")

        if event_type == "text":
            text = _coerce_message_text(
                payload.get("data")
                or payload.get("text")
                or payload.get("content")
                or payload.get("message")
            )
            if not text:
                return []
            self._final_output_text += text
            event_payload = dict(payload)
            event_payload["delta"] = True
            return [
                CliJsonStreamEvent(
                    kind="assistant_text",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    text=text,
                    payload=event_payload,
                )
            ]

        if event_type == "end":
            session_id = str(
                payload.get("sessionId")
                or payload.get("session_id")
                or payload.get("session")
                or ""
            ).strip() or None
            events: list[CliJsonStreamEvent] = []
            if session_id:
                self._session_id = session_id
                events.append(
                    CliJsonStreamEvent(
                        kind="session_started",
                        cli_name=self.cli_name,
                        session_id=session_id,
                        payload=payload,
                    )
                )
            self._completed = True
            events.append(
                CliJsonStreamEvent(
                    kind="completed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    payload=payload,
                )
            )
            return events

        if event_type in {"error", "failed"}:
            return [
                CliJsonStreamEvent(
                    kind="failed",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    is_terminal=True,
                    text=str(payload.get("message") or payload.get("data") or "") or None,
                    payload=payload,
                )
            ]

        # Grok emits tokenized `thought` events in streaming-json mode. Do not
        # expose chain-of-thought-like data to user-visible activity ticks.
        if event_type in {"thought", "thinking"}:
            return []

        return [
            CliJsonStreamEvent(
                kind="raw_event",
                cli_name=self.cli_name,
                session_id=self._session_id,
                payload=payload,
            )
        ]


class KimiJsonStreamAdapter(BaseCliJsonStreamAdapter):
    """Kimi Code CLI в `--output-format stream-json` печатает чат-сообщения OpenAI-вида.

    Строки бывают трёх видов: `role=assistant` (текст и/или `tool_calls`),
    `role=tool` (результат вызова) и служебные `role=meta` (`system.version`,
    `turn.step.retrying`, `session.resume_hint`). Токен для `--resume` приходит
    только в самом конце хода, в `session.resume_hint`. Признака завершения в
    потоке нет, поэтому ход закрывается по EOF процесса.
    """

    cli_name = "kimi"

    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._final_output_text = ""
        self._tool_labels: dict[str, str] = {}

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def final_output_text(self) -> str:
        return self._final_output_text

    def feed_line(self, line: str) -> list[CliJsonStreamEvent]:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            raise ValueError(f"CLI JSON stream line must decode to object, got {type(payload)!r}")

        role = str(payload.get("role") or "").strip().lower()
        # Сводка `/goal` печатается единственной строкой без `role`, но с `type`.
        if not role and not str(payload.get("type") or "").strip():
            raise ValueError("CLI JSON stream event is missing role")

        events: list[CliJsonStreamEvent] = []
        session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip() or None
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            events.append(
                CliJsonStreamEvent(
                    kind="session_started",
                    cli_name=self.cli_name,
                    session_id=session_id,
                    payload=payload,
                )
            )

        if role == "assistant":
            text = _coerce_message_text(payload.get("content"))
            if text:
                self._final_output_text = text
                events.append(
                    CliJsonStreamEvent(
                        kind="assistant_text",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        text=text,
                        payload=payload,
                    )
                )
            tool_calls = payload.get("tool_calls")
            for call in tool_calls if isinstance(tool_calls, list) else []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                function = function if isinstance(function, dict) else {}
                call_id = str(call.get("id") or "").strip()
                tool_label = _tool_label(function.get("name"), _kimi_tool_arguments(function.get("arguments")))
                if call_id:
                    self._tool_labels[call_id] = tool_label
                events.append(
                    CliJsonStreamEvent(
                        kind="tool_event",
                        cli_name=self.cli_name,
                        session_id=self._session_id,
                        turn_id=call_id or None,
                        text=tool_label,
                        payload=payload,
                    )
                )
            return events

        if role == "tool":
            call_id = str(payload.get("tool_call_id") or payload.get("toolCallId") or "").strip()
            tool_label = self._tool_labels.get(call_id) or (call_id or "tool")
            events.append(
                CliJsonStreamEvent(
                    kind="tool_event",
                    cli_name=self.cli_name,
                    session_id=self._session_id,
                    turn_id=call_id or None,
                    text=_tool_result_text(
                        tool_label,
                        detail=_tool_result_detail(payload.get("content")),
                        # Kimi не размечает провал инструмента отдельным полем,
                        # текст ошибки приходит в том же `content`.
                        is_error=False,
                    ),
                    payload=payload,
                )
            )
            return events

        events.append(
            CliJsonStreamEvent(
                kind="raw_event",
                cli_name=self.cli_name,
                session_id=self._session_id,
                payload=payload,
            )
        )
        return events


def build_cli_json_stream_adapter(cli_name: str) -> Optional[BaseCliJsonStreamAdapter]:
    name = str(cli_name or "").strip().lower()
    if name == "codex":
        return CodexJsonStreamAdapter()
    if name == "gemini":
        return GeminiJsonStreamAdapter()
    if name == "qwen":
        return QwenJsonStreamAdapter()
    if name == "claude":
        return ClaudeJsonStreamAdapter()
    if name == "grok":
        return GrokJsonStreamAdapter()
    if name == "kimi":
        return KimiJsonStreamAdapter()
    return None


def recover_cli_text_from_raw_stream(cli_name: str, raw_text: str) -> str:
    adapter = build_cli_json_stream_adapter(cli_name)
    if adapter is None:
        return ""

    assistant_parts: list[str] = []
    fallback_texts: list[str] = []
    for line in str(raw_text or "").splitlines():
        payload_line = str(line or "").strip()
        if not payload_line:
            continue
        try:
            events = adapter.feed_line(payload_line)
        except Exception:
            continue
        for event in events:
            text = str(event.text or "").strip()
            if event.kind == "assistant_text" and text:
                assistant_parts.append(text)
            elif text:
                fallback_texts.append(text)

    final_text = str(adapter.final_output_text() or "").strip()
    if final_text:
        return final_text
    if assistant_parts:
        return "".join(assistant_parts).strip()
    if fallback_texts:
        return fallback_texts[-1]
    return ""


class CliJsonStreamRecorder:
    def __init__(
        self,
        *,
        enabled: bool,
        workdir: str,
        cli_name: str,
        session_uid: str,
    ) -> None:
        self._enabled = bool(enabled)
        self._workdir = Path(workdir)
        self._cli_name = _safe_token(cli_name)
        self._session_uid = _safe_token(session_uid)
        self._run_token = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        self._raw_path: Optional[Path] = None
        self._normalized_path: Optional[Path] = None
        self._raw_handle = None
        self._normalized_handle = None
        self._pending_raw: list[str] = []
        self._pending_normalized: list[str] = []
        self._write_lock = threading.Lock()

    @property
    def raw_path(self) -> Optional[Path]:
        return self._raw_path

    @property
    def normalized_path(self) -> Optional[Path]:
        return self._normalized_path

    def record_raw_line(self, line: str) -> None:
        if not self._enabled:
            return
        self._pending_raw.append(str(line).rstrip("\n") + "\n")

    def record_event(self, event: CliJsonStreamEvent) -> None:
        if not self._enabled:
            return
        self._pending_normalized.append(json.dumps(event.to_record(), ensure_ascii=False) + "\n")

    async def flush_pending(self) -> None:
        """Сбросить накопленные строки на диск вне event loop."""
        if not self._enabled:
            return
        raw, normalized = self._take_pending()
        if not raw and not normalized:
            return
        await asyncio.to_thread(self._write_pending, raw, normalized)

    def _take_pending(self) -> tuple[list[str], list[str]]:
        raw, self._pending_raw = self._pending_raw, []
        normalized, self._pending_normalized = self._pending_normalized, []
        return raw, normalized

    def _write_pending(self, raw: list[str], normalized: list[str]) -> None:
        # Запись идёт из рабочего потока to_thread, а close() — из event loop,
        # поэтому доступ к дескрипторам сериализуется.
        with self._write_lock:
            self._write_pending_locked(raw, normalized)

    def _write_pending_locked(self, raw: list[str], normalized: list[str]) -> None:
        self._ensure_open()
        if raw:
            assert self._raw_handle is not None
            self._raw_handle.write("".join(raw))
            self._raw_handle.flush()
        if normalized:
            assert self._normalized_handle is not None
            self._normalized_handle.write("".join(normalized))
            self._normalized_handle.flush()

    def close(self) -> None:
        with self._write_lock:
            if self._enabled:
                raw, normalized = self._take_pending()
                if raw or normalized:
                    try:
                        self._write_pending_locked(raw, normalized)
                    except Exception:
                        logger.exception("CLI JSON stream recorder final flush failed")
            for handle_name in ("_raw_handle", "_normalized_handle"):
                handle = getattr(self, handle_name)
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception:
                    logger.exception("CLI JSON stream recorder close failed handle=%s", handle_name)
                finally:
                    setattr(self, handle_name, None)

    def _ensure_open(self) -> None:
        if self._raw_handle is not None and self._normalized_handle is not None:
            return
        base_dir = self._workdir / ".cli-proxy" / "cli-json-stream" / self._cli_name / self._session_uid
        base_dir.mkdir(parents=True, exist_ok=True)
        if self._raw_path is None:
            self._raw_path = base_dir / f"{self._run_token}.raw.jsonl"
        if self._normalized_path is None:
            self._normalized_path = base_dir / f"{self._run_token}.normalized.jsonl"
        if self._raw_handle is None:
            self._raw_handle = self._raw_path.open("a", encoding="utf-8")
        try:
            if self._normalized_handle is None:
                self._normalized_handle = self._normalized_path.open("a", encoding="utf-8")
        except Exception:
            if self._raw_handle is not None:
                self._raw_handle.close()
                self._raw_handle = None
            raise


def _collect_path_like_values(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key or "").strip()
            if key_text in {"absolute_path", "file_path", "filePath", "path", "dir_path"}:
                path = str(nested or "").strip()
                if path:
                    paths.append(path)
            else:
                paths.extend(_collect_path_like_values(nested))
        return paths
    if isinstance(value, list):
        for item in value:
            paths.extend(_collect_path_like_values(item))
    return paths


def extract_cli_evidence_from_normalized_stream(path: Any) -> list[dict[str, Any]]:
    normalized_path = Path(str(path or "").strip())
    if not str(normalized_path):
        return []
    if not normalized_path.exists() or not normalized_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        lines = normalized_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.exception("failed to read normalized CLI stream path=%s", normalized_path)
        return []
    for line in lines:
        payload = loads_safe(line)
        if not isinstance(payload, dict):
            continue
        event_kind = str(payload.get("kind") or "").strip().lower()
        event_text = str(payload.get("text") or "").strip()
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = {}
        candidate_paths = _collect_path_like_values(event_payload)
        if not candidate_paths and event_kind not in {"tool_event", "progress"}:
            continue
        preview = _truncate(event_text or _tool_result_detail(event_payload), 240)
        for candidate in candidate_paths:
            key = ("repo_evidence", str(candidate).strip(), preview)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "type": "repo_evidence",
                    "path": str(candidate).strip(),
                    "content_preview": preview,
                    "name": f"{event_kind or 'event'} evidence",
                }
            )
    return records


__all__ = [
    "BaseCliJsonStreamAdapter",
    "CliJsonStreamEvent",
    "CliJsonStreamRecorder",
    "ClaudeJsonStreamAdapter",
    "CodexJsonStreamAdapter",
    "GeminiJsonStreamAdapter",
    "KimiJsonStreamAdapter",
    "QwenJsonStreamAdapter",
    "build_cli_json_stream_adapter",
    "cli_json_stream_archive_enabled",
    "extract_cli_evidence_from_normalized_stream",
    "recover_cli_text_from_raw_stream",
]
