"""Cross-user filesystem helpers for session writers.

Background: in production the bot runs as root via systemd, but Claude Code is
launched with `su - claude-bot`, so its on-disk session files must live under
`/home/claude-bot/.claude/...` and be owned by claude-bot. This module exposes
small helpers to resolve another user's HOME and chown freshly created files
to that user. All operations are best-effort and log on failure.
"""

from __future__ import annotations

import logging
import os
import pwd
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def home_for_user(username: Optional[str]) -> Optional[Path]:
    name = str(username or "").strip()
    if not name:
        return None
    try:
        return Path(pwd.getpwnam(name).pw_dir)
    except Exception:
        logger.warning("session_transfer: cannot resolve home for user=%s", name)
        return None


def _uid_gid(username: Optional[str]) -> Optional[Tuple[int, int]]:
    name = str(username or "").strip()
    if not name:
        return None
    try:
        info = pwd.getpwnam(name)
        return info.pw_uid, info.pw_gid
    except Exception:
        return None


def mkdir_chain_with_chown(target_dir: Path, username: Optional[str]) -> None:
    """Create target_dir (parents=True) and chown only the dirs we just created."""
    to_create: List[Path] = []
    p = target_dir
    while not p.exists():
        to_create.append(p)
        p = p.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    info = _uid_gid(username)
    if info is None:
        return
    uid, gid = info
    for path in reversed(to_create):
        _chown_best_effort(path, uid, gid)


def chown_path(path: Path, username: Optional[str]) -> None:
    info = _uid_gid(username)
    if info is None:
        return
    uid, gid = info
    _chown_best_effort(path, uid, gid)


def _chown_best_effort(path: Path, uid: int, gid: int) -> None:
    try:
        st = os.stat(str(path))
        if st.st_uid == uid and st.st_gid == gid:
            return
        os.chown(str(path), uid, gid)
    except PermissionError:
        logger.warning("session_transfer: chown denied for %s (need root)", path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("session_transfer: chown failed for %s", path)
