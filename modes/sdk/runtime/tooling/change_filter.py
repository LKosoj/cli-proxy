"""
Utilities for filtering noisy/low-signal file changes before sending them to LLMs.

Problem this solves:
- git outputs (log --stat, diff --stat, status --porcelain, diff --name-status) can include huge
  amounts of irrelevant/generated content (node_modules, build artifacts, caches, binaries).
- the Manager prompt becomes bloated and drowns the actual task context.

This module keeps the logic centralized and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


NOISE_DIR_SEGMENTS = {
    ".git",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cli-proxy",
    ".manager",
    ".manager_archive",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".vite",
}

LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
}

SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
}

NOISE_EXTENSIONS = {
    ".map",  # sourcemaps
    ".db",
    ".sqlite",
    ".sqlite3",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
}


def _norm_path(p: str) -> str:
    p = (p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _split_segments(p: str) -> List[str]:
    p = _norm_path(p)
    return [seg for seg in p.split("/") if seg]


def _ext_lower(filename: str) -> str:
    name = (filename or "").strip()
    dot = name.rfind(".")
    return name[dot:].lower() if dot != -1 else ""


def is_noise_path(path: str) -> Tuple[bool, Optional[str]]:
    """
    Returns (is_noise, reason_key).
    reason_key is a stable string for summarizing filtered items.
    """
    p = _norm_path(path)
    if not p:
        return False, None

    segs = _split_segments(p)
    for seg in segs:
        if seg in NOISE_DIR_SEGMENTS:
            return True, seg

    base = segs[-1] if segs else p
    if base in SENSITIVE_FILENAMES or base.startswith(".env."):
        return True, "sensitive"
    if base in LOCKFILES:
        return True, "lockfile"

    ext = _ext_lower(base)
    if ext in NOISE_EXTENSIONS:
        return True, "binary"

    return False, None


@dataclass(frozen=True)
class FilterSummary:
    kept: int
    filtered: int
    filtered_by_reason: Dict[str, int]

    def format_ru(self) -> str:
        if self.filtered <= 0:
            return ""
        parts: List[str] = []
        for k in sorted(self.filtered_by_reason.keys()):
            parts.append(f"{k}={self.filtered_by_reason[k]}")
        detail = (", ".join(parts)) if parts else ""
        if detail:
            return f"(скрыто {self.filtered} низкосигнальных путей: {detail})"
        return f"(скрыто {self.filtered} низкосигнальных путей)"


def filter_paths(paths: Iterable[str]) -> Tuple[List[str], FilterSummary]:
    kept: List[str] = []
    filtered_by: Dict[str, int] = {}
    filtered = 0
    total = 0
    for p in paths:
        total += 1
        noisy, reason = is_noise_path(p)
        if noisy:
            filtered += 1
            key = reason or "other"
            filtered_by[key] = filtered_by.get(key, 0) + 1
            continue
        kept.append(p)
    return kept, FilterSummary(kept=len(kept), filtered=filtered, filtered_by_reason=filtered_by)


def cap_lines(lines: List[str], max_lines: int) -> Tuple[List[str], int]:
    if max_lines <= 0:
        return [], len(lines)
    if len(lines) <= max_lines:
        return lines, 0
    more = len(lines) - max_lines
    return lines[:max_lines], more


def filter_git_porcelain_lines(lines: Iterable[str]) -> Tuple[List[str], FilterSummary]:
    """
    Filter `git status --porcelain` lines.
    Path is usually after the first 3 characters.
    Renames look like: 'R  old -> new'
    """
    kept: List[str] = []
    filtered_by: Dict[str, int] = {}
    filtered = 0
    total = 0
    for raw in lines:
        total += 1
        line = (raw or "").rstrip("\n")
        if not line.strip():
            continue
        payload = line[3:].strip() if len(line) >= 4 else ""
        candidates = [p.strip() for p in payload.split(" -> ") if p.strip()] if payload else []
        if not candidates:
            # If we can't parse, keep it to avoid losing information.
            kept.append(line)
            continue
        noisy_all = True
        first_reason: Optional[str] = None
        for p in candidates:
            noisy, reason = is_noise_path(p)
            if not noisy:
                noisy_all = False
                break
            if first_reason is None:
                first_reason = reason
        if noisy_all:
            filtered += 1
            key = first_reason or "other"
            filtered_by[key] = filtered_by.get(key, 0) + 1
            continue
        kept.append(line)
    return kept, FilterSummary(kept=len(kept), filtered=filtered, filtered_by_reason=filtered_by)


def filter_git_name_status_lines(lines: Iterable[str]) -> Tuple[List[str], FilterSummary]:
    """
    Filter `git diff --name-status` lines.
    Examples:
    - 'M\\tpath'
    - 'R100\\told\\tnew'
    """
    kept: List[str] = []
    filtered_by: Dict[str, int] = {}
    filtered = 0
    total = 0
    for raw in lines:
        total += 1
        line = (raw or "").rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        paths = [p for p in parts[1:] if p.strip()]
        if not paths:
            kept.append(line)
            continue
        noisy_all = True
        first_reason: Optional[str] = None
        for p in paths:
            noisy, reason = is_noise_path(p)
            if not noisy:
                noisy_all = False
                break
            if first_reason is None:
                first_reason = reason
        if noisy_all:
            filtered += 1
            key = first_reason or "other"
            filtered_by[key] = filtered_by.get(key, 0) + 1
            continue
        kept.append(line)
    return kept, FilterSummary(kept=len(kept), filtered=filtered, filtered_by_reason=filtered_by)


def filter_git_stat_text(stat_text: str) -> Tuple[str, FilterSummary]:
    """
    Filter `git diff --stat`-like text.
    Keeps non-file lines (commit subject, blank lines, totals), filters file lines that contain '|'.
    """
    lines = (stat_text or "").splitlines()
    kept: List[str] = []
    filtered_by: Dict[str, int] = {}
    filtered = 0
    for line in lines:
        if "|" not in line:
            kept.append(line)
            continue
        path_part = line.split("|", 1)[0].strip()
        noisy, reason = is_noise_path(path_part)
        if noisy:
            filtered += 1
            key = reason or "other"
            filtered_by[key] = filtered_by.get(key, 0) + 1
            continue
        kept.append(line)
    out = "\n".join(kept).strip("\n")
    return out, FilterSummary(
        kept=len([line for line in kept if line.strip()]),
        filtered=filtered,
        filtered_by_reason=filtered_by,
    )


def format_git_log_name_status(output: str, *, max_lines: int = 80) -> str:
    """
    Takes output of:
      git log -1 --name-status --format=%s (%h)
    and returns a filtered, capped string.
    """
    raw = (output or "").strip("\n")
    if not raw.strip():
        return ""
    lines = raw.splitlines()
    head = lines[0].rstrip()
    rest = [ln.rstrip() for ln in lines[1:] if ln.strip()]
    rest_filtered, summ = filter_git_name_status_lines(rest)
    rest_filtered, more = cap_lines(rest_filtered, max_lines=max_lines)
    out_lines: List[str] = [head]
    if rest_filtered:
        out_lines.append("")
        out_lines.extend(rest_filtered)
    note = summ.format_ru()
    if more > 0:
        if note:
            note = f"{note} (+{more} строк сверху)"
        else:
            note = f"(+{more} строк сверху)"
    if note:
        out_lines.append("")
        out_lines.append(note)
    return "\n".join(out_lines).strip()
