import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.services.logging_service import resolve_log_paths
from session import session_runtime_uid
from utils.ui import format_session_selector_label, format_session_title


class LogsServiceError(Exception):
    status = 400


class LogTypeError(LogsServiceError):
    status = 400


class LogAccessDeniedError(LogsServiceError):
    status = 403


@dataclass
class ParsedLogEntry:
    lines: List[str] = field(default_factory=list)
    chat_id: str = "-"
    session_id: str = "-"
    session_uid: str = "-"
    session_name: str = "-"
    logger_name: str = "-"
    level: str = "-"
    timestamp: str = ""

    def to_payload(self) -> Dict[str, str]:
        text = "\n".join(self.lines)
        # Generate a stable ID for deduplication on the frontend
        raw_id = f"{self.timestamp}|{self.level}|{self.logger_name}|{text}"
        entry_id = hashlib.md5(raw_id.encode("utf-8", errors="replace")).hexdigest()
        return {
            "id": entry_id,
            "text": text,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "session_uid": self.session_uid,
            "session_name": self.session_name,
            "logger_name": self.logger_name,
            "level": self.level,
            "timestamp": self.timestamp,
        }


class LogEntryAccumulator:
    _TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3}\s+")
    _HEAD_RE = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+"
        r"(?P<level>[A-Z]+)\s+"
        r"(?:\[(?P<logger>[^\]]+)\]\s+)?"
        r"(?P<rest>.*)$"
    )
    _SID_RE = re.compile(r"\[sid=(?P<sid>[^\s\]]+)\]")
    _CTX_RE = re.compile(r"\[suid=(?P<suid>[^\s\]]+)\]")

    def __init__(self) -> None:
        self._current: Optional[ParsedLogEntry] = None
        self._last_update_ts: float = 0.0

    @classmethod
    def _is_header_line(cls, line: str) -> bool:
        return bool(cls._TS_RE.match(line))

    @classmethod
    def _parse_header(cls, line: str) -> ParsedLogEntry:
        entry = ParsedLogEntry(lines=[line])
        match = cls._HEAD_RE.match(line)
        if match:
            entry.timestamp = str(match.group("ts") or "")
            entry.level = str(match.group("level") or "-")
            entry.logger_name = str(match.group("logger") or "-")
            rest = str(match.group("rest") or "")
            sid_match = cls._SID_RE.search(rest)
            if sid_match:
                entry.session_id = str(sid_match.group("sid") or "-")
            ctx_match = cls._CTX_RE.search(rest)
            if ctx_match:
                entry.session_uid = str(ctx_match.group("suid") or "-")
                chat_part, sid_part = cls._split_session_uid(entry.session_uid)
                if chat_part:
                    entry.chat_id = str(chat_part)
                if sid_part and entry.session_id == "-":
                    entry.session_id = str(sid_part)
        return entry

    @staticmethod
    def _split_session_uid(session_uid: str) -> tuple[str, str]:
        token = str(session_uid or "").strip()
        if not token or token == "-":
            return "-", "-"
        if token.startswith("thread:"):
            parts = token.split(":", 2)
            if len(parts) == 3:
                return str(parts[1] or "-"), "-"
        if token.startswith("chat:"):
            parts = token.split(":", 2)
            if len(parts) == 3:
                return str(parts[1] or "-"), str(parts[2] or "-")
            if len(parts) == 2:
                return str(parts[1] or "-"), "-"
        if token.startswith("desktop:"):
            parts = token.split(":", 1)
            if len(parts) == 2:
                return "desktop", "-"
        if ":" in token:
            chat_part, sid_part = token.split(":", 1)
            return str(chat_part or "-"), str(sid_part or "-")
        return "-", token

    def feed_line(self, line: str) -> List[ParsedLogEntry]:
        now = time.monotonic()
        self._last_update_ts = now
        completed: List[ParsedLogEntry] = []
        clean = line.rstrip("\n")

        if self._is_header_line(clean):
            if self._current is not None:
                completed.append(self._current)
            self._current = self._parse_header(clean)
            return completed

        if self._current is None:
            self._current = ParsedLogEntry(lines=[clean])
        else:
            self._current.lines.append(clean)
        return completed

    def flush_stale(self, *, now: float, idle_sec: float) -> Optional[ParsedLogEntry]:
        if self._current is None:
            return None
        if self._last_update_ts <= 0:
            return None
        if now - self._last_update_ts < float(idle_sec):
            return None
        entry = self._current
        self._current = None
        self._last_update_ts = now
        return entry

    def flush_all(self) -> Optional[ParsedLogEntry]:
        entry = self._current
        self._current = None
        self._last_update_ts = 0.0
        return entry


