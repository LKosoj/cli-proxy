from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё_:+.-]{1,}")
_SELECTED_SKILL_ID_RE = re.compile(r'"selected_skill_id"\s*:\s*"([^"\r\n]+)"')
_SELECTED_SKILL_IDS_FIRST_RE = re.compile(r'"selected_skill_ids"\s*:\s*\[\s*"([^"\r\n]+)"')
_CONFIDENCE_RE = re.compile(r'"confidence"\s*:\s*(-?\d+)')


def _clean_text(value: Any, *, max_len: int = 512) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _strip_ansi(value: Any) -> str:
    return _ANSI_ESCAPE_RE.sub("", str(value or ""))


def _task_hash(task_text: str) -> str:
    digest = hashlib.sha256(str(task_text or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _salvage_discovery_selector_payload(raw: str) -> Dict[str, Any] | None:
    text = str(raw or "")
    skill_id_match = _SELECTED_SKILL_ID_RE.search(text)
    list_match = _SELECTED_SKILL_IDS_FIRST_RE.search(text)
    confidence_match = _CONFIDENCE_RE.search(text)

    selected_skill_id = ""
    if skill_id_match:
        selected_skill_id = _clean_text(skill_id_match.group(1), max_len=128)
    elif list_match:
        selected_skill_id = _clean_text(list_match.group(1), max_len=128)

    confidence: int | None = None
    if confidence_match:
        try:
            confidence = int(confidence_match.group(1))
        except ValueError:
            confidence = None

    if not selected_skill_id and confidence is None:
        return None

    payload: Dict[str, Any] = {}
    if selected_skill_id:
        payload["selected_skill_id"] = selected_skill_id
        payload["selected_skill_ids"] = [selected_skill_id]
    if confidence is not None:
        payload["confidence"] = confidence
    return payload


def _normalize_mode(mode: Any) -> str:
    token = str(mode or "").strip().lower()
    return token or "suggest"


def _normalize_install_policy(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"manual", "admin_approve", "allowlisted_auto"}:
        return token
    return "manual"


def _extract_description_from_markdown(body: str) -> str:
    lines = [line.strip() for line in str(body or "").splitlines()]
    for line in lines:
        if not line or line.startswith("#"):
            continue
        return _clean_text(line, max_len=512)
    for line in lines:
        if line.startswith("#"):
            return _clean_text(line.lstrip("#").strip(), max_len=512)
    return ""


def _parse_front_matter(raw: str) -> tuple[Dict[str, Any], str]:
    text = str(raw or "")
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---", 4)
    if closing < 0:
        return {}, text
    header = text[4:closing]
    body = text[closing + 4:]
    if body.startswith("\n"):
        body = body[1:]
    try:
        payload = yaml.safe_load(header)
    except Exception:
        logger.exception("skill runtime: failed to parse front matter for acquired skill")
        payload = {}
    return payload if isinstance(payload, dict) else {}, body


def _dedupe_strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = _clean_text(item, max_len=256)
        if not token or token in seen:
            continue
        result.append(token)
        seen.add(token)
    return tuple(result)


def _tokenize_text(value: Any) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.finditer(str(value or "").lower()):
        token = match.group(0).strip("._:-+")
        if len(token) < 3 or token in seen:
            continue
        tokens.append(token)
        seen.add(token)
    return tuple(tokens)
