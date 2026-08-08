"""Discovery of recent on-disk CLI sessions for resume selection.

Each CLI keeps its conversations in its own layout under the user's home dir.
This module lists the newest ones for a given workdir so the user can pick
which conversation to resume instead of typing a raw session id.

Path/key helpers are imported from ``session_transfer.reader_*`` on purpose:
the ids listed here must resolve to the very same files those readers open.
"""

from __future__ import annotations

import logging
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from modes.sdk.runtime.json_normalizer import loads_safe

from app.services.session_transfer._user_paths import home_for_user
from app.services.session_transfer.reader_claude import CLAUDE_RUN_USER
from app.services.session_transfer.reader_claude import _project_key as _claude_project_key
from app.services.session_transfer.reader_codex import _is_synthetic_user_text as _codex_is_synthetic
from app.services.session_transfer.reader_gemini import _project_hash as _gemini_project_hash
from app.services.session_transfer.reader_grok import _workspace_key as _grok_workspace_key
from app.services.session_transfer.reader_kimi import _content_text as _kimi_content_text
from app.services.session_transfer.reader_kimi import _is_real_user_message as _kimi_is_real_user
from app.services.session_transfer.reader_kimi import _workspace_key as _kimi_workspace_key
from app.services.session_transfer.reader_qwen import _project_key_candidates as _qwen_project_keys

logger = logging.getLogger(__name__)

DEFAULT_RECENT_LIMIT = 4
PREVIEW_MAX_LEN = 60

# Read only the head of a transcript when looking for its first user request.
_PREVIEW_SCAN_LINES = 200
# Codex keeps every rollout in one global tree; cap the scan for the project filter.
_CODEX_SCAN_LIMIT = 500

# Bridge markers injected into prompts (see cli_backends/tmux_parser.py) are noise in previews.
_SERVICE_MARKER_RE = re.compile(r"<{2,3}[A-Z_]+:[0-9a-fA-F-]+>{2,3}")


@dataclass(frozen=True)
class CliSessionCandidate:
    """One CLI conversation found on disk and usable as a resume target."""

    cli: str
    session_id: str
    mtime: float
    preview: str


def list_recent_cli_sessions(
    cli_name: str,
    workdir: str,
    *,
    limit: int = DEFAULT_RECENT_LIMIT,
) -> List[CliSessionCandidate]:
    """Return up to *limit* newest sessions of *cli_name* recorded for *workdir*."""
    name = str(cli_name or "").strip().lower()
    root = str(workdir or "").strip()
    lister = _LISTERS.get(name)
    if lister is None or not root or limit <= 0:
        return []
    try:
        found = lister(root, int(limit))
    except Exception:
        logger.exception("cli session history failed cli=%s workdir=%s", name, root)
        return []
    found.sort(key=lambda candidate: candidate.mtime, reverse=True)
    return found[:limit]


def _home_dirs() -> List[Path]:
    """Home directories that may hold CLI history (bot may run as another user)."""
    out: List[Path] = []
    seen: set[str] = set()
    candidates: List[Optional[Path]] = [Path.home()]
    try:
        candidates.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except Exception:
        logger.warning("cli session history: cannot resolve home for current uid")
    candidates.append(home_for_user(CLAUDE_RUN_USER))
    for home in candidates:
        if home is None:
            continue
        key = str(home)
        if key in seen:
            continue
        seen.add(key)
        out.append(home)
    return out


def _per_char_key(workdir: str) -> str:
    """Claude/Qwen name a project dir by replacing every non-alphanumeric char with '-'.

    The readers collapse runs of such chars instead, which misses paths with
    dots (``/home/user/.paperclip`` -> ``-home-user--paperclip``).
    """
    raw = os.path.realpath(str(workdir or "")).rstrip(os.sep) or str(workdir or "")
    return re.sub(r"[^A-Za-z0-9]", "-", raw)


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _clean_preview(text: str) -> str:
    """Collapse a first user message into a short single-line label."""
    line = " ".join(_SERVICE_MARKER_RE.sub(" ", str(text or "")).split())
    if not line or line.startswith("<") or line.startswith("Caveat:"):
        return ""
    if len(line) > PREVIEW_MAX_LEN:
        return line[: PREVIEW_MAX_LEN - 1].rstrip() + "…"
    return line


def _iter_jsonl(path: Path, *, max_lines: int = _PREVIEW_SCAN_LINES) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for index, raw in enumerate(handle):
                if index >= max_lines:
                    return
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = loads_safe(line, strict_first=True)
                except Exception:
                    continue
                if isinstance(data, dict):
                    yield data
    except OSError:
        return


