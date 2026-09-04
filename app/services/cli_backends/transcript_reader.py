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
from typing import Any, Iterable, Optional

from app.services.session_transfer.reader_claude import _project_key as _claude_project_key
from app.services.session_transfer.reader_kimi import _workspace_key as _kimi_workspace_key
from app.services.session_transfer.reader_qwen import _project_key_candidates as _qwen_project_keys
from modes.sdk.runtime.json_normalizer import loads_safe


logger = logging.getLogger(__name__)

_SUPPORTED_PROVIDERS = {"claude", "codex", "grok", "qwen", "gemini", "kimi"}
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


def _parse_timestamp(raw: Any) -> Optional[float]:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        logger.debug("structured transcript timestamp is invalid value=%r", text)
        return None


def _event_timestamp(raw: Any) -> float:
    stamp = _parse_timestamp(raw)
    return stamp if stamp is not None else time.time()


def _kimi_activity(raw: Any) -> float:
    """Kimi stamps every wire record in epoch milliseconds, unlike the other CLIs."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw) / 1000.0
    return time.time()


def _content_text(content: Any, *, strip: bool = True) -> str:
    """Текст сообщения CLI.

    strip=False нужен стриминговым чанкам: они склеиваются встык, поэтому
    пробелы по краям значимы — иначе «Grok » + «готов» даёт «Grokготов».
    """
    if isinstance(content, str):
        return content.strip() if strip else content
    if isinstance(content, dict):
        text = str(content.get("text") or "")
        return text.strip() if strip else text
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "text":
            continue
        text = str(item.get("text") or "")
        if strip:
            text = text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


class CliTranscriptReader:
    def __init__(
        self,
        *,
        cli_name: str,
        workdir: str,
        started_at: float,
        username: str = "",
        session_id: str = "",
        home_dir: Optional[Path] = None,
        locator: Optional[TranscriptLocator] = None,
    ):
        self.provider = str(cli_name or "").strip().lower()
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
        self._kimi_step_parts: list[str] = []
        self._gemini_turn_end: tuple[str, str] = ("", "")
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
        if self.provider == "qwen":
            return self.home_dir / ".qwen" / "projects"
        if self.provider == "gemini":
            return self.home_dir / ".gemini" / "tmp"
        if self.provider == "kimi":
            return self.home_dir / ".kimi-code" / "sessions"
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

    def _exact_paths(self) -> list[Path]:
        """Журналы треда, найденные по известному идентификатору сессии."""
        if self.provider not in _SUPPORTED_PROVIDERS or not self.session_id or not self.root.is_dir():
            return []
        if self.provider in {"claude", "qwen"}:
            return list(self.root.rglob(f"{self.session_id}.jsonl"))
        if self.provider == "codex":
            return list(self.root.rglob(f"*-{self.session_id}.jsonl"))
        if self.provider == "gemini":
            return list(self.root.rglob(f"session-{self.session_id}.json"))
        if self.provider == "grok":
            workspace_key = urllib.parse.quote(self.workdir, safe="")
            direct = self.root / workspace_key / self.session_id / "updates.jsonl"
            return [direct] if direct.is_file() else []
        direct = (
            self.root
            / _kimi_workspace_key(self.workdir)
            / self.session_id
            / "agents"
            / "main"
            / "wire.jsonl"
        )
        return [direct] if direct.is_file() else []

    def _workspace_dirs(self) -> list[Path]:
        """Каталоги журналов провайдера, закреплённые за рабочей директорией.

        У codex и gemini путь журнала workdir не кодирует, поэтому для них
        принадлежность проверяется по полю cwd внутри самого журнала.
        """
        if not self.workdir:
            return []
        if self.provider == "claude":
            return [self.root / _claude_project_key(self.workdir)]
        if self.provider == "qwen":
            return [self.root / key / "chats" for key in _qwen_project_keys(self.workdir)]
        if self.provider == "grok":
            return [self.root / urllib.parse.quote(self.workdir, safe="")]
        if self.provider == "kimi":
            return [self.root / _kimi_workspace_key(self.workdir)]
        return []

    def _matches_workdir(self, path: Path) -> bool:
        if not self.workdir or self.provider not in {"codex", "gemini"}:
            return True
        cwd = ""
        try:
            if self.provider == "gemini":
                payload = loads_safe(path.read_text(encoding="utf-8", errors="replace"), strict_first=True)
                cwd = str(payload.get("cwd") or "").strip() if isinstance(payload, dict) else ""
            else:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for _ in range(5):
                        line = handle.readline()
                        if not line:
                            break
                        record = loads_safe(line.strip(), strict_first=True)
                        if not isinstance(record, dict) or record.get("type") != "session_meta":
                            continue
                        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                        cwd = str(payload.get("cwd") or "").strip()
                        break
        except Exception:
            logger.debug("structured transcript workdir probe failed path=%s", path, exc_info=True)
            return False
        return not cwd or os.path.realpath(cwd) == self.workdir

    def _recent_paths(self) -> list[Path]:
        """Свежие журналы провайдера — когда тред по идентификатору не найден."""
        if self.provider not in _SUPPORTED_PROVIDERS:
            return []

        if self.provider == "grok":
            pattern = "updates.jsonl"
        elif self.provider == "kimi":
            pattern = "wire.jsonl"
        elif self.provider == "gemini":
            pattern = "session-*.json"
        else:
            pattern = "*.jsonl"
        # Каталог рабочей директории — исчерпывающий ответ: если журнала там нет,
        # его нет и вовсе, а поиск по всему корню подцепил бы чужой проект.
        roots = self._workspace_dirs() or [self.root]
        candidates: list[tuple[float, Path]] = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for path in root.rglob(pattern):
                    try:
                        stat_result = path.stat()
                    except OSError:
                        logger.debug("structured transcript candidate stat failed path=%s", path, exc_info=True)
                        continue
                    if stat_result.st_mtime < self.started_at - 30:
                        continue
                    candidates.append((float(stat_result.st_mtime), path))
            except OSError:
                logger.exception("structured transcript discovery failed root=%s", root)
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in candidates[:_DISCOVERY_CANDIDATE_LIMIT]]

    def _line_timestamp(self, raw_line: bytes) -> Optional[float]:
        try:
            text = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        if not text:
            return None
        try:
            record = loads_safe(text, strict_first=True)
        except Exception:
            return None
        if not isinstance(record, dict):
            return None
        if self.provider == "kimi":
            # Kimi пишет время в миллисекундах эпохи, остальные CLI — в ISO.
            raw = record.get("time")
            stamp = _parse_timestamp(raw)
            return stamp / 1000.0 if stamp and stamp > 0 else None
        return _parse_timestamp(record.get("timestamp"))

    def _start_offset(self, path: Path) -> Optional[int]:
        """Смещение первой записи журнала, появившейся после старта запроса.

        Границу хода задаёт время: запись с меткой раньше started_at относится к
        прошлому ходу, всё после неё — к текущему.
        """
        try:
            size = path.stat().st_size
            start = max(0, size - _DISCOVERY_TAIL_BYTES)
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:
            logger.debug("structured transcript tail read failed path=%s", path, exc_info=True)
            return None
        if start:
            # Хвост мог начаться посреди строки — она достанется прошлому ходу.
            head = data.find(b"\n")
            if head < 0:
                return None
            start += head + 1
            data = data[head + 1:]
        offset = None
        position = start + len(data)
        for raw_line in reversed(data.splitlines(keepends=True)):
            position -= len(raw_line)
            stamp = self._line_timestamp(raw_line)
            if stamp is None:
                continue
            if stamp < self.started_at:
                break
            offset = position
        return offset

    def _session_id_from_path(self, path: Path) -> str:
        if self.provider in {"claude", "qwen"}:
            return path.stem
        if self.provider == "grok":
            return path.parent.name
        if self.provider == "kimi":
            # <sessions>/<workspace key>/<session id>/agents/<agent id>/wire.jsonl
            parents = path.parents
            return parents[2].name if len(parents) > 2 else ""
        if self.provider == "gemini":
            return path.stem[len("session-"):] if path.stem.startswith("session-") else path.stem
        match = _CODEX_SESSION_ID_RE.search(path.name)
        return match.group(1) if match else ""

    def _attach(self, path: Path, offset: int) -> None:
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

    def _attach_new_records(self, paths: Iterable[Path]) -> bool:
        for path in paths:
            if not self._matches_workdir(path):
                continue
            offset = self._start_offset(path)
            if offset is not None:
                self._attach(path, offset)
                return True
        return False

    def _discover(self) -> None:
        if self.provider == "gemini":
            # Снимок сессии переписывается целиком, поэтому смещение не нужно:
            # границу хода задаёт время сообщений при каждом чтении.
            for path in (*self._exact_paths(), *self._recent_paths()):
                if self._matches_workdir(path):
                    self._attach(path, 0)
                    return
            return
        if self._attach_new_records(self._exact_paths()):
            return
        # Тред мог уехать в другой журнал: CLI умеет продолжить его новым файлом.
        self._attach_new_records(self._recent_paths())

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
                self.latest_assistant_text = text
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or str(item.get("type") or "").strip() != "tool_use":
                        continue
                    tool_name = str(item.get("name") or "").strip()
                    if tool_name:
                        self.latest_progress_text = tool_name
        elif event_type == "system" and str(record.get("subtype") or "").strip() == "turn_duration":
            self.complete = True

    def _handle_codex(self, record: dict[str, Any]) -> None:
        record_type = str(record.get("type") or "").strip()
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record_type == "response_item":
            if str(payload.get("type") or "").strip() != "message":
                return
            if str(payload.get("role") or "").strip() != "assistant":
                return
            text = "\n".join(
                str(item.get("text") or "").strip()
                for item in payload.get("content", [])
                if isinstance(item, dict)
                and str(item.get("type") or "").strip() == "output_text"
                and str(item.get("text") or "").strip()
            )
            if text:
                self.recognized = True
                self.activity_at = _event_timestamp(record.get("timestamp"))
                self.latest_assistant_text = text
            return
        if record_type != "event_msg":
            return
        event_type = str(payload.get("type") or "").strip()
        if event_type in {"user_message", "agent_message", "task_started", "task_complete", "token_count"}:
            self.recognized = True
        self.activity_at = _event_timestamp(record.get("timestamp"))
        if event_type == "agent_message":
            text = str(payload.get("message") or "").strip()
            if text:
                self.latest_assistant_text = text
        elif event_type == "task_complete":
            text = str(payload.get("last_agent_message") or "").strip()
            if text:
                self.latest_assistant_text = text
            self.complete = True

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
            text = _content_text(update.get("content"), strip=False)
            if text:
                metadata = update.get("_meta") if isinstance(update.get("_meta"), dict) else params.get("_meta")
                metadata = metadata if isinstance(metadata, dict) else {}
                stream_key = str(metadata.get("streamStartMs") or metadata.get("promptId") or "default")
                if stream_key != self._grok_stream_key:
                    self._grok_stream_key = stream_key
                    self._grok_stream_chunks = []
                self._grok_stream_chunks.append(text)
                self.latest_assistant_text = "".join(self._grok_stream_chunks).strip()
        elif event_type == "turn_completed":
            self.complete = True

    def _handle_qwen(self, record: dict[str, Any]) -> None:
        event_type = str(record.get("type") or "").strip()
        if event_type in {"user", "assistant", "system", "tool_result"}:
            self.recognized = True
        self.activity_at = _event_timestamp(record.get("timestamp"))
        if event_type != "assistant":
            return
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        parts = message.get("parts")
        if not isinstance(parts, list):
            return
        texts: list[str] = []
        called_tool = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            call = part.get("functionCall")
            if isinstance(call, dict):
                called_tool = True
                tool_name = str(call.get("name") or "").strip()
                if tool_name:
                    self.latest_progress_text = tool_name
                continue
            # thought=true — внутренние рассуждения модели, в ответ пользователю они не идут.
            if part.get("thought"):
                continue
            text = str(part.get("text") or "").strip()
            if text:
                texts.append(text)
        text = "\n".join(texts)
        if not text:
            return
        self.latest_assistant_text = text
        # Отдельной записи о конце хода qwen не пишет: ход закончен, когда модель
        # ответила текстом и не позвала инструмент.
        if not called_tool:
            self.complete = True

    def _handle_kimi(self, record: dict[str, Any]) -> None:
        """Kimi journals a turn as prompt -> loop events -> turn.ended.

        Assistant text never arrives as one record: each step emits its blocks as
        separate `content.part` events, so the parts of the running step are joined
        back together on every poll.
        """
        record_type = str(record.get("type") or "").strip()
        if record_type in {"turn.prompt", "context.append_message", "context.append_loop_event", "turn.ended"}:
            self.recognized = True
        self.activity_at = _kimi_activity(record.get("time"))

        if record_type == "turn.ended":
            self.complete = True
            return

        if record_type == "context.append_message":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if str(message.get("role") or "").strip() != "assistant":
                return
            text = _content_text(message.get("content"))
            if text:
                self.latest_assistant_text = text
            return

        if record_type != "context.append_loop_event":
            return
        event = record.get("event") if isinstance(record.get("event"), dict) else {}
        event_type = str(event.get("type") or "").strip()
        if event_type == "step.begin":
            self._kimi_step_parts = []
        elif event_type == "tool.call":
            tool_name = str(event.get("name") or "").strip()
            if tool_name:
                self.latest_progress_text = tool_name
        elif event_type == "content.part":
            part = event.get("part")
            text = _content_text([part]) if isinstance(part, dict) else ""
            if not text:
                return
            self._kimi_step_parts.append(text)
            self.latest_assistant_text = "\n".join(self._kimi_step_parts).strip()

    def _handle_record(self, record: dict[str, Any]) -> None:
        if self.provider == "claude":
            self._handle_claude(record)
        elif self.provider == "codex":
            self._handle_codex(record)
        elif self.provider == "qwen":
            self._handle_qwen(record)
        elif self.provider == "grok":
            self._handle_grok(record)
        elif self.provider == "kimi":
            self._handle_kimi(record)

    def _read_gemini_snapshot(self) -> None:
        """Перечитывает файл сессии gemini целиком.

        В отличие от остальных CLI, gemini не дописывает журнал, а каждый раз
        переписывает его заново, поэтому байтовый курсор неприменим: сообщения
        текущего запроса каждый раз отбираются заново по времени старта.
        """
        if self.locator is None:
            return
        path = Path(self.locator.path)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            logger.debug("structured transcript read failed path=%s", path, exc_info=True)
            return
        if not raw:
            return
        try:
            payload = loads_safe(raw, strict_first=True)
        except Exception:
            # Файл переписывается целиком, поэтому в момент записи бывает неполным.
            logger.debug("gemini session snapshot is not readable path=%s", path, exc_info=True)
            return
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return
        # Границей служит время старта запроса; сообщения без времени считаем прошлыми.
        start = -1
        for index, item in enumerate(messages):
            if not isinstance(item, dict):
                continue
            stamp = item.get("timestamp")
            if (_event_timestamp(stamp) if stamp else 0.0) < self.started_at:
                start = index
        self.recognized = True
        turn_end: tuple[str, str] = ("", "")
        for item in messages[start + 1:]:
            if not isinstance(item, dict):
                continue
            self.activity_at = _event_timestamp(item.get("timestamp"))
            if str(item.get("type") or "").strip() != "gemini":
                continue
            text = str(item.get("content") or "").strip()
            if not text:
                continue
            self.latest_assistant_text = text
            turn_end = ("", "") if item.get("toolCalls") else (str(item.get("id") or ""), text)
        # Записи о конце хода в снимке нет, а вызовы инструментов дописываются в
        # то же сообщение следом за текстом: ход закончен, когда последний ответ
        # модели не изменился между чтениями и инструментов в нём так и нет.
        if turn_end[1] and turn_end == self._gemini_turn_end:
            self.complete = True
        self._gemini_turn_end = turn_end

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
        if self.provider not in _SUPPORTED_PROVIDERS:
            return TranscriptPollResult()
        if self.locator is None:
            self._discover()
        if self.provider == "gemini":
            self._read_gemini_snapshot()
        else:
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
