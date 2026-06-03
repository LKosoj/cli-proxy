"""Append-only architecture decisions log for SDD multi-feature continuity.

Mirrors `constitution.py`: a read-only loader plus an idempotent appender. The
constitution holds systemic principles; `decisions.md` holds per-feature historical
rationale (out-of-scope + key plan decisions) deposited at the final feature gate so
later features inherit prior intent without reading old spec/plan/tasks archives.
"""
from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from typing import Optional, Sequence

_log = logging.getLogger(__name__)

_DECISIONS_RELATIVE_PATH = os.path.join(".cli-proxy", "decisions.md")
_MAX_DECISIONS_CHARS = 20000
_HEADER = "# Architecture Decisions\n"


def load_decisions(workdir: Optional[str]) -> str:
    """Return contents of <workdir>/.cli-proxy/decisions.md, or "" on any failure."""
    if not workdir:
        return ""
    root = Path(str(workdir))
    path = root / _DECISIONS_RELATIVE_PATH
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_resolved)
        if not resolved.is_file():
            return ""
        with open(resolved, encoding="utf-8") as fh:
            return fh.read(_MAX_DECISIONS_CHARS)
    except FileNotFoundError:
        return ""
    except Exception:
        _log.exception("load_decisions failed path=%s", path)
        return ""


def _section_marker(feature_slug: str) -> str:
    return f"## {feature_slug}"


def append_decision(
    workdir: Optional[str],
    *,
    feature_slug: str,
    out_of_scope: Optional[Sequence[str]] = None,
    plan_decisions: Optional[Sequence[str]] = None,
) -> bool:
    """Append an ADR-lite block for *feature_slug*. Idempotent per slug.

    Returns True if a new block was written, False if skipped (no slug, unsafe path,
    already recorded, or write failure).
    """
    slug = str(feature_slug or "").strip()
    if not workdir or not slug:
        return False
    root = Path(str(workdir))
    path = root / _DECISIONS_RELATIVE_PATH
    try:
        root_resolved = root.resolve(strict=True)
        (root / ".cli-proxy").resolve(strict=False).relative_to(root_resolved)
        # Validate the FINAL file path too — a symlinked decisions.md must not escape workdir.
        path.resolve(strict=False).relative_to(root_resolved)
    except Exception:
        return False

    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8")
        except Exception:
            existing = ""

    marker = _section_marker(slug)
    if any(
        line.strip() == marker or line.strip().startswith(marker + " (")
        for line in existing.splitlines()
    ):
        return False  # idempotent: this feature already recorded (marker carries a date suffix)

    oos = [str(x).strip() for x in (out_of_scope or []) if str(x or "").strip()]
    decs = [str(x).strip() for x in (plan_decisions or []) if str(x or "").strip()]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    lines = [f"{marker} ({stamp})", "", "### Out of scope"]
    lines += [f"- {x}" for x in oos] if oos else ["- (none)"]
    lines += ["", "### Key decisions"]
    lines += [f"- {x}" for x in decs] if decs else ["- (none)"]
    block = "\n".join(lines) + "\n"

    if existing:
        sep = "" if existing.endswith("\n") else "\n"
        content = existing + sep + "\n" + block
    else:
        content = _HEADER + "\n" + block

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        _log.exception("append_decision write failed path=%s", path)
        return False


__all__ = ["load_decisions", "append_decision"]