def _load_json(path: Path) -> Optional[dict]:
    try:
        payload = loads_safe(path.read_text(encoding="utf-8", errors="ignore"), strict_first=True)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _first_text_block(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "") == "text":
                return str(item.get("text") or "")
    return ""


def _newest(
    entries: List[Tuple[float, str, Path]],
    *,
    cli: str,
    limit: int,
    preview: Callable[[Path], str],
) -> List[CliSessionCandidate]:
    """Keep the newest unique ids and compute previews only for those."""
    seen: set[str] = set()
    out: List[CliSessionCandidate] = []
    for mtime, session_id, path in sorted(entries, key=lambda item: item[0], reverse=True):
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        out.append(
            CliSessionCandidate(cli=cli, session_id=session_id, mtime=mtime, preview=preview(path))
        )
        if len(out) >= limit:
            break
    return out


def _claude_preview(path: Path) -> str:
    for data in _iter_jsonl(path):
        if str(data.get("type") or "") != "user" or data.get("isSidechain"):
            continue
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = _clean_preview(_first_text_block(content))
        if text:
            return text
    return ""


def _list_claude(workdir: str, limit: int) -> List[CliSessionCandidate]:
    keys = _unique([_claude_project_key(workdir), _per_char_key(workdir)])
    entries: List[Tuple[float, str, Path]] = []
    for home in _home_dirs():
        for key in keys:
            project_dir = home / ".claude" / "projects" / key
            if not project_dir.is_dir():
                continue
            for path in project_dir.glob("*.jsonl"):
                entries.append((_mtime(path), path.stem, path))
    return _newest(entries, cli="claude", limit=limit, preview=_claude_preview)


def _qwen_preview(path: Path) -> str:
    for data in _iter_jsonl(path):
        if str(data.get("type") or "") != "user":
            continue
        message = data.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, dict):
                text = _clean_preview(str(part.get("text") or ""))
                if text:
                    return text
    return ""


def _list_qwen(workdir: str, limit: int) -> List[CliSessionCandidate]:
    keys = _unique(list(_qwen_project_keys(workdir)) + [_per_char_key(workdir)])
    entries: List[Tuple[float, str, Path]] = []
    for home in _home_dirs():
        for key in keys:
            chats_dir = home / ".qwen" / "projects" / key / "chats"
            if not chats_dir.is_dir():
                continue
            for path in chats_dir.glob("*.jsonl"):
                entries.append((_mtime(path), path.stem, path))
    return _newest(entries, cli="qwen", limit=limit, preview=_qwen_preview)


def _gemini_preview(path: Path) -> str:
    payload = _load_json(path) or {}
    messages = payload.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or str(message.get("type") or "") != "user":
            continue
        text = _clean_preview(str(message.get("content") or ""))
        if text:
            return text
    return ""


def _list_gemini(workdir: str, limit: int) -> List[CliSessionCandidate]:
    project_hash = _gemini_project_hash(workdir)
    project_name = os.path.basename(os.path.realpath(workdir).rstrip(os.sep)).casefold()
    files: List[Tuple[float, Path]] = []
    for home in _home_dirs():
        base = home / ".gemini" / "tmp"
        if not base.is_dir():
            continue
        for child in base.iterdir():
            name = child.name.casefold()
            if not child.is_dir():
                continue
            if not (
                child.name == project_hash
                or name == project_name
                or name.startswith(f"{project_name}-")
            ):
                continue
            chats_dir = child / "chats"
            if not chats_dir.is_dir():
                continue
            for path in chats_dir.glob("session-*.json"):
                files.append((_mtime(path), path))

    # Directory names are ambiguous (project basename may repeat), so the
    # projectHash inside each payload is the authoritative filter.
    out: List[CliSessionCandidate] = []
    seen: set[str] = set()
    for mtime, path in sorted(files, key=lambda item: item[0], reverse=True):
        payload = _load_json(path)
        if not payload or str(payload.get("projectHash") or "").strip() != project_hash:
            continue
        session_id = str(payload.get("sessionId") or "").strip() or path.stem[len("session-"):]
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        out.append(
            CliSessionCandidate(
                cli="gemini",
                session_id=session_id,
                mtime=mtime,
                preview=_gemini_preview(path),
            )
        )
        if len(out) >= limit:
            break
    return out


