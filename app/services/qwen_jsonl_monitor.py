"""
Monitor Qwen Code chat recordings in real-time.

Qwen Code writes chat events to .jsonl files in:
  /root/.qwen/projects/<project_key>/chats/<session_id>.jsonl

When --chat-recording is enabled, this file is updated in real-time
and can be used to extract progress ticks for the miniapp.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from modes.sdk.runtime.json_normalizer import loads_safe

logger = logging.getLogger(__name__)

QWEN_CHAT_BASE_DIR = Path("/root/.qwen/projects")


def _truncate(text: str, limit: int) -> str:
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


def _tool_args_preview(tool_name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return _truncate(str(payload or ""), 120)

    tool = str(tool_name or "").strip().lower()
    file_path = str(payload.get("absolute_path") or payload.get("file_path") or payload.get("path") or "").strip()
    pattern = str(payload.get("pattern") or "").strip()
    command = str(payload.get("command") or "").strip()

    if tool in {"read_file", "write_file", "list_directory"} and file_path:
        return _truncate(_short_path(file_path), 120)
    if tool == "glob" and pattern:
        return _truncate(pattern, 120)
    if tool == "grep_search" and pattern:
        if file_path:
            return _truncate(f"{pattern} @ {_short_path(file_path)}", 120)
        return _truncate(pattern, 120)
    if tool == "run_shell_command" and command:
        return _truncate(command, 120)

    for candidate in (command, pattern, file_path):
        if candidate:
            if candidate == file_path:
                return _truncate(_short_path(candidate), 120)
            return _truncate(candidate, 120)
    return ""


def _pick_result_text(result_display: str, detail: str) -> str:
    primary = _truncate(detail, 100) if detail else ""
    fallback = _truncate(result_display, 100) if result_display else ""
    if primary and (not fallback or len(primary) > len(fallback) + 16):
        return primary
    return fallback or primary


@dataclass
class QwenChatEvent:
    """A single event from Qwen Code chat recording."""

    uuid: str
    session_id: str
    timestamp: float
    event_type: str  # user, assistant, tool_call, tool_result, system
    subtype: Optional[str] = None  # ui_telemetry, etc.
    text: Optional[str] = None  # Assistant text output
    thought: Optional[str] = None  # Assistant thinking/reasoning
    tool_name: Optional[str] = None  # Tool being called
    tool_args: Optional[Dict[str, Any]] = None  # Tool arguments
    tool_result: Optional[str] = None  # Tool execution result
    tool_detail: Optional[str] = None  # Detailed tool output/error
    tool_status: Optional[str] = None  # success/error
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_jsonl_line(cls, line: str) -> Optional["QwenChatEvent"]:
        """Parse a single JSONL line into an event."""
        try:
            data = loads_safe(line.strip(), strict_first=True)
        except Exception:
            return None

        event_type = data.get("type", "")
        session_id = data.get("sessionId", "")
        uuid = data.get("uuid", "")
        timestamp_str = data.get("timestamp", "")

        # Parse timestamp
        try:
            from datetime import datetime

            ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = time.time()

        event = cls(
            uuid=uuid,
            session_id=session_id,
            timestamp=ts,
            event_type=event_type,
            raw=data,
        )

        # Extract subtype for system events
        if event_type == "system":
            event.subtype = data.get("subtype")
            if event.subtype == "ui_telemetry":
                payload = data.get("systemPayload", {})
                ui_event = payload.get("uiEvent", {})
                event.tool_name = ui_event.get("function_name")
                args = ui_event.get("function_args")
                if isinstance(args, dict):
                    event.tool_args = args
                event.tool_status = str(ui_event.get("status") or "").strip() or None

        # Extract assistant text and thoughts
        if event_type == "assistant":
            message = data.get("message", {})
            parts = message.get("parts", [])
            texts = []
            thoughts = []
            for part in parts:
                if part.get("thought"):
                    thoughts.append(part.get("text", ""))
                elif "text" in part:
                    texts.append(part.get("text", ""))
            event.text = "".join(texts) if texts else None
            event.thought = "".join(thoughts) if thoughts else None

        # Extract tool call info
        if event_type == "tool_call":
            payload = data.get("systemPayload", {})
            ui_event = payload.get("uiEvent", {})
            event.tool_name = ui_event.get("function_name")
            args = ui_event.get("function_args")
            if isinstance(args, dict):
                event.tool_args = args
            event.tool_status = str(ui_event.get("status") or "").strip() or None
            event.subtype = "tool_call"

        # Extract tool result
        if event_type == "tool_result":
            tool_result = data.get("toolCallResult", {})
            event.tool_result = tool_result.get("resultDisplay")
            event.tool_status = str(tool_result.get("status") or "").strip() or None
            message = data.get("message", {})
            parts = message.get("parts", [])
            for part in parts:
                func_resp = part.get("functionResponse", {})
                if not func_resp:
                    continue
                event.tool_name = str(func_resp.get("name") or "").strip() or event.tool_name
                resp = func_resp.get("response", {})
                output = str(resp.get("output") or "").strip()
                error = str(resp.get("error") or "").strip()
                event.tool_detail = output or error or None
                if not event.tool_result:
                    event.tool_result = output or error or ""
                break

        return event


@dataclass
class QwenSessionMonitor:
    """Monitor a single Qwen Code session."""

    session_id: str
    jsonl_path: Path
    last_position: int = 0
    seen_uuids: Set[str] = field(default_factory=set)
    last_read_time: float = 0.0
    callback: Optional[Callable[[QwenChatEvent], None]] = None

    def read_new_events(self) -> List[QwenChatEvent]:
        """Read new events from the JSONL file."""
        events: List[QwenChatEvent] = []

        if not self.jsonl_path.exists():
            return events

        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                # Seek to last position
                f.seek(self.last_position)

                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    event = QwenChatEvent.from_jsonl_line(line)
                    if event and event.uuid not in self.seen_uuids:
                        self.seen_uuids.add(event.uuid)
                        events.append(event)

                        # Call callback if provided
                        if self.callback:
                            try:
                                self.callback(event)
                            except Exception:
                                logger.exception("Qwen monitor callback failed")

                # Update position
                self.last_position = f.tell()
                self.last_read_time = time.time()

        except Exception:
            logger.exception("Failed to read Qwen JSONL file")

        return events


class QwenJsonlMonitor:
    """
    Monitor all Qwen Code chat recordings in real-time.

    Usage:
        monitor = QwenJsonlMonitor(workdir)
        monitor.start()
        # ... events will be sent to callback ...
        monitor.stop()
    """

    def __init__(
        self,
        workdir: str,
        callback: Optional[Callable[[QwenChatEvent], None]] = None,
        poll_interval: float = 0.5,
    ):
        self.workdir = workdir
        self.callback = callback
        self.poll_interval = poll_interval
        self.monitors: Dict[str, QwenSessionMonitor] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._log = logging.getLogger(self.__class__.__name__)

    def _project_key_candidates(self) -> List[str]:
        """Return possible Qwen project directory keys for this workdir."""
        raw = os.path.realpath(self.workdir).rstrip(os.sep) or self.workdir
        slash_key = raw.replace(os.sep, "-")
        compact_key = re.sub(r"[^A-Za-z0-9]+", "-", raw)
        if raw.startswith(os.sep) and not compact_key.startswith("-"):
            compact_key = "-" + compact_key
        compact_key = compact_key.rstrip("-")

        out: List[str] = []
        for key in (slash_key, compact_key):
            if key and key not in out:
                out.append(key)
        return out

    def _find_chat_dir(self) -> Optional[Path]:
        """Find the chat directory for this workdir."""
        for project_key in self._project_key_candidates():
            chat_dir = QWEN_CHAT_BASE_DIR / project_key / "chats"
            if chat_dir.exists():
                return chat_dir

        # Try to find by workdir pattern
        if QWEN_CHAT_BASE_DIR.exists():
            for project_dir in QWEN_CHAT_BASE_DIR.iterdir():
                if not project_dir.is_dir():
                    continue
                chats_dir = project_dir / "chats"
                if chats_dir.exists():
                    # Check if any chat file references this workdir
                    for jsonl_file in chats_dir.glob("*.jsonl"):
                        try:
                            with open(jsonl_file, "r") as f:
                                first_line = f.readline()
                                if first_line and self.workdir in first_line:
                                    return chats_dir
                        except Exception:
                            pass

        return None

    def _find_latest_session_file(self, chat_dir: Path) -> Optional[Path]:
        """Find the most recently modified JSONL file."""
        jsonl_files = list(chat_dir.glob("*.jsonl"))
        if not jsonl_files:
            return None

        # Sort by modification time, newest first
        jsonl_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return jsonl_files[0]

    def _discover_sessions(self, chat_dir: Path) -> None:
        """Discover and create monitors for all sessions."""
        for jsonl_file in chat_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            if session_id not in self.monitors:
                self.monitors[session_id] = QwenSessionMonitor(
                    session_id=session_id,
                    jsonl_path=jsonl_file,
                    callback=self.callback,
                )
                self._log.info("Discovered Qwen session: %s", session_id)

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                # Find chat directory
                chat_dir = self._find_chat_dir()
                if chat_dir:
                    # Discover new sessions
                    self._discover_sessions(chat_dir)

                    # Read new events from all monitors
                    for monitor in list(self.monitors.values()):
                        monitor.read_new_events()
                        # Monitors already call callback in read_new_events

                # Wait for next poll
                await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                break
            except Exception:
                self._log.exception("Qwen JSONL monitor poll failed")
                await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        self._log.info("Qwen JSONL monitor started for %s", self.workdir)

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                asyncio.get_event_loop().run_until_complete(self._task)
            except Exception:
                pass
            self._task = None
        self._log.info("Qwen JSONL monitor stopped")

    def get_latest_session_id(self) -> Optional[str]:
        """Get the most recent session ID."""
        if not self.monitors:
            return None

        # Find monitor with most recent activity
        latest_monitor = max(
            self.monitors.values(),
            key=lambda m: m.last_read_time,
            default=None,
        )
        return latest_monitor.session_id if latest_monitor else None


def extract_progress_text(event: QwenChatEvent) -> Optional[str]:
    """Extract progress text from a Qwen event for display."""
    if event.thought:
        return f"🤔 {_truncate(event.thought, 200)}"

    # Tool calls are system/ui_telemetry events
    if event.subtype == "ui_telemetry":
        ui_event = event.raw.get("systemPayload", {}).get("uiEvent", {})
        tool_name = ui_event.get("function_name")
        if tool_name:
            preview = _tool_args_preview(tool_name, ui_event.get("function_args"))
            if preview:
                return f"🔧 {tool_name}({preview})"
            return f"🔧 {tool_name}(...)"

    if event.event_type == "tool_call" and event.tool_name:
        preview = _tool_args_preview(event.tool_name, event.tool_args)
        if preview:
            return f"🔧 {event.tool_name}({preview})"
        return f"🔧 {event.tool_name}(...)"
    if event.tool_result or event.tool_detail:
        result = _pick_result_text(str(event.tool_result or ""), str(event.tool_detail or ""))
        if result:
            status = str(event.tool_status or "").strip().lower()
            icon = "❌" if status in {"error", "failed", "failure"} else "✅"
            tool_prefix = f"{event.tool_name}: " if event.tool_name else ""
            return f"{icon} {tool_prefix}{result}"
    if event.text:
        return _truncate(event.text, 200)
    return None
