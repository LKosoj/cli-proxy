from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .json_normalizer import loads_safe
from .obligations import normalize_obligation_items
from utils.text import strip_ansi


class CLIResponseFormat:
    JSON_OBJECT = "json_object"
    CLAIM_BUNDLE_JSON = "claim_bundle_json"
    REPO_REVIEW_BUNDLE_JSON = "repo_review_bundle_json"
    SPEC_FIX_BUNDLE_JSON = "spec_fix_bundle_json"
    OBLIGATION_REVIEW_BUNDLE_JSON = "obligation_review_bundle_json"


class CLIOutputType:
    REPO_REVIEW_VERDICT = "repo_review_verdict"
    REPO_REVIEW_MISMATCH = "repo_review_mismatch"
    REPO_REVIEW_UNVERIFIED_CLAIM = "repo_review_unverified_claim"
    REPO_REVIEW_CORRECTION = "repo_review_correction"
    OBLIGATION_REVIEW_VERDICT = "obligation_review_verdict"
    OBLIGATION_BLOCKING_OPEN = "obligation_blocking_open"
    OBLIGATION_FALSE_CLOSURE = "obligation_false_closure"
    CLI_RETRY_NOTICE = "cli_retry_notice"
    DEGRADED_MODE = "degraded_mode"


_SPEC_FIX_MAX_CLOSED_OBLIGATIONS = 32
_SPEC_FIX_MAX_REMAINING_OBLIGATIONS = 24
_SPEC_FIX_MAX_CORRECTIONS = 24
_SPEC_FIX_MAX_CLAIMS = 20
_SPEC_FIX_MAX_CLAIM_EVIDENCE = 4
_SPEC_FIX_MAX_EVIDENCE = 24
_SPEC_FIX_MAX_DEGRADED_MODES = 12
_SPEC_FIX_TEXT_ITEM_LIMIT = 320
_SPEC_FIX_EVIDENCE_PREVIEW_LIMIT = 240
_SPEC_FIX_PATH_LIMIT = 240