@dataclass
class LogsService:
    app: Any
    _LOG_TYPES: Tuple[str, ...] = ("main", "error", "agent", "cli_dialog", "miniapp")
    _HIDDEN_LOGGER_NAMES: Tuple[str, ...] = ("aiohttp.access",)

    @property
    def history_options(self) -> List[int]:
        return [0, 100, 200, 500, 1000]

    def _log_paths(self) -> Dict[str, str]:
        return resolve_log_paths(str(self.app.config.defaults.log_path))

    def list_log_types(self, *, include_paths: bool = False) -> List[Dict[str, Any]]:
        labels = {
            "main": "Основной",
            "error": "Ошибки",
            "agent": "Agent",
            "cli_dialog": "CLI диалог",
            "miniapp": "MiniApp",
        }
        paths = self._log_paths()
        out: List[Dict[str, Any]] = []
        for key in self._LOG_TYPES:
            path = str(paths.get(key) or "")
            exists = bool(path and os.path.exists(path))
            size = int(os.path.getsize(path)) if exists else 0
            item: Dict[str, Any] = {"id": key, "label": labels.get(key, key), "exists": exists, "size": size}
            if include_paths:
                item["path"] = path
            out.append(item)
        return out

    def resolve_log_path(self, log_type: str) -> str:
        paths = self._log_paths()
        key = str(log_type or "").strip()
        path = paths.get(key)
        if not path:
            raise LogTypeError("unknown log type")
        return str(path)

    def file_end_position(self, log_type: str) -> int:
        path = self.resolve_log_path(log_type)
        try:
            return int(os.path.getsize(path))
        except OSError:
            return 0

    def list_session_filters(self, *, user_id: int, is_admin: bool) -> List[Dict[str, Any]]:
        sessions: List[Dict[str, Any]] = []
        manager = getattr(self.app, "manager", None)
        if manager is not None:
            if is_admin:
                by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
                for chat_id in sorted(by_chat.keys()):
                    by_id = by_chat.get(chat_id) or {}
                    for sid in sorted(by_id.keys()):
                        session = by_id[sid]
                        session_name = str(getattr(session, "name", "") or sid)
                        suid = session_runtime_uid(session)
                        if not suid:
                            continue
                        session_title = format_session_selector_label(
                            session,
                            telegram_user_id=int(chat_id),
                        )
                        sessions.append({
                            "chat_id": int(chat_id),
                            "session_id": sid,
                            "session_uid": suid,
                            "session_name": session_name,
                            "label": session_title,
                        })
            else:
                by_id = manager.sessions_for_chat(int(user_id))
                for sid in sorted(by_id.keys()):
                    session = by_id[sid]
                    session_name = str(getattr(session, "name", "") or sid)
                    suid = session_runtime_uid(session)
                    if not suid:
                        continue
                    session_title = format_session_title(session)
                    sessions.append({
                        "chat_id": int(user_id),
                        "session_id": sid,
                        "session_uid": suid,
                        "session_name": session_name,
                        "label": session_title,
                    })

        return sorted(
            sessions,
            key=lambda x: (
                str(x.get("session_name") or "").lower(),
                str(x.get("session_uid") or ""),
                str(x.get("session_id") or ""),
            ),
        )

    def allowed_session_uids(self, *, user_id: int, is_admin: bool) -> set[str]:
        manager = getattr(self.app, "manager", None)
        if manager is None:
            return set()
        allowed: set[str] = set()
        if is_admin:
            by_chat = dict(getattr(manager, "sessions_by_chat", {}) or {})
            for chat_id, by_id in by_chat.items():
                if not isinstance(by_id, dict):
                    continue
                for session in by_id.values():
                    suid = session_runtime_uid(session)
                    if suid:
                        allowed.add(suid)
            return allowed

        by_id = manager.sessions_for_chat(int(user_id))
        for session in by_id.values():
            suid = session_runtime_uid(session)
            if suid:
                allowed.add(suid)
        return allowed

    def allowed_session_pairs(self, *, user_id: int, is_admin: bool) -> set[tuple[str, str]]:
        sessions = self.list_session_filters(user_id=int(user_id), is_admin=bool(is_admin))
        return {
            (
                str(item.get("session_uid") or "").strip(),
                str(item.get("session_id") or "").strip(),
            )
            for item in sessions
            if str(item.get("session_uid") or "").strip() and str(item.get("session_id") or "").strip()
        }

    def _read_tail_lines(
        self,
        path: str,
        *,
        min_headers: int,
        max_bytes: int = 8 * 1024 * 1024,
        chunk_size: int = 64 * 1024,
    ) -> List[str]:
        try:
            size = int(os.path.getsize(path))
        except OSError:
            return []
        if size <= 0:
            return []

        pos = size
        data = bytearray()
        lines: List[str] = []

        with open(path, "rb") as f:
            while pos > 0 and len(data) < int(max_bytes):
                step = min(int(chunk_size), pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step)
                if not chunk:
                    break
                data[:0] = chunk

                text = data.decode("utf-8", errors="replace")
                lines = text.splitlines(True)
                header_count = sum(1 for ln in lines if LogEntryAccumulator._is_header_line(ln.rstrip("\n")))
                if header_count >= int(min_headers):
                    break

        if not lines:
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines(True)
        return lines

    @staticmethod
    def _drop_partial_head_entry(lines: List[str], entries: List[ParsedLogEntry]) -> List[ParsedLogEntry]:
        if not lines or not entries:
            return entries
        first_line = lines[0].rstrip("\n")
        if LogEntryAccumulator._is_header_line(first_line):
            return entries
        return entries[1:]

    def ensure_session_scope_allowed(
        self,
        *,
        user_id: int,
        is_admin: bool,
        session_uid: Optional[str],
        session_id: Optional[str] = None,
    ) -> None:
        suid = str(session_uid or "").strip()
        sid = str(session_id or "").strip()
        if sid and not suid:
            raise LogAccessDeniedError("session_id filter requires session_uid")
        if not suid or suid == "*":
            return
        if is_admin:
            return
        if sid:
            if (suid, sid) not in self.allowed_session_pairs(user_id=int(user_id), is_admin=bool(is_admin)):
                raise LogAccessDeniedError("session filter is not allowed")
            return
        if suid not in self.allowed_session_uids(user_id=int(user_id), is_admin=bool(is_admin)):
            raise LogAccessDeniedError("session filter is not allowed")

    def _entry_allowed(
        self,
        entry: ParsedLogEntry,
        *,
        user_id: int,
        is_admin: bool,
        session_uid_filter: Optional[str],
        session_id_filter: Optional[str],
        allowed_session_uids: Optional[set[str]] = None,
        allowed_session_pairs: Optional[set[tuple[str, str]]] = None,
    ) -> bool:
        if self._is_hidden_entry(entry):
            return False

        session_uid = str(entry.session_uid or "-")
        session_id = str(entry.session_id or "-")

        suid_filter = str(session_uid_filter or "").strip()
        if suid_filter and suid_filter != "*" and session_uid != suid_filter:
            return False
        sid_filter = str(session_id_filter or "").strip()
        if sid_filter and session_id != sid_filter:
            return False

        if is_admin:
            if not session_uid or session_uid == "-":
                return True
            if allowed_session_uids is not None and session_uid not in allowed_session_uids:
                return False
            if (
                allowed_session_pairs is not None
                and session_id
                and session_id != "-"
                and (session_uid, session_id) not in allowed_session_pairs
            ):
                return False
            return True

        if not session_uid or session_uid == "-":
            return False
        if allowed_session_uids is None:
            return False
        if session_uid not in allowed_session_uids:
            return False
        if (
            allowed_session_pairs is not None
            and session_id
            and session_id != "-"
            and (session_uid, session_id) not in allowed_session_pairs
        ):
            return False
        return True

    def entry_allowed(
        self,
        entry: ParsedLogEntry,
        *,
        user_id: int,
        is_admin: bool,
        session_uid_filter: Optional[str],
        session_id_filter: Optional[str],
        allowed_session_uids: Optional[set[str]] = None,
        allowed_session_pairs: Optional[set[tuple[str, str]]] = None,
    ) -> bool:
        return self._entry_allowed(
            entry,
            user_id=int(user_id),
            is_admin=bool(is_admin),
            session_uid_filter=session_uid_filter,
            session_id_filter=session_id_filter,
            allowed_session_uids=allowed_session_uids,
            allowed_session_pairs=allowed_session_pairs,
        )

    def _is_hidden_entry(self, entry: ParsedLogEntry) -> bool:
        logger_name = str(entry.logger_name or "").strip()
        return logger_name in self._HIDDEN_LOGGER_NAMES

    def parse_lines(self, lines: List[str]) -> List[ParsedLogEntry]:
        acc = LogEntryAccumulator()
        entries: List[ParsedLogEntry] = []
        for line in lines:
            entries.extend(acc.feed_line(line))
        tail = acc.flush_all()
        if tail is not None:
            entries.append(tail)
        return entries

    def read_history(
        self,
        *,
        log_type: str,
        history_limit: int,
        user_id: int,
        is_admin: bool,
        session_uid_filter: Optional[str] = None,
        session_id_filter: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        limit = int(history_limit)
        if limit <= 0:
            return []

        path = self.resolve_log_path(log_type)
        if not os.path.exists(path):
            return []

        lines = self._read_tail_lines(
            path,
            min_headers=max(limit * 3, 100),
            max_bytes=8 * 1024 * 1024,
        )
        parsed = self._drop_partial_head_entry(lines, self.parse_lines(lines))
        allowed_session_uids = self.allowed_session_uids(user_id=int(user_id), is_admin=bool(is_admin))
        allowed_session_pairs = self.allowed_session_pairs(user_id=int(user_id), is_admin=bool(is_admin))
        filtered = [
            entry
            for entry in parsed
            if self._entry_allowed(
                entry,
                user_id=int(user_id),
                is_admin=bool(is_admin),
                session_uid_filter=session_uid_filter,
                session_id_filter=session_id_filter,
                allowed_session_uids=allowed_session_uids,
                allowed_session_pairs=allowed_session_pairs,
            )
        ]
        if len(filtered) > limit:
            filtered = filtered[-limit:]
        return [entry.to_payload() for entry in filtered]
