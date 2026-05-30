"""
Monitor Claude Code transcripts in real time.

Claude Code stores local transcripts under:
  ~/.claude/projects/<project_key>/<session_id>.jsonl

This monitor follows the active transcript file for the current workdir,
extracts progress-like events, and exposes the detected session id so the
caller can persist it as the resume token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from modes.sdk.runtime.json_normalizer import loads_safe

logger = logging.getLogger(__name__)


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


def _tool_input_preview(tool_name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return _truncate(str(payload or ""), 120)

    tool = str(tool_name or "").strip().lower()
    file_path = str(
        payload.get("file_path")
        or payload.get("filePath")
        or payload.get("absolute_path")
        or payload.get("path")
        or payload.get("directory")
        or ""
    ).strip()
    pattern = str(payload.get("pattern") or "").strip()
    command = str(payload.get("command") or "").strip()
    query = str(payload.get("query") or payload.get("prompt") or "").strip()
    url = str(payload.get("url") or "").strip()

    if tool in {"read", "write", "edit", "multiedit"} and file_path:
        start_line = payload.get("start_line")
        end_line = payload.get("end_line")
        line_suffix = ""
        if start_line and end_line:
            line_suffix = f":{start_line}-{end_line}"
        elif start_line:
            line_suffix = f":{start_line}"
        return _truncate(f"{_short_path(file_path)}{line_suffix}", 120)
    if tool in {"glob", "grep"} and pattern:
        if file_path:
            return _truncate(f"{pattern} @ {_short_path(file_path)}", 120)
        return _truncate(pattern, 120)
    if tool == "bash" and command:
        return _truncate(command, 120)
    if tool in {"websearch", "webfetch"}:
        target = query or url
        if target:
            return _truncate(target, 120)

    for candidate in (command, pattern, query, url):
        if candidate:
            return _truncate(candidate, 120)
    if file_path:
        return _truncate(_short_path(file_path), 120)

    for key in ("description", "question", "task", "mode"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _truncate(value, 120)
    return ""


def _format_tool_label(tool_name: str, payload: Any) -> str:
    name = str(tool_name or "").strip() or "Tool"
    preview = _tool_input_preview(name, payload)
    if preview:
        return f"{name}({preview})"
    return f"{name}(...)"


def _extract_tool_result_text(raw_content: Any) -> str:
    if isinstance(raw_content, str):
        return _truncate(raw_content, 180)
    if isinstance(raw_content, list):
        parts: List[str] = []
        for item in raw_content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
                if text:
                    parts.append(text)
        return _truncate(" ".join(parts), 180)
    return ""


def _format_hook_progress_text(hook_name: str, tool_label: str) -> str:
    hook = str(hook_name or "").strip()
    label = str(tool_label or "").strip()
    if not hook:
        return f"⏳ {label}" if label else ""
    if not label:
        return f"⏳ {hook}"
    if ":" in hook:
        prefix, tool_name = hook.rsplit(":", 1)
        if label.startswith(f"{tool_name}("):
            return f"⏳ {prefix}:{label}"
    return f"⏳ {hook} {label}"


def _parse_timestamp(raw: Any) -> float:
    text = str(raw or "").strip()
    if not text:
        return time.time()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def _summarize_tool_result(payload: Any, *, raw_text: str = "") -> str:
    if not isinstance(payload, dict):
        return _truncate(raw_text, 140) if raw_text else ""

    num_files = payload.get("numFiles")
    filenames = payload.get("filenames")
    try:
        if num_files is not None:
            count = int(num_files)
            if count > 0:
                if isinstance(filenames, list) and filenames:
                    shown = [
                        _short_path(item)
                        for item in filenames[: min(3, len(filenames))]
                        if str(item or "").strip()
                    ]
                    if shown:
                        preview = " ".join(shown)
                        suffix = " ..." if count > len(shown) else ""
                        return _truncate(f"Found {count} files {preview}{suffix}", 140)
                return f"Found {count} files"
    except Exception:
        pass

    file_obj = payload.get("file")
    if isinstance(file_obj, dict):
        file_path = str(file_obj.get("filePath") or "").strip()
        if file_path:
            preview = _truncate(file_obj.get("content") or raw_text, 120)
            if preview:
                return f"Read {_short_path(file_path)}: {preview}"
            return f"Read {_short_path(file_path)}"

    file_path = str(payload.get("filePath") or "").strip()
    if file_path:
        action_type = str(payload.get("type") or "").strip().lower()
        if action_type == "create":
            return f"Wrote {_short_path(file_path)}"
        if action_type in {"update", "edit"}:
            return f"Updated {_short_path(file_path)}"
        preview = _truncate(raw_text or payload.get("content") or "", 120)
        if preview:
            return f"Read {_short_path(file_path)}: {preview}"
        return f"Read {_short_path(file_path)}"

    stdout = _truncate(payload.get("stdout") or "", 120)
    stderr = _truncate(payload.get("stderr") or "", 120)
    if stdout:
        return stdout
    if stderr:
        return stderr

    if payload.get("noOutputExpected") is True:
        return "Completed"

    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return _truncate(content, 140)

    return _truncate(raw_text, 140) if raw_text else ""


def extract_progress_texts(raw_event: Dict[str, Any]) -> List[str]:
    """Extract human-readable progress messages from a Claude transcript event."""
    event_type = str(raw_event.get("type") or "").strip()
    out: List[str] = []

    if event_type == "assistant":
        message = raw_event.get("message", {})
        content = message.get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").strip()
                if item_type == "tool_use":
                    tool_name = str(item.get("name") or "").strip()
                    if tool_name:
                        out.append(f"🔧 {_format_tool_label(tool_name, item.get('input'))}")
                elif item_type == "thinking":
                    thought = str(item.get("thinking") or "").strip()
                    if thought:
                        out.append(f"🤔 {_truncate(thought, 200)}")
                elif item_type == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        out.append(_truncate(text, 200))
        return out

    if event_type == "user":
        message = raw_event.get("message", {})
        content = message.get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip() != "tool_result":
                    continue
                text = _summarize_tool_result(
                    raw_event.get("toolUseResult"),
                    raw_text=_extract_tool_result_text(item.get("content")),
                )
                if text:
                    status_icon = "❌" if bool(item.get("is_error")) else "✅"
                    out.append(f"{status_icon} {text}")
        if out:
            return out

        fallback = _summarize_tool_result(raw_event.get("toolUseResult"))
        if fallback:
            return [f"✅ {fallback}"]
        return []

    if event_type == "progress":
        data = raw_event.get("data", {})
        hook_name = str(data.get("hookName") or "").strip()
        if hook_name:
            return [f"⏳ {hook_name}"]
        if str(data.get("type") or "").strip() == "agent_progress":
            nested_message = data.get("message")
            if isinstance(nested_message, dict):
                role = str(nested_message.get("role") or "").strip()
                if role in {"assistant", "user"}:
                    nested_event: Dict[str, Any] = {
                        "type": role,
                        "message": nested_message,
                    }
                    tool_result = data.get("toolUseResult")
                    if tool_result is not None:
                        nested_event["toolUseResult"] = tool_result
                    nested_items = extract_progress_texts(nested_event)
                    if nested_items:
                        return nested_items
            status = str(data.get("status") or "").strip()
            if status:
                return [f"⏳ {status}"]

    return []


@dataclass
class ClaudeTranscriptEvent:
    session_id: str
    timestamp: float
    event_type: str
    uuid: Optional[str] = None
    progress_items: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_jsonl_line(cls, line: str) -> Optional["ClaudeTranscriptEvent"]:
        try:
            data = loads_safe(line.strip(), strict_first=True)
        except Exception:
            return None

        session_id = str(data.get("sessionId") or "").strip()
        event_type = str(data.get("type") or "").strip()
        uuid_raw = str(data.get("uuid") or "").strip()

        return cls(
            session_id=session_id,
            timestamp=_parse_timestamp(data.get("timestamp")),
            event_type=event_type,
            uuid=uuid_raw or None,
            progress_items=extract_progress_texts(data),
            raw=data,
        )


@dataclass
class ClaudeSessionMonitor:
    session_id: str
    jsonl_path: Path
    last_position: int = 0
    seen_uuids: Set[str] = field(default_factory=set)
    last_read_time: float = 0.0
    callback: Optional[Callable[[ClaudeTranscriptEvent], None]] = None
    tool_use_labels: Dict[str, str] = field(default_factory=dict)

    def _remember_tool_labels(self, event: ClaudeTranscriptEvent) -> None:
        raw = event.raw if isinstance(event.raw, dict) else {}
        message = raw.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip() != "tool_use":
                continue
            tool_use_id = str(item.get("id") or "").strip()
            tool_name = str(item.get("name") or "").strip()
            if tool_use_id and tool_name:
                self.tool_use_labels[tool_use_id] = _format_tool_label(tool_name, item.get("input"))

    def _enrich_progress_items(self, event: ClaudeTranscriptEvent) -> None:
        if not event.progress_items:
            return
        raw = event.raw if isinstance(event.raw, dict) else {}
        if str(raw.get("type") or "").strip() != "progress":
            return
        data = raw.get("data", {})
        if not isinstance(data, dict):
            return
        hook_name = str(data.get("hookName") or "").strip()
        tool_use_id = str(raw.get("toolUseID") or raw.get("parentToolUseID") or "").strip()
        tool_label = self.tool_use_labels.get(tool_use_id)
        if hook_name and tool_label:
            event.progress_items = [_format_hook_progress_text(hook_name, tool_label)]

    def read_new_events(self) -> List[ClaudeTranscriptEvent]:
        events: List[ClaudeTranscriptEvent] = []
        if not self.jsonl_path.exists():
            return events

        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as handle:
                handle.seek(self.last_position)
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue

                    event = ClaudeTranscriptEvent.from_jsonl_line(line)
                    if event is None:
                        continue
                    self._remember_tool_labels(event)
                    self._enrich_progress_items(event)
                    if event.uuid and event.uuid in self.seen_uuids:
                        continue
                    if event.uuid:
                        self.seen_uuids.add(event.uuid)
                    events.append(event)
                    if self.callback is not None:
                        try:
                            self.callback(event)
                        except Exception:
                            logger.exception("Claude monitor callback failed")

                self.last_position = handle.tell()
                self.last_read_time = time.time()
        except Exception:
            logger.exception("Failed to read Claude transcript")

        return events


class ClaudeJsonlMonitor:
    """
    Track one active Claude transcript file for the current workdir.

    The monitor intentionally latches onto the first relevant transcript that
    appears or grows after the monitor starts. This avoids mixing history from
    older unrelated sessions in the same project directory.
    """

    def __init__(
        self,
        workdir: str,
        callback: Optional[Callable[[ClaudeTranscriptEvent], None]] = None,
        session_callback: Optional[Callable[[str], None]] = None,
        poll_interval: float = 0.3,
        username: str = "claude-bot",
        session_id: Optional[str] = None,
    ):
        self.workdir = workdir
        self.callback = callback
        self.session_callback = session_callback
        self.poll_interval = poll_interval
        self.username = username
        self.monitors: Dict[str, ClaudeSessionMonitor] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._log = logging.getLogger(self.__class__.__name__)
        self._tracked_transcript_id: Optional[str] = str(session_id or "").strip() or None
        self._known_file_sizes: Dict[str, int] = self._snapshot_known_files()

    def _emit_session_id(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid or self.session_callback is None:
            return
        try:
            self.session_callback(sid)
        except Exception:
            self._log.exception("Claude session callback failed")

    def _project_key_candidates(self) -> List[str]:
        raw = os.path.realpath(self.workdir).rstrip(os.sep) or self.workdir
        compact_key = re.sub(r"[^A-Za-z0-9]+", "-", raw)
        if raw.startswith(os.sep) and not compact_key.startswith("-"):
            compact_key = "-" + compact_key
        compact_key = compact_key.rstrip("-")
        return [compact_key] if compact_key else []

    def _candidate_project_roots(self) -> List[Path]:
        roots: List[Path] = []
        seen: Set[str] = set()

        for home in (Path.home(), self._home_for_user(self.username)):
            if home is None:
                continue
            root = home / ".claude" / "projects"
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)

        return roots

    @staticmethod
    def _home_for_user(username: str) -> Optional[Path]:
        name = str(username or "").strip()
        if not name:
            return None
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except Exception:
            return None

    def _candidate_project_dirs(self) -> List[Path]:
        dirs: List[Path] = []
        seen: Set[str] = set()
        keys = self._project_key_candidates()
        if not keys:
            return dirs

        for root in self._candidate_project_roots():
            for key in keys:
                candidate = root / key
                text = str(candidate)
                if text in seen:
                    continue
                seen.add(text)
                dirs.append(candidate)
        return dirs

    def _snapshot_known_files(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for project_dir in self._candidate_project_dirs():
            if not project_dir.is_dir():
                continue
            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    out[str(jsonl_file.resolve())] = int(jsonl_file.stat().st_size)
                except Exception:
                    continue
        return out

    def _find_project_dir(self) -> Optional[Path]:
        tracked_transcript_id = str(self._tracked_transcript_id or "").strip()
        if tracked_transcript_id:
            for candidate in self._candidate_project_dirs():
                if not candidate.is_dir():
                    continue
                if (candidate / f"{tracked_transcript_id}.jsonl").exists():
                    return candidate
                if (candidate / tracked_transcript_id).is_dir():
                    return candidate

        for candidate in self._candidate_project_dirs():
            if candidate.is_dir():
                return candidate

        workdir = os.path.realpath(self.workdir)
        for root in self._candidate_project_roots():
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    newest = max(child.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                except ValueError:
                    continue
                except Exception:
                    continue
                try:
                    preview = newest.read_text(encoding="utf-8", errors="ignore")[:4096]
                except Exception:
                    continue
                if workdir and workdir in preview:
                    return child
        return None

    def _discover_active_session(self, project_dir: Path) -> None:
        if self._tracked_transcript_id:
            return

        new_candidates: List[tuple[float, Path, int]] = []
        grown_candidates: List[tuple[float, Path, int]] = []
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                path_key = str(jsonl_file.resolve())
                current_size = int(jsonl_file.stat().st_size)
                current_mtime = float(jsonl_file.stat().st_mtime)
            except Exception:
                continue

            previous_size = self._known_file_sizes.get(path_key)
            if previous_size is None:
                new_candidates.append((current_mtime, jsonl_file, 0))
                continue
            if current_size > previous_size:
                grown_candidates.append((current_mtime, jsonl_file, previous_size))

        candidates = new_candidates or grown_candidates
        if not candidates:
            return

        candidates.sort(key=lambda item: item[0], reverse=True)
        _, jsonl_file, initial_offset = candidates[0]
        session_id = jsonl_file.stem
        monitor = ClaudeSessionMonitor(
            session_id=session_id,
            jsonl_path=jsonl_file,
            last_position=max(0, int(initial_offset)),
            callback=self.callback,
        )
        self.monitors[session_id] = monitor
        self._tracked_transcript_id = session_id
        self._known_file_sizes[str(jsonl_file.resolve())] = int(jsonl_file.stat().st_size)
        self._log.info("Discovered Claude session: %s", session_id)
        self._emit_session_id(session_id)

    @staticmethod
    def _subagent_monitor_key(session_id: str, path: Path) -> str:
        return f"{session_id}:subagent:{path.stem}"

    def _ensure_session_monitors(self, project_dir: Path) -> None:
        session_id = str(self._tracked_transcript_id or "").strip()
        if not session_id:
            return

        root_path = project_dir / f"{session_id}.jsonl"
        if root_path.exists() and session_id not in self.monitors:
            path_key = str(root_path.resolve())
            initial_offset = int(self._known_file_sizes.get(path_key, 0))
            self.monitors[session_id] = ClaudeSessionMonitor(
                session_id=session_id,
                jsonl_path=root_path,
                last_position=max(0, initial_offset),
                callback=self.callback,
            )
            self._known_file_sizes[path_key] = int(root_path.stat().st_size)
            self._log.info("Attached Claude root transcript: %s", root_path)

        subagents_dir = project_dir / session_id / "subagents"
        if not subagents_dir.is_dir():
            return

        for jsonl_file in sorted(subagents_dir.glob("*.jsonl")):
            key = self._subagent_monitor_key(session_id, jsonl_file)
            if key in self.monitors:
                continue
            path_key = str(jsonl_file.resolve())
            initial_offset = int(self._known_file_sizes.get(path_key, 0))
            self.monitors[key] = ClaudeSessionMonitor(
                session_id=session_id,
                jsonl_path=jsonl_file,
                last_position=max(0, initial_offset),
                callback=self.callback,
            )
            self._known_file_sizes[path_key] = int(jsonl_file.stat().st_size)
            self._log.info("Attached Claude subagent transcript: %s", jsonl_file)

    def _poll_sync(self) -> None:
        """Synchronous poll step: discover sessions and read new events."""
        project_dir = self._find_project_dir()
        if project_dir is not None:
            self._discover_active_session(project_dir)
            self._ensure_session_monitors(project_dir)
            for monitor in list(self.monitors.values()):
                monitor.read_new_events()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self._poll_sync)
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                self._log.exception("Claude JSONL monitor poll failed")
                await asyncio.sleep(self.poll_interval)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        if self._tracked_transcript_id:
            self._emit_session_id(self._tracked_transcript_id)
        self._log.info("Claude JSONL monitor started for %s", self.workdir)

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._log.info("Claude JSONL monitor stopped")

    def get_latest_session_id(self) -> Optional[str]:
        return self._tracked_transcript_id
