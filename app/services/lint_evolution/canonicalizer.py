from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .rule_kinds import PATTERNS, UNKNOWN

_NON_WORD = re.compile(r"\W+", re.UNICODE)


@dataclass(frozen=True)
class CanonResult:
    rule_kind: str
    subject_hash: str
    matched_text: str


def _subject_from_text(text: str, *, max_chars: int = 80) -> str:
    head = (text or "").strip()[:max_chars]
    normalized = _NON_WORD.sub(" ", head).strip().lower()
    return normalized or "empty"


def _hash(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()[:12]


def canonicalize(text: str) -> CanonResult:
    raw = str(text or "")
    for entry in PATTERNS:
        match = entry.pattern.search(raw)
        if match:
            subject = _subject_from_text(raw)
            return CanonResult(rule_kind=entry.kind, subject_hash=_hash(subject), matched_text=match.group(0))
    return CanonResult(rule_kind=UNKNOWN, subject_hash=_hash(_subject_from_text(raw)), matched_text="")
