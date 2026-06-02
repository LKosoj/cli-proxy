from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

_CLARIFICATION_MARKER = "[NEEDS CLARIFICATION]"
_REQ_ID_RE = re.compile(r"\bREQ-(\d+)\b")
_REQ_LEADING_RE = re.compile(r"^\s*(REQ-\d+)\s*:", re.IGNORECASE)
_REQ_TRAILING_RE = re.compile(r"\(\s*(REQ-\d+)\s*\)\s*$", re.IGNORECASE)

_AC_LINE_RE = re.compile(r"^\s*[-*]\s+(.+)$")


@dataclass(frozen=True)
class EarsCriterion:
    raw: str
    pattern: str          # ubiquitous|event|state|optional|unwanted|unknown
    req_id: Optional[str]  # "REQ-3" if AC is bound to a requirement
    needs_clarification: bool


def _detect_pattern(text: str) -> str:
    stripped = text.strip()
    # Strip leading REQ-N: prefix before pattern detection
    body = re.sub(r"^\s*REQ-\d+\s*:\s*", "", stripped, flags=re.IGNORECASE).strip()
    upper = body.upper()
    # unwanted is checked first: IF…THEN is more specific than event/state/optional
    if re.search(r"\bIF\b.+\bTHEN\b", upper, re.DOTALL):
        return "unwanted"
    if upper.startswith("WHEN "):
        return "event"
    if upper.startswith("WHILE "):
        return "state"
    if upper.startswith("WHERE "):
        return "optional"
    if "SHALL" in upper:
        return "ubiquitous"
    return "unknown"


def _extract_req_id(text: str) -> Optional[str]:
    m = _REQ_LEADING_RE.match(text)
    if m:
        return m.group(1).upper()
    m = _REQ_TRAILING_RE.search(text)
    if m:
        return m.group(1).upper()
    return None


def parse_ears(text: str) -> EarsCriterion:
    """Parse a single EARS criterion string."""
    raw = str(text or "").strip()
    needs_clarification = _CLARIFICATION_MARKER.lower() in raw.lower()
    req_id = _extract_req_id(raw)
    pattern = _detect_pattern(raw)
    return EarsCriterion(raw=raw, pattern=pattern, req_id=req_id, needs_clarification=needs_clarification)


def parse_ears_block(spec_md: str) -> List[EarsCriterion]:
    """Extract AC lines from spec.md body and parse each as EarsCriterion."""
    results: List[EarsCriterion] = []
    in_ac_section = False
    for line in spec_md.splitlines():
        low = line.strip().lower()
        if low.startswith("#") and "acceptance" in low:
            in_ac_section = True
            continue
        if low.startswith("#") and in_ac_section:
            in_ac_section = False
        m = _AC_LINE_RE.match(line)
        if m:
            item_text = m.group(1).strip()
            if in_ac_section or _REQ_ID_RE.search(item_text) or "shall" in item_text.lower():
                results.append(parse_ears(item_text))
    return results


def validate_ears(spec_md: str) -> List[str]:
    """Return list of problems found in spec_md AC block; empty list = valid."""
    criteria = parse_ears_block(spec_md)
    problems: List[str] = []
    for c in criteria:
        if c.pattern == "unknown":
            problems.append(f"AC без EARS-паттерна: {c.raw[:80]}")
        if c.needs_clarification:
            problems.append(f"AC требует уточнения: {c.raw[:80]}")
    return problems


def extract_clarification_questions(payload: dict) -> List[str]:
    """Return list of EARS strings / requirement texts that need clarification."""
    seen: set = set()
    result: List[str] = []
    for c in payload.get("acceptance_criteria") or []:
        ears = (c.get("ears") or "") if isinstance(c, dict) else ""
        if ears and parse_ears(ears).needs_clarification and ears not in seen:
            seen.add(ears)
            result.append(ears)
    for r in payload.get("requirements") or []:
        text = (r.get("text") or "") if isinstance(r, dict) else ""
        # case-insensitive — согласовано с parse_ears (тот тоже сравнивает в lower).
        if text and _CLARIFICATION_MARKER.lower() in text.lower() and text not in seen:
            seen.add(text)
            result.append(text)
    return result
