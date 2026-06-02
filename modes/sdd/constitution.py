"""Load project constitution for SDD mode and injection into prompts."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

_CONSTITUTION_RELATIVE_PATH = os.path.join(".cli-proxy", "constitution.md")
_MAX_CONSTITUTION_CHARS = 20000


def load_constitution(workdir: Optional[str]) -> str:
    """Return contents of <workdir>/.cli-proxy/constitution.md, or "" on any failure."""
    if not workdir:
        return ""
    root = Path(str(workdir))
    path = root / _CONSTITUTION_RELATIVE_PATH
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_resolved)
        if not resolved.is_file():
            return ""
        with open(resolved, encoding="utf-8") as fh:
            return fh.read(_MAX_CONSTITUTION_CHARS)
    except FileNotFoundError:
        return ""
    except Exception:
        _log.exception("load_constitution failed path=%s", path)
        return ""