def _grok_preview(path: Path) -> str:
    summary = _load_json(path / "summary.json") or {}
    text = _clean_preview(
        str(summary.get("generated_title") or summary.get("session_summary") or "")
    )
    if text:
        return text
    for data in _iter_jsonl(path / "chat_history.jsonl"):
        if str(data.get("type") or "") != "user" or data.get("synthetic_reason"):
            continue
        content = data.get("content")
        raw = content if isinstance(content, str) else _first_text_block(content)
        text = _clean_preview(raw)
        if text:
            return text
    return ""


def _list_grok(workdir: str, limit: int) -> List[CliSessionCandidate]:
    key = _grok_workspace_key(workdir)
    if not key:
        return []
    entries: List[Tuple[float, str, Path]] = []
    for home in _home_dirs():
        base = home / ".grok" / "sessions" / key
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            mtime = max(_mtime(child), _mtime(child / "chat_history.jsonl"))
            entries.append((mtime, child.name, child))
    return _newest(entries, cli="grok", limit=limit, preview=_grok_preview)


def _kimi_preview(path: Path) -> str:
    state = _load_json(path / "state.json") or {}
    text = _clean_preview(str(state.get("title") or state.get("lastPrompt") or ""))
    if text:
        return text
    for data in _iter_jsonl(path / "agents" / "main" / "wire.jsonl"):
        if str(data.get("type") or "") != "context.append_message":
            continue
        message = data.get("message")
        if not isinstance(message, dict) or str(message.get("role") or "") != "user":
            continue
        if not _kimi_is_real_user(message):
            continue
        text = _clean_preview(_kimi_content_text(message.get("content")))
        if text:
            return text
    return ""


def _list_kimi(workdir: str, limit: int) -> List[CliSessionCandidate]:
    key = _kimi_workspace_key(workdir)
    if not key:
        return []
    entries: List[Tuple[float, str, Path]] = []
    for home in _home_dirs():
        base = home / ".kimi-code" / "sessions" / key
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            mtime = max(_mtime(child), _mtime(child / "agents" / "main" / "wire.jsonl"))
            entries.append((mtime, child.name, child))
    return _newest(entries, cli="kimi", limit=limit, preview=_kimi_preview)


def _codex_preview(path: Path) -> str:
    for data in _iter_jsonl(path):
        if str(data.get("type") or "") != "response_item":
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict) or str(payload.get("type") or "") != "message":
            continue
        if str(payload.get("role") or "").strip().lower() != "user":
            continue
        content = payload.get("content")
        raw = ""
        if isinstance(content, str):
            raw = content
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and str(item.get("type") or "") in (
                    "input_text",
                    "output_text",
                    "text",
                ):
                    raw = str(item.get("text") or "")
                    break
        if _codex_is_synthetic(raw):
            continue
        text = _clean_preview(raw)
        if text:
            return text
    return ""


def _codex_rollout_paths(base: Path) -> Iterator[Path]:
    """Yield rollout files newest-first by walking the YYYY/MM/DD tree backwards."""
    for year in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        for month in sorted((p for p in year.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
            for day in sorted((p for p in month.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
                for path in sorted(day.glob("rollout-*.jsonl"), key=lambda p: p.name, reverse=True):
                    yield path


def _codex_session_meta(path: Path) -> Optional[dict]:
    for data in _iter_jsonl(path, max_lines=1):
        if str(data.get("type") or "") != "session_meta":
            return None
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else None
    return None


def _list_codex(workdir: str, limit: int) -> List[CliSessionCandidate]:
    target = os.path.realpath(workdir)
    out: List[CliSessionCandidate] = []
    seen: set[str] = set()
    scanned = 0
    for home in _home_dirs():
        base = home / ".codex" / "sessions"
        if not base.is_dir():
            continue
        for path in _codex_rollout_paths(base):
            scanned += 1
            if scanned > _CODEX_SCAN_LIMIT:
                logger.info(
                    "codex history scan stopped after %d files in %s; older sessions not listed",
                    _CODEX_SCAN_LIMIT,
                    base,
                )
                break
            meta = _codex_session_meta(path)
            if not meta:
                continue
            cwd = str(meta.get("cwd") or "").strip()
            if not cwd or os.path.realpath(cwd) != target:
                continue
            session_id = str(meta.get("session_id") or meta.get("id") or "").strip()
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            out.append(
                CliSessionCandidate(
                    cli="codex",
                    session_id=session_id,
                    mtime=_mtime(path),
                    preview=_codex_preview(path),
                )
            )
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return out


_LISTERS: Dict[str, Callable[[str, int], List[CliSessionCandidate]]] = {
    "claude": _list_claude,
    "codex": _list_codex,
    "gemini": _list_gemini,
    "grok": _list_grok,
    "kimi": _list_kimi,
    "qwen": _list_qwen,
}
