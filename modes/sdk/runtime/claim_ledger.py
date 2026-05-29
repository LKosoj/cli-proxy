from __future__ import annotations

from typing import Any, Dict, List


VALID_CLAIM_STATUSES = {"confirmed", "needs_check", "unconfirmed"}
VALID_FINAL_USAGES = {"fact", "open_question", "blocked_item"}


def _normalize_evidence_list(value: Any) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        item = {
            "type": str(raw.get("type") or "text").strip() or "text",
            "path": str(raw.get("path") or "").strip(),
            "preview": str(raw.get("preview") or "").strip(),
        }
        if not item["path"] and not item["preview"]:
            continue
        items.append(item)
    return items


def normalize_claim_entry(entry: Dict[str, Any], *, fallback_id: str) -> Dict[str, Any]:
    raw = dict(entry or {}) if isinstance(entry, dict) else {}
    claim_id = str(raw.get("claim_id") or "").strip() or fallback_id
    status = str(raw.get("status") or "").strip().lower()
    if status not in VALID_CLAIM_STATUSES:
        status = "needs_check"
    text = str(raw.get("text") or "").strip()
    source_step_id = str(raw.get("source_step_id") or raw.get("task_id") or "").strip()
    component_scope = str(raw.get("component_scope") or "general").strip() or "general"
    allowed_final_usage = str(raw.get("allowed_final_usage") or "").strip().lower()
    if allowed_final_usage not in VALID_FINAL_USAGES:
        if status == "confirmed":
            allowed_final_usage = "fact"
        elif status == "needs_check":
            allowed_final_usage = "open_question"
        else:
            allowed_final_usage = "blocked_item"
    return {
        "claim_id": claim_id,
        "status": status,
        "text": text,
        "evidence": _normalize_evidence_list(raw.get("evidence")),
        "source_step_id": source_step_id,
        "component_scope": component_scope,
        "allowed_final_usage": allowed_final_usage,
        "task_id": source_step_id,
        "title": str(raw.get("title") or "").strip(),
        "step_artifact": str(raw.get("step_artifact") or "").strip(),
    }


def normalize_claim_ledger(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(entries or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = normalize_claim_entry(raw, fallback_id=f"claim_{idx}")
        if not item["text"]:
            continue
        claim_id = item["claim_id"]
        if claim_id in seen_ids:
            claim_id = f"{claim_id}_{idx}"
            item["claim_id"] = claim_id
        seen_ids.add(claim_id)
        normalized.append(item)
    return normalized


def validate_claim_ledger(entries: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(entries or [], start=1):
        if not isinstance(item, dict):
            errors.append(f"claim[{idx}] is not an object")
            continue
        claim_id = str(item.get("claim_id") or "").strip()
        if not claim_id:
            errors.append(f"claim[{idx}] missing claim_id")
        elif claim_id in seen_ids:
            errors.append(f"duplicate claim_id: {claim_id}")
        else:
            seen_ids.add(claim_id)
        status = str(item.get("status") or "").strip().lower()
        if status not in VALID_CLAIM_STATUSES:
            errors.append(f"claim[{idx}] invalid status: {status}")
        if not str(item.get("text") or "").strip():
            errors.append(f"claim[{idx}] missing text")
        usage = str(item.get("allowed_final_usage") or "").strip().lower()
        if usage not in VALID_FINAL_USAGES:
            errors.append(f"claim[{idx}] invalid allowed_final_usage: {usage}")
        if not str(item.get("source_step_id") or "").strip():
            warnings.append(f"claim[{idx}] missing source_step_id")
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"claim[{idx}] evidence must be a list")
    return {"errors": errors, "warnings": warnings}