def strip_outer_code_fence(text: str) -> str:
    raw = str(text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return raw


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _extract_balanced_json_objects(text: str) -> List[str]:
    """Extract top-level balanced JSON objects from *text* in O(n) time.

    Scans left-to-right, tracking brace depth and string context. When a
    top-level object closes, it is recorded and scanning resumes *after* it,
    so each character is visited at most once.
    """
    raw = str(text or "")
    if "{" not in raw:
        return []
    objects: List[tuple[int, int]] = []
    nested_candidates: List[tuple[int, int, int]] = []
    stack: List[int] = []
    in_string = False
    escape = False
    for idx, current in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif current == "\\":
                escape = True
            elif current == '"':
                in_string = False
            continue
        if current == '"':
            in_string = True
            continue
        if current == "{":
            stack.append(idx)
            continue
        if current != "}" or not stack:
            continue

        start = stack.pop()
        if stack:
            nested_candidates.append((start, idx + 1, stack[-1]))
        else:
            objects.append((start, idx + 1))

    if stack:
        # Treat unmatched outer braces as noise and keep their direct balanced children.
        unmatched_starts = set(stack)
        objects.extend(
            (start, end)
            for start, end, parent in nested_candidates
            if parent in unmatched_starts
        )
    return [raw[start:end].strip() for start, end in sorted(objects)]


def _truncate_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _truncate_string_list(items: List[Any], *, item_limit: int, text_limit: int) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for item in items:
        text = _truncate_text(item, limit=text_limit)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= item_limit:
            break
    return normalized


def _truncate_evidence_items(
    items: List[Dict[str, Any]],
    *,
    item_limit: int,
    preview_limit: int,
    path_limit: int,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        record = {
            "type": _truncate_text(item.get("type") or "text", limit=64) or "text",
            "path": _truncate_text(item.get("path") or "", limit=path_limit),
            "preview": _truncate_text(item.get("preview") or "", limit=preview_limit),
        }
        key = (record["type"], record["path"], record["preview"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(record)
        if len(normalized) >= item_limit:
            break
    return normalized


def _truncate_claim_items(
    items: List[Dict[str, Any]],
    *,
    item_limit: int,
    text_limit: int,
    evidence_limit: int,
    preview_limit: int,
    path_limit: int,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        record = {
            "claim_id": _truncate_text(item.get("claim_id") or "", limit=96),
            "status": str(item.get("status") or "").strip().lower() or "needs_check",
            "text": _truncate_text(item.get("text") or "", limit=text_limit),
            "evidence": _truncate_evidence_items(
                item.get("evidence") or [],
                item_limit=evidence_limit,
                preview_limit=preview_limit,
                path_limit=path_limit,
            ),
        }
        key = (record["claim_id"], record["status"], record["text"])
        if not record["claim_id"] or not record["text"] or key in seen:
            continue
        seen.add(key)
        normalized.append(record)
        if len(normalized) >= item_limit:
            break
    return normalized


def _truncate_obligation_items(
    items: List[Dict[str, Any]],
    *,
    item_limit: int,
    statement_limit: int,
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        record = {
            "obligation_id": _truncate_text(item.get("obligation_id") or item.get("id") or "", limit=120),
            "statement": _truncate_text(item.get("statement") or item.get("text") or "", limit=statement_limit),
            "status": str(item.get("status") or "").strip().lower(),
            "blocking": bool(item.get("blocking")),
        }
        key = (record["obligation_id"], record["statement"], record["status"])
        if not record["obligation_id"] or not record["statement"] or key in seen:
            continue
        seen.add(key)
        normalized.append(record)
        if len(normalized) >= item_limit:
            break
    return normalized


def _iter_json_object_candidates(raw_output: str) -> List[str]:
    raw = strip_ansi(str(raw_output or "")).strip()
    if not raw:
        return []
    candidates: List[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        text = str(candidate or "").strip()
        if not text or text in seen or not (text.startswith("{") and text.endswith("}")):
            return
        seen.add(text)
        candidates.append(text)

    stripped = strip_outer_code_fence(raw)
    _add(stripped)
    fenced_matches = list(_JSON_FENCE_RE.finditer(raw))
    for match in reversed(fenced_matches):
        _add(match.group(1))
    for candidate in reversed(_extract_balanced_json_objects(raw)):
        _add(candidate)
    if stripped != raw:
        for candidate in reversed(_extract_balanced_json_objects(stripped)):
            _add(candidate)
    return candidates


def _load_json_object(raw_output: str) -> Optional[Dict[str, Any]]:
    for candidate in _iter_json_object_candidates(raw_output):
        try:
            payload = loads_safe(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def retry_notice_output(message: str) -> Dict[str, Any]:
    text = str(message or "").strip()
    return {
        "type": CLIOutputType.CLI_RETRY_NOTICE,
        "content": text,
        "content_preview": text,
    }


def wrap_prompt_for_response_format(prompt: str, response_format: str) -> str:
    base = str(prompt or "").strip()
    if response_format == CLIResponseFormat.JSON_OBJECT:
        return (
            f"{base}\n\n"
            f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.JSON_OBJECT}\n"
            "Верни строго один JSON-объект без markdown, без code fences и без пояснений вне JSON.\n"
        )
    if response_format == CLIResponseFormat.CLAIM_BUNDLE_JSON:
        return (
            f"{base}\n\n"
            f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.CLAIM_BUNDLE_JSON}\n"
            "Верни строго JSON-объект без markdown и без code fences.\n"
            "Обязательные поля должны присутствовать всегда. Если данных нет, верни пустые массивы, а не пропускай поля.\n"
            "Схема:\n"
            "{\n"
            '  "final_text": "строка",\n'
            '  "claims": [\n'
            "    {\n"
            '      "claim_id": "claim_1",\n'
            '      "status": "confirmed|needs_check|unconfirmed",\n'
            '      "text": "строка",\n'
            '      "evidence": [\n'
            '        {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "evidence": [\n'
            '    {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "  ],\n"
            '  "open_gaps": ["строка"]\n'
            "}\n"
            "В final_text положи полный итоговый текст результата. "
            "Claims и evidence заполни только подтверждаемыми данными.\n"
        )
    if response_format == CLIResponseFormat.SPEC_FIX_BUNDLE_JSON:
        return (
            f"{base}\n\n"
            f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.SPEC_FIX_BUNDLE_JSON}\n"
            "Верни строго JSON-объект без markdown и без code fences.\n"
            "Обязательные поля должны присутствовать всегда. Если данных нет, верни пустые массивы.\n"
            "Держи machine-readable списки краткими и без повторов:\n"
            "- closed_obligations, remaining_obligations, corrections_applied: только ключевые пункты;\n"
            "- claims и evidence: только подтверждения, которые реально нужны для финальной фиксации;\n"
            "- не дублируй одно и то же evidence в десятках формулировок;\n"
            "- final_text должен содержать полный исправленный документ, а служебные массивы — только сжатую выжимку.\n"
            "Схема:\n"
            "{\n"
            '  "final_text": "строка",\n'
            '  "closed_obligations": ["строка"],\n'
            '  "remaining_obligations": [\n'
            '    {"obligation_id": "обязательный_id", "statement": "строка", "status": "open|unverified", "blocking": true}\n'
            "  ],\n"
            '  "corrections_applied": ["строка"],\n'
            '  "claims": [\n'
            "    {\n"
            '      "claim_id": "claim_1",\n'
            '      "status": "confirmed|needs_check|unconfirmed",\n'
            '      "text": "строка",\n'
            '      "evidence": [\n'
            '        {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "evidence": [\n'
            '    {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "  ],\n"
            '  "degraded_modes": ["строка"]\n'
            "}\n"
        )
    if response_format == CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON:
        return (
            f"{base}\n\n"
            f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON}\n"
            "Верни строго JSON-объект без markdown и без code fences.\n"
            "Это режим финальной repo-grounded сверки. Обязательные поля должны присутствовать всегда.\n"
            "Если список пустой, верни [], а не пропускай поле.\n"
            "Схема:\n"
            "{\n"
            '  "verdict": "строка",\n'
            '  "mismatches": ["строка"],\n'
            '  "unverified_claims": ["строка"],\n'
            '  "corrections": ["строка"],\n'
            '  "claims": [\n'
            "    {\n"
            '      "claim_id": "claim_1",\n'
            '      "status": "confirmed|needs_check|unconfirmed",\n'
            '      "text": "строка",\n'
            '      "evidence": [\n'
            '        {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "evidence": [\n'
            '    {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "  ],\n"
            '  "open_gaps": ["строка"]\n'
            "}\n"
            "verdict должен кратко фиксировать итог сверки. "
            "corrections перечисляй только как конкретные правки в документе.\n"
        )
    if response_format == CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON:
        return (
            f"{base}\n\n"
            f"CLI_RESPONSE_FORMAT: {CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON}\n"
            "Верни строго JSON-объект без markdown и без code fences.\n"
            "Обязательные поля должны присутствовать всегда. Если список пустой, верни [].\n"
            "Схема:\n"
            "{\n"
            '  "verdict": "строка",\n'
            '  "closed_blocking_obligations": ["строка"],\n'
            '  "open_blocking_obligations": [\n'
            '    {"obligation_id": "obligation_1", "statement": "строка", "status": "open|unverified", "blocking": true}\n'
            "  ],\n"
            '  "false_closures": [\n'
            '    {"obligation_id": "obligation_2", "statement": "строка", "status": "open", "blocking": true}\n'
            "  ],\n"
            '  "unsupported_assertions": ["строка"],\n'
            '  "required_corrections": ["строка"],\n'
            '  "claims": [\n'
            "    {\n"
            '      "claim_id": "claim_1",\n'
            '      "status": "confirmed|needs_check|unconfirmed",\n'
            '      "text": "строка",\n'
            '      "evidence": [\n'
            '        {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "evidence": [\n'
            '    {"type": "repo_evidence|text|file", "path": "строка", "preview": "строка"}\n'
            "  ],\n"
            '  "degraded_modes": ["строка"]\n'
            "}\n"
        )
    return base


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"confirmed", "needs_check", "unconfirmed"}:
        return status
    return "needs_check"


def normalize_evidence(items: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return normalized
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        record = {
            "type": str(item.get("type") or "text").strip() or "text",
            "path": str(item.get("path") or item.get("file_path") or "").strip(),
            "preview": str(item.get("preview") or item.get("content_preview") or item.get("content") or "").strip(),
        }
        key = (record["type"], record["path"], record["preview"])
        if key in seen:
            continue
        seen.add(key)
        normalized.append(record)
    return normalized


def _normalize_required_obligation_items(
    items: Any,
    *,
    allowed_statuses: set[str],
    require_blocking: bool,
) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(items, list):
        return None
    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        obligation_id = str(item.get("obligation_id") or item.get("id") or "").strip()
        statement = str(item.get("statement") or item.get("text") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        blocking = item.get("blocking")
        if not obligation_id or not statement or status not in allowed_statuses or not isinstance(blocking, bool):
            return None
        if require_blocking and not blocking:
            return None
        item_normalized = normalize_obligation_items([item])
        if len(item_normalized) != 1:
            return None
        normalized.append(item_normalized[0])
    return normalized


def _normalize_required_evidence_items(items: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(items, list):
        return None
    normalized: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            return None
        evidence_items = normalize_evidence([item])
        if len(evidence_items) != 1:
            return None
        record = evidence_items[0]
        key = (
            str(record.get("type") or ""),
            str(record.get("path") or ""),
            str(record.get("preview") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(record)
    return normalized


def _normalize_required_claim_items(items: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(items, list):
        return None
    normalized: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        claim_id = str(item.get("claim_id") or "").strip()
        text = str(item.get("text") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        evidence = _normalize_required_evidence_items(item.get("evidence"))
        if not claim_id or not text or status not in {"confirmed", "needs_check", "unconfirmed"} or evidence is None:
            return None
        normalized.append(
            {
                "claim_id": claim_id,
                "status": status,
                "text": text,
                "evidence": evidence,
            }
        )
    return normalized


def render_repo_review_text(bundle: Dict[str, Any]) -> str:
    verdict = str(bundle.get("verdict") or "").strip()
    sections = [
        ("VERDICT", [verdict] if verdict else []),
        ("MISMATCHES", [str(item).strip() for item in (bundle.get("mismatches") or []) if str(item).strip()]),
        ("UNVERIFIED_CLAIMS", [str(item).strip() for item in (bundle.get("unverified_claims") or []) if str(item).strip()]),
        ("CORRECTIONS", [str(item).strip() for item in (bundle.get("corrections") or []) if str(item).strip()]),
        ("OPEN_GAPS", [str(item).strip() for item in (bundle.get("open_gaps") or []) if str(item).strip()]),
    ]
    lines: List[str] = []
    for title, items in sections:
        lines.extend([title, ""])
        if items:
            if title == "VERDICT":
                lines.append(items[0])
            else:
                for item in items:
                    lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.extend(["", ""])
    return "\n".join(lines).strip()


def render_obligation_review_text(bundle: Dict[str, Any]) -> str:
    verdict = str(bundle.get("verdict") or "").strip()
    sections = [
        ("VERDICT", [verdict] if verdict else []),
        (
            "OPEN_BLOCKING_OBLIGATIONS",
            [
                str(item.get("statement") or "").strip()
                for item in (bundle.get("open_blocking_obligations") or [])
                if isinstance(item, dict) and str(item.get("statement") or "").strip()
            ],
        ),
        (
            "FALSE_CLOSURES",
            [
                str(item.get("statement") or "").strip()
                for item in (bundle.get("false_closures") or [])
                if isinstance(item, dict) and str(item.get("statement") or "").strip()
            ],
        ),
        (
            "UNSUPPORTED_ASSERTIONS",
            [str(item).strip() for item in (bundle.get("unsupported_assertions") or []) if str(item).strip()],
        ),
        (
            "REQUIRED_CORRECTIONS",
            [str(item).strip() for item in (bundle.get("required_corrections") or []) if str(item).strip()],
        ),
    ]
    lines: List[str] = []
    for title, items in sections:
        lines.extend([title, ""])
        if items:
            if title == "VERDICT":
                lines.append(items[0])
            else:
                for item in items:
                    lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.extend(["", ""])
    return "\n".join(lines).strip()


def parse_bundle_for_response_format(raw_output: str, response_format: str) -> Optional[Dict[str, Any]]:
    fmt = str(response_format or "").strip().lower()
    if not fmt:
        return None
    payload = _load_json_object(raw_output)
    if not isinstance(payload, dict):
        return None
    if response_format == CLIResponseFormat.CLAIM_BUNDLE_JSON:
        final_text = str(payload.get("final_text") or "").strip()
        if not final_text:
            return None
        claims_raw = payload.get("claims")
        evidence_raw = payload.get("evidence")
        open_gaps_raw = payload.get("open_gaps")
        if not all(isinstance(value, list) for value in (claims_raw, evidence_raw, open_gaps_raw)):
            return None
        claims = _normalize_required_claim_items(claims_raw)
        evidence = _normalize_required_evidence_items(evidence_raw)
        if claims is None or evidence is None:
            return None
        return {
            "final_text": final_text,
            "claims": claims,
            "evidence": evidence,
            "open_gaps": [str(item).strip() for item in open_gaps_raw if str(item).strip()],
        }
    if response_format == CLIResponseFormat.SPEC_FIX_BUNDLE_JSON:
        final_text = str(payload.get("final_text") or "").strip()
        if not final_text:
            return None
        closed_raw = payload.get("closed_obligations")
        remaining_raw = payload.get("remaining_obligations")
        corrections_raw = payload.get("corrections_applied")
        degraded_raw = payload.get("degraded_modes")
        if not all(isinstance(value, list) for value in (closed_raw, remaining_raw, corrections_raw, degraded_raw)):
            return None
        remaining_obligations = _normalize_required_obligation_items(
            remaining_raw,
            allowed_statuses={"open", "unverified"},
            require_blocking=True,
        )
        claims = _normalize_required_claim_items(payload.get("claims"))
        evidence = _normalize_required_evidence_items(payload.get("evidence"))
        if remaining_obligations is None or claims is None or evidence is None:
            return None
        return {
            "final_text": final_text,
            "closed_obligations": _truncate_string_list(
                closed_raw,
                item_limit=_SPEC_FIX_MAX_CLOSED_OBLIGATIONS,
                text_limit=_SPEC_FIX_TEXT_ITEM_LIMIT,
            ),
            "remaining_obligations": _truncate_obligation_items(
                remaining_obligations,
                item_limit=_SPEC_FIX_MAX_REMAINING_OBLIGATIONS,
                statement_limit=_SPEC_FIX_TEXT_ITEM_LIMIT,
            ),
            "corrections_applied": _truncate_string_list(
                corrections_raw,
                item_limit=_SPEC_FIX_MAX_CORRECTIONS,
                text_limit=_SPEC_FIX_TEXT_ITEM_LIMIT,
            ),
            "claims": _truncate_claim_items(
                claims,
                item_limit=_SPEC_FIX_MAX_CLAIMS,
                text_limit=_SPEC_FIX_TEXT_ITEM_LIMIT,
                evidence_limit=_SPEC_FIX_MAX_CLAIM_EVIDENCE,
                preview_limit=_SPEC_FIX_EVIDENCE_PREVIEW_LIMIT,
                path_limit=_SPEC_FIX_PATH_LIMIT,
            ),
            "evidence": _truncate_evidence_items(
                evidence,
                item_limit=_SPEC_FIX_MAX_EVIDENCE,
                preview_limit=_SPEC_FIX_EVIDENCE_PREVIEW_LIMIT,
                path_limit=_SPEC_FIX_PATH_LIMIT,
            ),
            "degraded_modes": _truncate_string_list(
                degraded_raw,
                item_limit=_SPEC_FIX_MAX_DEGRADED_MODES,
                text_limit=_SPEC_FIX_TEXT_ITEM_LIMIT,
            ),
        }
    if response_format == CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON:
        verdict = str(payload.get("verdict") or "").strip()
        if not verdict:
            return None
        mismatches_raw = payload.get("mismatches")
        unverified_raw = payload.get("unverified_claims")
        corrections_raw = payload.get("corrections")
        open_gaps_raw = payload.get("open_gaps")
        if not all(isinstance(value, list) for value in (mismatches_raw, unverified_raw, corrections_raw, open_gaps_raw)):
            return None
        claims = _normalize_required_claim_items(payload.get("claims"))
        evidence = _normalize_required_evidence_items(payload.get("evidence"))
        if claims is None or evidence is None:
            return None
        bundle = {
            "verdict": verdict,
            "mismatches": [str(item).strip() for item in mismatches_raw if str(item).strip()],
            "unverified_claims": [str(item).strip() for item in unverified_raw if str(item).strip()],
            "corrections": [str(item).strip() for item in corrections_raw if str(item).strip()],
            "claims": claims,
            "evidence": evidence,
            "open_gaps": [str(item).strip() for item in open_gaps_raw if str(item).strip()],
        }
        bundle["final_text"] = render_repo_review_text(bundle)
        return bundle
    if response_format == CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON:
        verdict = str(payload.get("verdict") or "").strip()
        if not verdict:
            return None
        closed_raw = payload.get("closed_blocking_obligations")
        open_raw = payload.get("open_blocking_obligations")
        false_raw = payload.get("false_closures")
        unsupported_raw = payload.get("unsupported_assertions")
        corrections_raw = payload.get("required_corrections")
        degraded_raw = payload.get("degraded_modes")
        if not all(
            isinstance(value, list)
            for value in (closed_raw, open_raw, false_raw, unsupported_raw, corrections_raw, degraded_raw)
        ):
            return None
        open_blocking_obligations = _normalize_required_obligation_items(
            open_raw,
            allowed_statuses={"open", "unverified"},
            require_blocking=True,
        )
        false_closures = _normalize_required_obligation_items(
            false_raw,
            allowed_statuses={"open", "unverified"},
            require_blocking=True,
        )
        claims = _normalize_required_claim_items(payload.get("claims"))
        evidence = _normalize_required_evidence_items(payload.get("evidence"))
        if open_blocking_obligations is None or false_closures is None or claims is None or evidence is None:
            return None
        bundle = {
            "verdict": verdict,
            "closed_blocking_obligations": [str(item).strip() for item in closed_raw if str(item).strip()],
            "open_blocking_obligations": open_blocking_obligations,
            "false_closures": false_closures,
            "unsupported_assertions": [str(item).strip() for item in unsupported_raw if str(item).strip()],
            "required_corrections": [str(item).strip() for item in corrections_raw if str(item).strip()],
            "claims": claims,
            "evidence": evidence,
            "degraded_modes": [str(item).strip() for item in degraded_raw if str(item).strip()],
        }
        bundle["final_text"] = render_obligation_review_text(bundle)
        return bundle
    return None


def repo_review_bundle_to_outputs(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    outputs: List[Dict[str, Any]] = []
    verdict = str(bundle.get("verdict") or "").strip()
    if verdict:
        outputs.append({"type": CLIOutputType.REPO_REVIEW_VERDICT, "content": verdict, "content_preview": verdict})
    for item in bundle.get("mismatches") or []:
        text = str(item or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.REPO_REVIEW_MISMATCH, "content": text, "content_preview": text})
    for item in bundle.get("unverified_claims") or []:
        text = str(item or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM, "content": text, "content_preview": text})
    for item in bundle.get("corrections") or []:
        text = str(item or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.REPO_REVIEW_CORRECTION, "content": text, "content_preview": text})
    for item in bundle.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        outputs.append(
            {
                "type": str(item.get("type") or "repo_evidence").strip() or "repo_evidence",
                "path": str(item.get("path") or "").strip(),
                "content_preview": str(item.get("preview") or "").strip(),
            }
        )
    for item in bundle.get("open_gaps") or []:
        text = str(item or "").strip()
        if text:
            outputs.append({"type": "open_gap", "content": text, "content_preview": text})
    return outputs


def obligation_review_bundle_to_outputs(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    outputs: List[Dict[str, Any]] = []
    verdict = str(bundle.get("verdict") or "").strip()
    if verdict:
        outputs.append({"type": CLIOutputType.OBLIGATION_REVIEW_VERDICT, "content": verdict, "content_preview": verdict})
    for item in bundle.get("open_blocking_obligations") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("statement") or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.OBLIGATION_BLOCKING_OPEN, "content": text, "content_preview": text})
    for item in bundle.get("false_closures") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("statement") or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.OBLIGATION_FALSE_CLOSURE, "content": text, "content_preview": text})
    for item in bundle.get("unsupported_assertions") or []:
        text = str(item or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM, "content": text, "content_preview": text})
    for item in bundle.get("required_corrections") or []:
        text = str(item or "").strip()
        if text:
            outputs.append({"type": CLIOutputType.REPO_REVIEW_CORRECTION, "content": text, "content_preview": text})
    for item in bundle.get("degraded_modes") or []:
        text = str(item or "").strip()
        if text:
            outputs.append(degraded_mode_output(text))
    for item in bundle.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        outputs.append(
            {
                "type": str(item.get("type") or "repo_evidence").strip() or "repo_evidence",
                "path": str(item.get("path") or "").strip(),
                "content_preview": str(item.get("preview") or "").strip(),
            }
        )
    return outputs


def degraded_mode_output(reason: str) -> Dict[str, Any]:
    text = str(reason or "").strip()
    return {
        "type": CLIOutputType.DEGRADED_MODE,
        "content": text,
        "content_preview": text,
    }


def collect_repo_review_runtime_gaps_from_outputs(output_groups: List[List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {
        "codebase_mismatches": [],
        "unverified_claims": [],
        "issues": [],
        "degraded_modes": [],
    }

    def _append(field_name: str, text: str) -> None:
        normalized = str(text or "").strip()
        if not normalized:
            return
        bucket = out.setdefault(field_name, [])
        if normalized not in bucket:
            bucket.append(normalized)

    for outputs in output_groups:
        for output in outputs:
            if not isinstance(output, dict):
                continue
            out_type = str(output.get("type") or "").strip()
            text = str(output.get("content_preview") or output.get("content") or "").strip()
            if out_type == CLIOutputType.REPO_REVIEW_MISMATCH:
                _append("codebase_mismatches", text)
            elif out_type == CLIOutputType.REPO_REVIEW_UNVERIFIED_CLAIM:
                _append("unverified_claims", text)
            elif out_type == CLIOutputType.REPO_REVIEW_CORRECTION:
                _append("issues", text)
            elif out_type == "open_gap":
                _append("issues", text)
            elif out_type == CLIOutputType.DEGRADED_MODE:
                _append("degraded_modes", text)
    return out
