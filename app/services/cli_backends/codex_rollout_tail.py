"""Чтение rollout-файла codex как запасного источника результата.

Headless-запуск `codex exec --json` иногда замолкает в stdout, хотя сам codex
продолжает писать rollout в `~/.codex/sessions/**`. Этот модуль позволяет
забрать ответ оттуда.

От `CliTranscriptReader` отличается тем, что не опирается на маркеры
`<<<CLI_PROXY_REQUEST>>>` / `<<<DONE>>>`: в headless промпт ими не оборачивается,
а завершение хода определяется событием `task_complete`.
"""

from __future__ import annotations

import logging
import os
import pwd
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from modes.sdk.runtime.json_normalizer import loads_safe


logger = logging.getLogger(__name__)

_DISCOVERY_CANDIDATE_LIMIT = 20
# Записи чуть старше старта процесса тоже учитываем: codex успевает записать
# task_started до того, как мы зафиксировали время запуска.
_START_TIME_SLACK_SEC = 5.0


@dataclass(frozen=True)
class CodexRolloutState:
    path: str
    assistant_text: str = ""
    turn_complete: bool = False
    last_event_at: Optional[float] = None
    session_id: str = ""


def _event_timestamp(raw: Any) -> Optional[float]:
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        logger.debug("codex rollout timestamp is invalid value=%r", text)
        return None


def _resolve_home(username: str) -> Path:
    name = str(username or "").strip()
    if not name:
        return Path.home()
    try:
        return Path(pwd.getpwnam(name).pw_dir)
    except KeyError:
        logger.warning("codex rollout user does not exist username=%s", name)
        return Path.home()


class CodexRolloutTail:
    """Находит rollout текущего запуска и отдаёт его состояние."""

    def __init__(
        self,
        *,
        workdir: str,
        started_at: float,
        session_id: str = "",
        username: str = "",
        home_dir: Optional[Path] = None,
    ) -> None:
        self.workdir = os.path.realpath(str(workdir or "")) if workdir else ""
        self.started_at = float(started_at)
        self.session_id = str(session_id or "").strip()
        self.home_dir = Path(home_dir) if home_dir is not None else _resolve_home(username)
        self.root = self.home_dir / ".codex" / "sessions"
        self._path: Optional[Path] = None

    def _matches_workdir(self, path: Path) -> bool:
        if not self.workdir:
            return True
        try:
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
                    if not cwd:
                        return True
                    return os.path.realpath(cwd) == self.workdir
        except (OSError, ValueError):
            logger.debug("codex rollout meta read failed path=%s", path, exc_info=True)
        return False

    def _resolve_path(self) -> Optional[Path]:
        if self._path is not None and self._path.is_file():
            return self._path
        if not self.root.is_dir():
            return None
        try:
            if self.session_id:
                # resume дописывает тот же файл, поэтому поиск по треду надёжен.
                for candidate in self.root.rglob(f"*-{self.session_id}.jsonl"):
                    self._path = candidate
                    return candidate
            candidates: list[tuple[float, Path]] = []
            for path in self.root.rglob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    logger.debug("codex rollout stat failed path=%s", path, exc_info=True)
                    continue
                if mtime < self.started_at - _START_TIME_SLACK_SEC:
                    continue
                candidates.append((float(mtime), path))
            candidates.sort(key=lambda item: item[0], reverse=True)
            for _, path in candidates[:_DISCOVERY_CANDIDATE_LIMIT]:
                if self._matches_workdir(path):
                    self._path = path
                    return path
        except OSError:
            logger.exception("codex rollout discovery failed root=%s", self.root)
        return None

    def poll(self) -> Optional[CodexRolloutState]:
        path = self._resolve_path()
        if path is None:
            return None
        assistant_text = ""
        turn_complete = False
        last_event_at: Optional[float] = None
        session_id = self.session_id
        threshold = self.started_at - _START_TIME_SLACK_SEC
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        record = loads_safe(text, strict_first=True)
                    except Exception:
                        logger.debug("codex rollout record skipped path=%s", path, exc_info=True)
                        continue
                    if not isinstance(record, dict):
                        continue
                    record_type = str(record.get("type") or "").strip()
                    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                    if record_type == "session_meta":
                        # Идентификатор треда читаем без фильтра по времени: при resume
                        # запись остаётся от первого хода, но тред тот же.
                        meta_id = str(payload.get("id") or "").strip()
                        if meta_id:
                            session_id = meta_id
                        continue
                    if record_type != "event_msg":
                        continue
                    timestamp = _event_timestamp(record.get("timestamp"))
                    # Записи прошлых ходов отбрасываем, иначе resume подхватит старый ответ.
                    if timestamp is not None and timestamp < threshold:
                        continue
                    event_type = str(payload.get("type") or "").strip()
                    if timestamp is not None:
                        last_event_at = timestamp
                    if event_type == "agent_message":
                        message = str(payload.get("message") or "").strip()
                        if message:
                            assistant_text = message
                    elif event_type == "task_complete":
                        message = str(payload.get("last_agent_message") or "").strip()
                        if message:
                            assistant_text = message
                        turn_complete = True
        except OSError:
            logger.debug("codex rollout read failed path=%s", path, exc_info=True)
            return None
        if last_event_at is None:
            try:
                last_event_at = path.stat().st_mtime
            except OSError:
                last_event_at = time.time()
        return CodexRolloutState(
            path=str(path),
            assistant_text=assistant_text,
            turn_complete=turn_complete,
            last_event_at=last_event_at,
            session_id=session_id,
        )
