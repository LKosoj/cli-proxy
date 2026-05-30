from __future__ import annotations

from typing import Any, Dict, List, Optional


MISSING = object()


def clean_text(value: Any, *, max_len: int = 256) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def clean_optional_text(value: Any, *, max_len: int = 256) -> Optional[str]:
    text = clean_text(value, max_len=max_len)
    return text or None


def as_list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = clean_text(item, max_len=128)
        if not cleaned or cleaned in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned)
    return result


def nested_get(payload: Any, path: str) -> Any:
    current = payload
    for token in [part for part in str(path or "").split(".") if part]:
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        return MISSING
    return current


def summarize_run_skill_log(events: List[Dict[str, Any]], state: Dict[str, Any]) -> List[str]:
    summary: list[str] = []
    seen: set[str] = set()
    for event in reversed(list(events or [])):
        if not isinstance(event, dict):
            continue
        event_type = clean_text(event.get("event_type"), max_len=64)
        entry = ""
        if event_type in {"cli_skill_context_applied", "skill_selection"}:
            skill_ids = event.get("selected_skill_ids")
            if not isinstance(skill_ids, list):
                skill_ids = event.get("selected_skills")
            skills = [
                clean_text(item, max_len=64)
                for item in list(skill_ids or [])
                if clean_text(item, max_len=64)
            ]
            if skills:
                prefix = "Injected" if event_type == "cli_skill_context_applied" else "Selected"
                entry = f"{prefix}: {', '.join(skills)}"
        elif event_type == "skill_install":
            skill_id = clean_text(event.get("skill_id"), max_len=64)
            if skill_id:
                entry = f"Installed: {skill_id}"
        elif event_type == "skill_discovery":
            skills = [
                clean_text(item, max_len=64)
                for item in list(event.get("discovered_skills") or [])
                if clean_text(item, max_len=64)
            ]
            if skills:
                entry = f"Discovered: {', '.join(skills)}"
        elif event_type == "skill_promote_global":
            skill_id = clean_text(event.get("skill_id"), max_len=64)
            if skill_id:
                entry = f"Promoted: {skill_id}"
        if entry and entry not in seen:
            summary.append(entry)
            seen.add(entry)
        if len(summary) >= 3:
            break
    if summary:
        return summary
    fallback_skills = [
        clean_text(item, max_len=64)
        for item in list(state.get("selected_skill_ids") or [])
        if clean_text(item, max_len=64)
    ]
    if fallback_skills:
        return [f"Injected: {', '.join(fallback_skills[:4])}"]
    return []
