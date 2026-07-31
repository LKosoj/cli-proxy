from __future__ import annotations

import logging
import os
import pwd
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from modes.sdk.runtime.json_normalizer import loads_safe


logger = logging.getLogger(__name__)

_SUPPORTED_PROVIDERS = {"claude", "codex", "grok"}
_DISCOVERY_TAIL_BYTES = 4 * 1024 * 1024
_DISCOVERY_CANDIDATE_LIMIT = 30
_CODEX_SESSION_ID_RE = re.compile(r"-([0-9a-fA-F-]{36})\.jsonl$")


@dataclass(frozen=True)
class TranscriptLocator:
    provider: str
    path: str
    start_offset: int
    session_id: str = ""


@dataclass(frozen=True)
class TranscriptPollResult:
    assistant_text: str = ""
    progress_text: str = ""
    complete: bool = False
    available: bool = False
    recognized: bool = False
    activity_at: Optional[float] = None
    session_id: str = ""
    locator: Optional[TranscriptLocator] = None


def _event_timestamp(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return time.time()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        logger.debug("structured transcript timestamp is invalid value=%r", text)
        return time.time()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        return str(content.get("text") or "").strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "text":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


class CliTranscriptReader:
    def __init__(
        self,
        *,
        cli_name: str,
        request_id: str,
        workdir: str,
        started_at: float,
        username: str = "",
        session_id: str = "",
        home_dir: Optional[Path] = None,
        locator: Optional[TranscriptLocator] = None,
    ):
        self.provider = str(cli_name or "").strip().lower()
        self.request_id = str(request_id or "").strip()
        self.workdir = os.path.realpath(str(workdir or ""))
        self.started_at = float(started_at)
        self.session_id = str(session_id or "").strip()
        self.home_dir = Path(home_dir) if home_dir is not None else self._resolve_home(username)
        self.root = self._provider_root()
        self.locator: Optional[TranscriptLocator] = None
        self.cursor = 0
        self.latest_assistant_text = ""
        self.latest_progress_text = ""
        self.complete = False
        self.recognized = False
        self.activity_at: Optional[float] = None
        self._grok_stream_key = ""
        self._grok_stream_chunks: list[str] = []
        self._grok_stream_raw = ""
        if locator is not None:
            self._restore_locator(locator)

    @staticmethod
    def _resolve_home(username: str) -> Path:
        name = str(username or "").strip()
        if not name:
            return Path.home()
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except KeyError:
            logger.warning("structured transcript user does not exist username=%s", name)
            return Path.home()

    def _provider_root(self) -> Path:
        if self.provider == "claude":
            return self.home_dir / ".claude" / "projects"
        if self.provider == "codex":
            return self.home_dir / ".codex" / "sessions"
        if self.provider == "grok":
            return self.home_dir / ".grok" / "sessions"
        return self.home_dir / ".cli-proxy-unsupported-transcript"

    def _path_is_allowed(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _restore_locator(self, locator: TranscriptLocator) -> None:
        if str(locator.provider or "").strip().lower() != self.provider:
            return
        path = Path(str(locator.path or ""))
        if not self._path_is_allowed(path):
            logger.warning("structured transcript locator rejected path=%s provider=%s", path, self.provider)
            return
        self.locator = TranscriptLocator(
            provider=self.provider,
            path=str(path.resolve()),
            start_offset=max(0, int(locator.start_offset)),
            session_id=str(locator.session_id or self.session_id).strip(),
        )
        self.cursor = self.locator.start_offset
        if self.locator.session_id:
            self.session_id = self.locator.session_id

    def _candidate_paths(self) -> list[Path]:
        if self.provider not in _SUPPORTED_PROVIDERS or not self.root.is_dir():
            return []

        exact: list[Path] = []
        if self.session_id:
            if self.provider == "claude":
                exact = list(self.root.rglob(f"{self.session_id}.jsonl"))
            elif self.provider == "codex":
                exact = list(self.root.rglob(f"*-{self.session_id}.jsonl"))
            elif self.provider == "grok":
                workspace_key = urllib.parse.quote(self.workdir, safe="")
                direct = self.root / workspace_key / self.session_id / "updates.jsonl"
                if direct.is_file():
                    exact = [direct]
        if exact:
            return exact

        pattern = "updates.jsonl" if self.provider == "grok" else "*.jsonl"
        candidates: list[tuple[float, Path]] = []
        try:
            for path in self.root.rglob(pattern):
                try:
                    stat_result = path.stat()
                except OSError:
                    logger.debug("structured transcript candidate stat failed path=%s", path, exc_info=True)
                    continue
                if stat_result.st_mtime < self.started_at - 30:
                    continue
                candidates.append((float(stat_result.st_mtime), path))
        except OSError:
            logger.exception("structured transcript discovery failed root=%s", self.root)
            return []
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in candidates[:_DISCOVERY_CANDIDATE_LIMIT]]

    def _marker_offset(self, path: Path) -> Optional[int]:
        marker = f"<<<CLI_PROXY_REQUEST:{self.request_id}>>>".encode("utf-8")
        try:
            size = path.stat().st_size
            start = max(0, size - _DISCOVERY_TAIL_BYTES)
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:
            logger.debug("structured transcript marker read failed path=%s", path, exc_info=True)
            return None
        marker_index = data.rfind(marker)
        if marker_index < 0:
            return None
        line_start = data.rfind(b"\n", 0, marker_index) + 1
        return start + line_start

    def _session_id_from_path(self, path: Path) -> str:
        if self.provider == "claude":
            return path.stem
        if self.provider == "grok":
            return path.parent.name
        match = _CODEX_SESSION_ID_RE.search(path.name)
        return match.group(1) if match else ""

    def _discover(self) -> None:
        for path in self._candidate_paths():
            offset = self._marker_offset(path)
            if offset is None:
                continue
            session_id = self._session_id_from_path(path) or self.session_id
            self.locator = TranscriptLocator(
                provider=self.provider,
                path=str(path.resolve()),
                start_offset=offset,
                session_id=session_id,
            )
            self.cursor = offset
            if session_id:
                self.session_id = session_id
            return

    def _done_marker(self) -> str:
        return f"<<<DONE:{self.request_id}>>>"

    def _clean_assistant_text(self, text: str) -> str:
        return str(text or "").replace(self._done_marker(), "").strip()

    def _handle_claude(self, record: dict[str, Any]) -> None:
        event_type = str(record.get("type") or "").strip()
        if event_type in {"user", "assistant", "system", "progress"}:
            self.recognized = True
        self.activity_at = _event_timestamp(record.get("timestamp"))
        if event_type == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            text = _content_text(content)
            if text:
                if self._done_marker() in text:
                    self.complete = True
                cleaned_text = self._clean_assistant_text(text)
                if cleaned_text:
                    self.latest_assistant_text = cleaned_text
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or str(item.get("type") or "").strip() != "tool_use":
                        continue
                    tool_name = str(item.get("name") or "").strip()
                    if tool_name:
                        self.latest_progress_text = tool_name
        # system/turn_duration only marks a turn end: monitoring continues until the DONE marker,
        # because background agents may keep working after it.

    def _handle_codex(self, record: dict[str, Any]) -> None:
        if str(record.get("type") or "").strip() != "event_msg":
            return
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        event_type = str(payload.get("type") or "").strip()
        if event_type in {"user_message", "agent_message", "task_started", "task_complete", "token_count"}:
            self.recognized = True
        self.activity_at = _event_timestamp(record.get("timestamp"))
        if event_type == "agent_message":
            text = str(payload.get("message") or "").strip()
            if text:
                self.latest_assistant_text = self._clean_assistant_text(text)
        elif event_type == "task_complete":
            text = str(payload.get("last_agent_message") or "").strip()
            if text:
                self.latest_assistant_text = self._clean_assistant_text(text)
            # Only mark complete if DONE marker seen (for long-running tasks wrapped with explicit completion protocol).
            # This keeps monitoring active across turns for all CLIs until the AI prints the marker.
            if self._done_marker() in (text or ""):
                self.complete = True
            # else: intermediate task complete, keep complete=False so outer monitor can continue

    def _handle_grok(self, record: dict[str, Any]) -> None:
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        event_type = str(update.get("sessionUpdate") or "").strip()
        if event_type:
            self.recognized = True
        self.activity_at = _event_timestamp(record.get("timestamp"))
        if event_type == "tool_call":
            title = str(update.get("title") or "").strip()
            if title:
                self.latest_progress_text = title
        elif event_type == "agent_message_chunk":
            text = _content_text(update.get("content"))
            if text:
                metadata = update.get("_meta") if isinstance(update.get("_meta"), dict) else params.get("_meta")
                metadata = metadata if isinstance(metadata, dict) else {}
                stream_key = str(metadata.get("streamStartMs") or metadata.get("promptId") or "default")
                if stream_key != self._grok_stream_key:
                    self._grok_stream_key = stream_key
                    self._grok_stream_chunks = []
                self._grok_stream_chunks.append(text)
                self._grok_stream_raw = "".join(self._grok_stream_chunks)
                self.latest_assistant_text = self._clean_assistant_text(self._grok_stream_raw)
        elif event_type == "turn_completed":
            # Only mark complete if DONE marker seen (consistent with other CLIs and long-running protocol).
            # The raw stream is checked because latest_assistant_text has the marker stripped already.
            if self._done_marker() in (self._grok_stream_raw or ""):
                self.complete = True
            # else keep monitoring until explicit marker for this request

    def _handle_record(self, record: dict[str, Any]) -> None:
        if self.provider == "claude":
            self._handle_claude(record)
        elif self.provider == "codex":
            self._handle_codex(record)
        elif self.provider == "grok":
            self._handle_grok(record)

    def _read_new_records(self) -> None:
        if self.locator is None:
            return
        path = Path(self.locator.path)
        try:
            size = path.stat().st_size
            if self.cursor > size:
                self.cursor = self.locator.start_offset if self.locator.start_offset <= size else 0
            with path.open("rb") as handle:
                handle.seek(self.cursor)
                data = handle.read()
        except OSError:
            logger.debug("structured transcript read failed path=%s", path, exc_info=True)
            return
        if not data:
            return

        consumed = 0
        lines = data.splitlines(keepends=True)
        for index, raw_line in enumerate(lines):
            terminated = raw_line.endswith((b"\n", b"\r"))
            try:
                text = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                if index == len(lines) - 1 and not terminated:
                    break
                logger.debug("structured transcript line decode failed path=%s", path)
                consumed += len(raw_line)
                continue
            if not text:
                consumed += len(raw_line)
                continue
            try:
                record = loads_safe(text, strict_first=True)
            except Exception:
                if index == len(lines) - 1 and not terminated:
                    break
                logger.debug("structured transcript JSONL record skipped path=%s", path, exc_info=True)
                consumed += len(raw_line)
                continue
            consumed += len(raw_line)
            if isinstance(record, dict):
                self._handle_record(record)
        self.cursor += consumed

    def get_all_relevant_paths(self) -> list[str]:
        """Return paths to all transcripts that should be considered for liveness.

        For Claude this includes the main session JSONL plus any subagent
        transcripts under <session_id>/subagents/*.jsonl. Used to prevent
        quiet-timeout while background/sub-agents are still writing.
        """
        paths: list[str] = []
        if self.locator and self.locator.path:
            paths.append(self.locator.path)

        if self.provider != "claude" or not self.session_id:
            return paths

        try:
            if not self.root.is_dir():
                return paths

            session = self.session_id
            # Try to locate the subagents directory relative to a known main transcript
            sub_dirs: list[Path] = []
            for main_p in self.root.rglob(f"{session}.jsonl"):
                cand = main_p.parent / session / "subagents"
                if cand.is_dir():
                    sub_dirs.append(cand)

            if not sub_dirs:
                # Fallback: search for subagents dir directly (works even if main .jsonl not indexed yet)
                for cand in self.root.glob(f"**/{session}/subagents"):
                    if cand.is_dir():
                        sub_dirs.append(cand)

            for sub_dir in sub_dirs:
                for sub_file in sorted(sub_dir.glob("*.jsonl")):
                    sp = str(sub_file.resolve())
                    if sp not in paths:
                        paths.append(sp)
        except Exception:
            logger.debug("get_all_relevant_paths failed to collect subagent transcripts", exc_info=True)

        return paths

    def poll(self) -> TranscriptPollResult:
        if self.provider not in _SUPPORTED_PROVIDERS or not self.request_id:
            return TranscriptPollResult()
        if self.locator is None:
            self._discover()
        self._read_new_records()
        return TranscriptPollResult(
            assistant_text=self.latest_assistant_text,
            progress_text=self.latest_progress_text,
            complete=self.complete,
            available=self.locator is not None,
            recognized=self.recognized,
            activity_at=self.activity_at,
            session_id=self.session_id if self.locator is not None else "",
            locator=self.locator,
        )
