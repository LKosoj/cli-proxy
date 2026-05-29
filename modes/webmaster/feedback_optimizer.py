from __future__ import annotations

import re
from typing import Dict, Tuple


def _normalize_patch_text(value: object) -> str:
    return str(value or "").strip()


def _line_items(value: object) -> list[str]:
    raw = _normalize_patch_text(value)
    if not raw:
        return []
    lines: list[str] = []
    for part in raw.replace("\r", "\n").split("\n"):
        item = part.strip().lstrip("-").strip()
        if item:
            lines.append(item)
    return lines


def _rule_items(value: object) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_line_items(item))
        return out
    return _line_items(value)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        key = str(item or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(str(item).strip())
    return out


_SPECIFIC_PROMPT_LEARNING_PATTERNS = (
    re.compile(r"\brq-\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\btask[_\-\s]?\d+\b", re.IGNORECASE),
)


def _looks_task_specific_patch_text(value: object) -> bool:
    text = _normalize_patch_text(value)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SPECIFIC_PROMPT_LEARNING_PATTERNS)


def normalize_general_rules(value: object) -> list[str]:
    out: list[str] = []
    for item in _dedupe(_rule_items(value)):
        normalized = _normalize_patch_text(item)
        if not normalized:
            continue
        if _looks_task_specific_patch_text(normalized):
            continue
        out.append(normalized)
    return _dedupe(out)


def normalize_general_patch(patch: object) -> dict[str, object] | None:
    if not isinstance(patch, dict):
        return None
    normalized = {
        "added_rules": normalize_general_rules(patch.get("added_rules")),
        "changed_rules": normalize_general_rules(patch.get("changed_rules")),
        "removed_rules": normalize_general_rules(patch.get("removed_rules")),
        "reason": "",
        "expected_effect": "",
    }
    reason = _normalize_patch_text(patch.get("reason"))
    if reason and not _looks_task_specific_patch_text(reason):
        normalized["reason"] = reason
    expected = _normalize_patch_text(patch.get("expected_effect"))
    if expected and not _looks_task_specific_patch_text(expected):
        normalized["expected_effect"] = expected
    if not (normalized["added_rules"] or normalized["changed_rules"] or normalized["removed_rules"]):
        return None
    return normalized


def normalize_learning_payload(learning: object) -> dict[str, object]:
    if not isinstance(learning, dict):
        return {"patches": [], "active_version": 1}
    raw_patches = learning.get("patches")
    patches: list[dict[str, object]] = []
    if isinstance(raw_patches, list):
        for item in raw_patches:
            normalized_patch = normalize_general_patch(item)
            if normalized_patch is not None:
                patches.append(normalized_patch)
    try:
        version = int(learning.get("active_version", 1) or 1)
    except Exception:
        version = 1
    return {"patches": patches, "active_version": max(1, version)}


def apply_prompt_learning(base_prompt: str, learning: Dict[str, object]) -> Tuple[str, int]:
    normalized_learning = normalize_learning_payload(learning)
    patches = normalized_learning.get("patches")
    if not isinstance(patches, list) or not patches:
        return str(base_prompt or ""), int(normalized_learning.get("active_version", 1))
    lines = [str(base_prompt or "").strip()]
    added_rules: list[str] = []
    changed_rules: list[str] = []
    removed_rules: list[str] = []
    reasons: list[str] = []

    for idx, patch in enumerate(patches[-20:], start=1):
        if not isinstance(patch, dict):
            continue
        added_rules.extend(_rule_items(patch.get("added_rules")))
        changed_rules.extend(_rule_items(patch.get("changed_rules")))
        removed_rules.extend(_rule_items(patch.get("removed_rules")))
        reason = _normalize_patch_text(patch.get("reason"))
        if reason:
            reasons.append(f"{idx}. {reason}")

    added_rules = _dedupe(added_rules)
    changed_rules = _dedupe(changed_rules)
    removed_rules = _dedupe(removed_rules)

    if added_rules or changed_rules or removed_rules:
        lines.append("")
        lines.append("Дополнительные правила (накопленные коррекции):")
    if added_rules:
        lines.append("Новые правила:")
        lines.extend([f"- {x}" for x in added_rules])
    if changed_rules:
        lines.append("Измененные правила (заменяют прежние формулировки):")
        lines.extend([f"- {x}" for x in changed_rules])
    if removed_rules:
        lines.append("Отмененные правила (не применять):")
        lines.extend([f"- {x}" for x in removed_rules])
    if reasons:
        lines.append("Обоснования изменений:")
        lines.extend(reasons)

    version = int(normalized_learning.get("active_version", len(patches) + 1) or 1)
    return "\n".join([x for x in lines if x is not None]), version
