"""
Monitor Gemini CLI session JSON files in real time.

Gemini stores chat state under:
  ~/.gemini/tmp/<project_key>/chats/session-*.json

Unlike append-only transcripts, these files are rewritten as the session
progresses. This monitor follows the active file for the current workdir and
emits progress-style items extracted from thoughts, tool calls, and assistant
messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from modes.sdk.runtime.json_normalizer import loads_safe

logger = logging.getLogger(__name__)


def _gemini_tmp_base_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".gemini" / "tmp"


def _parse_timestamp(raw: Any) -> float:
    text = str(raw or "").strip()
    if not text:
        return time.time()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


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


def _tool_label(tool_call: Dict[str, Any]) -> str:
    label = str(tool_call.get("displayName") or "").strip()
    if label:
        return label
    label = str(tool_call.get("name") or "").strip()
    if label:
        return label
    return "Tool"


def _thought_text(thought: Dict[str, Any]) -> str:
    subject = str(thought.get("subject") or "").strip()
    if subject:
        return f"🤔 {_truncate(subject, 120)}"
    description = str(thought.get("description") or "").strip()
    if description:
        return f"🤔 {_truncate(description, 200)}"
    return ""


def _tool_args_preview(tool_call: Dict[str, Any]) -> str:
    payload = tool_call.get("args")
    if not isinstance(payload, dict):
        return ""

    tool = str(tool_call.get("name") or "").strip().lower()
    file_path = str(payload.get("file_path") or payload.get("path") or "").strip()
    pattern = str(payload.get("pattern") or "").strip()
    command = str(payload.get("command") or "").strip()

    if tool in {"read_file", "write_file", "list_directory"} and file_path:
        start_line = payload.get("start_line")
        end_line = payload.get("end_line")
        line_suffix = ""
        if start_line and end_line:
            line_suffix = f":{start_line}-{end_line}"
        elif start_line:
            line_suffix = f":{start_line}"
        return _truncate(f"{_short_path(file_path)}{line_suffix}", 120)
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


def _tool_result_detail(tool_call: Dict[str, Any]) -> str:
    results = tool_call.get("result")
    if not isinstance(results, list):
        return ""
    for item in results:
        if not isinstance(item, dict):
            continue
        function_response = item.get("functionResponse")
        if not isinstance(function_response, dict):
            continue
        response = function_response.get("response")
        if not isinstance(response, dict):
            continue
        output = str(response.get("output") or "").strip()
        error = str(response.get("error") or "").strip()
        if output:
            return _truncate(output, 180)
        if error:
            return _truncate(error, 180)
    return ""


def _tool_result_text(tool_call: Dict[str, Any]) -> str:
    label = _tool_label(tool_call)
    status = str(tool_call.get("status") or "").strip().lower()
    result_display = _truncate(str(tool_call.get("resultDisplay") or "").strip(), 180)
    detail = _tool_result_detail(tool_call)
    preferred = detail if detail and len(detail) > len(result_display or "") + 16 else result_display or detail

    if status in {"error", "failed", "failure"}:
        return f"❌ {label}: {preferred or status}"
    if preferred:
        return f"✅ {label}: {preferred}"
    if status in {"success", "completed", "done"}:
        return f"✅ {label} completed"
    return ""


@dataclass
class GeminiProgressEvent:
    session_id: str
    timestamp: float
    progress_items: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _GeminiToolState:
    started: bool = False
    result_signature: Optional[str] = None


@dataclass
class _GeminiMessageState:
    last_content: str = ""
    thoughts_seen: Set[str] = field(default_factory=set)
    tool_states: Dict[str, _GeminiToolState] = field(default_factory=dict)


class GeminiJsonMonitor:
    """Track one active Gemini session JSON file for the current workdir."""

    def __init__(
        self,
        workdir: str,
        callback: Optional[Callable[[GeminiProgressEvent], None]] = None,
        session_callback: Optional[Callable[[str], None]] = None,
        poll_interval: float = 0.3,
        session_id: Optional[str] = None,
    ):
        self.workdir = workdir
        self.callback = callback
        self.session_callback = session_callback
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._log = logging.getLogger(self.__class__.__name__)
        self._tracked_session_id: Optional[str] = str(session_id or "").strip() or None
        self._tracked_json_path: Optional[Path] = None
        self._message_states: Dict[str, _GeminiMessageState] = {}
        self._known_file_stats: Dict[str, Tuple[int, float]] = self._snapshot_known_files()
        self._tracked_file_stat: Optional[Tuple[int, float]] = None

    def _emit_session_id(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid or self.session_callback is None:
            return
        try:
            self.session_callback(sid)
        except Exception:
            self._log.exception("Gemini session callback failed")

    @property
    def project_hash(self) -> str:
        raw = os.path.realpath(self.workdir).rstrip(os.sep) or self.workdir
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _project_name_candidates(self) -> List[str]:
        raw = os.path.realpath(self.workdir).rstrip(os.sep) or self.workdir
        base = os.path.basename(raw)
        return [base] if base else []

    def _candidate_project_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        seen: Set[str] = set()
        base_dir = _gemini_tmp_base_dir()
        if not base_dir.is_dir():
            return dirs

        project_names = [name for name in self._project_name_candidates() if name]
        normalized_exact_names = {name.casefold() for name in project_names}
        normalized_prefixes = tuple(f"{name.casefold()}-" for name in project_names)

        for child in base_dir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            normalized_name = name.casefold()
            if (
                name == self.project_hash
                or normalized_name in normalized_exact_names
                or any(normalized_name.startswith(prefix) for prefix in normalized_prefixes)
            ):
                key = str(child.resolve())
                if key in seen:
                    continue
                seen.add(key)
                dirs.append(child)
        return dirs

    def _candidate_session_files(self) -> List[Path]:
        files: List[Path] = []
        seen: Set[str] = set()
        for project_dir in self._candidate_project_dirs():
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            for session_file in chats_dir.glob("session-*.json"):
                key = str(session_file.resolve())
                if key in seen:
                    continue
                seen.add(key)
                files.append(session_file)
        return files

    def _snapshot_known_files(self) -> Dict[str, Tuple[int, float]]:
        snapshot: Dict[str, Tuple[int, float]] = {}
        for session_file in self._candidate_session_files():
            try:
                stat = session_file.stat()
            except Exception:
                continue
            snapshot[str(session_file.resolve())] = (int(stat.st_size), float(stat.st_mtime))
        return snapshot

    @staticmethod
    def _message_key(index: int, message: Dict[str, Any]) -> str:
        message_id = str(message.get("id") or "").strip()
        if message_id:
            return message_id
        return f"idx:{index}"

    @staticmethod
    def _tool_key(index: int, tool_call: Dict[str, Any]) -> str:
        tool_id = str(tool_call.get("id") or "").strip()
        if tool_id:
            return tool_id
        name = str(tool_call.get("name") or "").strip() or "tool"
        return f"{name}:{index}"

    @staticmethod
    def _load_payload(path: Path) -> Optional[Dict[str, Any]]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
        try:
            payload = loads_safe(text, strict_first=True)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _matching_payload(self, path: Path) -> Optional[Dict[str, Any]]:
        payload = self._load_payload(path)
        if not isinstance(payload, dict):
            return None
        if str(payload.get("projectHash") or "").strip() != self.project_hash:
            return None
        return payload

    def _discover_tracked_file(self) -> Optional[Tuple[Path, Dict[str, Any], bool]]:
        files = self._candidate_session_files()
        if not files:
            self._known_file_stats = {}
            return None

        current_stats: Dict[str, Tuple[int, float]] = {}
        new_candidates: List[Tuple[float, Path, Dict[str, Any]]] = []
        changed_candidates: List[Tuple[float, Path, Dict[str, Any]]] = []
        matched_session: Optional[Tuple[Path, Dict[str, Any]]] = None

        for session_file in files:
            try:
                stat = session_file.stat()
            except Exception:
                continue
            key = str(session_file.resolve())
            current_stats[key] = (int(stat.st_size), float(stat.st_mtime))
            payload = self._matching_payload(session_file)
            if payload is None:
                continue

            payload_session_id = str(payload.get("sessionId") or "").strip()
            if self._tracked_session_id and payload_session_id == self._tracked_session_id:
                matched_session = (session_file, payload)
                continue

            previous = self._known_file_stats.get(key)
            if previous is None:
                new_candidates.append((float(stat.st_mtime), session_file, payload))
                continue
            if previous != current_stats[key]:
                changed_candidates.append((float(stat.st_mtime), session_file, payload))

        self._known_file_stats = current_stats

        if matched_session is not None:
            return matched_session[0], matched_session[1], True

        candidates = new_candidates or changed_candidates
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, session_file, payload = candidates[0]
        return session_file, payload, False

    def _prime_from_payload(self, payload: Dict[str, Any]) -> None:
        self._message_states = {}
        self._process_payload(payload, emit=False)

    def _process_payload(self, payload: Dict[str, Any], *, emit: bool) -> List[GeminiProgressEvent]:
        session_id = str(payload.get("sessionId") or "").strip()
        if session_id:
            if session_id != self._tracked_session_id:
                self._tracked_session_id = session_id
            self._emit_session_id(session_id)

        events: List[GeminiProgressEvent] = []
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return events

        for index, raw_message in enumerate(messages):
            if not isinstance(raw_message, dict):
                continue
            if str(raw_message.get("type") or "").strip().lower() != "gemini":
                continue

            message_key = self._message_key(index, raw_message)
            state = self._message_states.setdefault(message_key, _GeminiMessageState())
            items: List[Tuple[float, int, str]] = []
            message_ts = _parse_timestamp(raw_message.get("timestamp"))

            for thought in list(raw_message.get("thoughts", []) or []):
                if not isinstance(thought, dict):
                    continue
                signature = "|".join(
                    [
                        str(thought.get("timestamp") or "").strip(),
                        str(thought.get("subject") or "").strip(),
                        str(thought.get("description") or "").strip(),
                    ]
                )
                if signature in state.thoughts_seen:
                    continue
                state.thoughts_seen.add(signature)
                text = _thought_text(thought)
                if emit and text:
                    items.append((_parse_timestamp(thought.get("timestamp")), 10, text))

            content = str(raw_message.get("content") or "").strip()
            if content and content != state.last_content:
                state.last_content = content
                if emit:
                    items.append((message_ts, 20, _truncate(content, 200)))

            for tool_index, tool_call in enumerate(list(raw_message.get("toolCalls", []) or [])):
                if not isinstance(tool_call, dict):
                    continue
                tool_key = self._tool_key(tool_index, tool_call)
                tool_state = state.tool_states.setdefault(tool_key, _GeminiToolState())
                tool_ts = _parse_timestamp(tool_call.get("timestamp") or raw_message.get("timestamp"))
                tool_name = _tool_label(tool_call)

                if tool_name and not tool_state.started:
                    tool_state.started = True
                    if emit:
                        preview = _tool_args_preview(tool_call)
                        tool_line = f"🔧 {tool_name}({preview})" if preview else f"🔧 {tool_name}(...)"
                        items.append((tool_ts, 30, tool_line))

                result_signature = "|".join(
                    [
                        str(tool_call.get("status") or "").strip(),
                        str(tool_call.get("resultDisplay") or "").strip(),
                        str(tool_call.get("timestamp") or "").strip(),
                    ]
                )
                result_text = _tool_result_text(tool_call)
                if result_text and result_signature != tool_state.result_signature:
                    tool_state.result_signature = result_signature
                    if emit:
                        items.append((tool_ts, 40, result_text))

            if not emit or not items:
                continue

            items.sort(key=lambda item: (item[0], item[1]))
            progress_items = [text for _, _, text in items if text]
            if progress_items:
                events.append(
                    GeminiProgressEvent(
                        session_id=session_id,
                        timestamp=items[-1][0],
                        progress_items=progress_items,
                        raw=raw_message,
                    )
                )

        return events

    def _attach_tracked_file(self, path: Path, payload: Dict[str, Any], *, prime_existing: bool) -> None:
        self._tracked_json_path = path
        try:
            stat = path.stat()
            self._tracked_file_stat = (int(stat.st_size), float(stat.st_mtime))
        except Exception:
            self._tracked_file_stat = None

        if prime_existing:
            self._prime_from_payload(payload)
            return

        for event in self._process_payload(payload, emit=True):
            if self.callback is None:
                continue
            try:
                self.callback(event)
            except Exception:
                self._log.exception("Gemini monitor callback failed")

    def _poll_tracked_file(self) -> None:
        if self._tracked_json_path is None:
            return
        if not self._tracked_json_path.exists():
            return
        try:
            stat = self._tracked_json_path.stat()
        except Exception:
            return

        current_stat = (int(stat.st_size), float(stat.st_mtime))
        if self._tracked_file_stat == current_stat:
            return

        payload = self._matching_payload(self._tracked_json_path)
        self._tracked_file_stat = current_stat
        if payload is None:
            return

        for event in self._process_payload(payload, emit=True):
            if self.callback is None:
                continue
            try:
                self.callback(event)
            except Exception:
                self._log.exception("Gemini monitor callback failed")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if self._tracked_json_path is None:
                    discovered = self._discover_tracked_file()
                    if discovered is not None:
                        path, payload, prime_existing = discovered
                        self._attach_tracked_file(path, payload, prime_existing=prime_existing)
                else:
                    self._poll_tracked_file()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                self._log.exception("Gemini JSON monitor poll failed")
                await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        if self._tracked_session_id:
            self._emit_session_id(self._tracked_session_id)
        self._log.info("Gemini JSON monitor started for %s", self.workdir)

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._log.info("Gemini JSON monitor stopped")

    def get_latest_session_id(self) -> Optional[str]:
        return self._tracked_session_id
