"""SessionTransferService: read sessions from one CLI and write them into another."""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from .canonical import CanonicalSession
from .capsule import DEFAULT_CAPSULE_MAX_CHARS, build_transfer_package

logger = logging.getLogger(__name__)

_READERS: Dict[str, Callable[[str, str], Optional[CanonicalSession]]] = {}
_WRITERS: Dict[str, Callable[..., Optional[str]]] = {}

# CLI tools whose on-disk session files must live under another user's HOME and
# be owned by that user. In production the bot runs as root and launches Claude
# via `su - claude-bot`, so its session files belong to claude-bot. Other CLIs
# currently run as the bot's own user, so no remap is needed.
_TARGET_USERNAMES: Dict[str, str] = {
    "claude": "claude-bot",
}


def _ensure_readers() -> Dict[str, Callable[[str, str], Optional[CanonicalSession]]]:
    if _READERS:
        return _READERS
    from . import reader_claude, reader_codex, reader_gemini, reader_qwen

    _READERS["claude"] = reader_claude.read_session
    _READERS["codex"] = reader_codex.read_session
    _READERS["gemini"] = reader_gemini.read_session
    _READERS["qwen"] = reader_qwen.read_session
    return _READERS


def _ensure_writers() -> Dict[str, Callable[..., Optional[str]]]:
    if _WRITERS:
        return _WRITERS
    from . import writer_claude, writer_codex, writer_gemini, writer_qwen

    _WRITERS["claude"] = writer_claude.write_session
    _WRITERS["codex"] = writer_codex.write_session
    _WRITERS["gemini"] = writer_gemini.write_session
    _WRITERS["qwen"] = writer_qwen.write_session
    return _WRITERS


def extract_session(
    source_cli: str,
    session_id: str,
    workspace: str,
) -> Optional[CanonicalSession]:
    """Read a CanonicalSession from the source CLI's native files."""
    cli_name = str(source_cli or "").strip().lower()
    sid = str(session_id or "").strip()
    ws = str(workspace or "").strip()
    if not cli_name or not sid or not ws:
        return None

    reader = _ensure_readers().get(cli_name)
    if reader is None:
        logger.info("session_transfer: no reader for cli=%s", cli_name)
        return None

    try:
        return reader(sid, ws)
    except Exception:
        logger.exception("session_transfer: reader failed for cli=%s session=%s", cli_name, sid)
        return None


def write_target_session(
    canonical: CanonicalSession,
    target_cli: str,
    workspace: str,
    *,
    compact: bool = True,
    max_chars: int = DEFAULT_CAPSULE_MAX_CHARS,
) -> Optional[str]:
    """Write canonical session into the target CLI's native files. Returns new session id."""
    cli_name = str(target_cli or "").strip().lower()
    ws = str(workspace or "").strip()
    if not cli_name or not ws or not canonical or not canonical.messages:
        return None

    writer = _ensure_writers().get(cli_name)
    if writer is None:
        logger.info("session_transfer: no writer for cli=%s", cli_name)
        return None

    username = _TARGET_USERNAMES.get(cli_name)
    try:
        target_canonical = canonical
        if compact:
            package = build_transfer_package(
                canonical,
                target_cli=cli_name,
                workspace=ws,
                max_chars=max_chars,
            )
            target_canonical = package.canonical
        return writer(target_canonical, ws, username)
    except Exception:
        logger.exception("session_transfer: writer failed for cli=%s", cli_name)
        return None
