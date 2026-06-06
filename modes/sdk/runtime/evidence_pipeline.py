from __future__ import annotations

import re
from typing import Any, Dict, List


_LOCAL_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((/[^)\s]+)\)")
_LOCAL_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_./-])(/[^)\]>\s`]+)")


def _is_codebase_map_path(path: str) -> bool:
    normalized = "/" + str(path or "").strip().replace("\\", "/").lower().lstrip("/")
    return "/.cli-proxy/.codebase_map/" in normalized or normalized.endswith("/.cli-proxy/.codebase_map")


def _is_orchestrator_artifact_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lower()
    return (
        "/_orchestrator/" in normalized
        or normalized.endswith("/_orchestrator")
        or (
            ("/.cli-proxy/runs/" in normalized or ".cli-proxy/runs/" in normalized)
            and "/artifacts/" in normalized
        )
    )


def _is_repo_file_anchor_path(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    return not _is_codebase_map_path(normalized) and not _is_orchestrator_artifact_path(normalized)


def _strip_line_suffix(path: str) -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return ""
    match = re.match(r"^(.*?)(?::\d+)?$", normalized)
    return str(match.group(1) if match else normalized).strip()


def _text_contains_repo_anchor(text: Any) -> bool:
    source = str(text or "").strip()
    if not source:
        return False
    for pattern in (_LOCAL_MARKDOWN_LINK_RE, _LOCAL_PATH_TOKEN_RE):
        for match in pattern.finditer(source):
            candidate = _strip_line_suffix(match.group(1))
            if _is_repo_file_anchor_path(candidate):
                return True
    return False


def collect_step_evidence(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    outputs = entry.get("outputs") or []
    if isinstance(outputs, list):
        for output in outputs[:5]:
            if not isinstance(output, dict):
                continue
            preview = str(output.get("content_preview") or output.get("content") or "").strip()
            ref_path = str(output.get("path") or output.get("file_path") or "").strip()
            evidence.append(
                {
                    "type": str(output.get("type") or "text").strip() or "text",
                    "path": ref_path,
                    "preview": preview,
                }
            )
    artifacts = entry.get("artifacts") or []
    if isinstance(artifacts, list):
        for artifact in artifacts[:5]:
            if not isinstance(artifact, dict):
                continue
            evidence.append(
                {
                    "type": str(artifact.get("type") or "file").strip() or "file",
                    "path": str(artifact.get("path") or "").strip(),
                    "preview": "",
                }
            )
    deduped: List[Dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for ev in evidence:
        key = (
            str(ev.get("type") or ""),
            str(ev.get("path") or ""),
            str(ev.get("preview") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(ev)
    return deduped


def claim_has_repo_anchor(claim: Dict[str, Any]) -> bool:
    evidence = claim.get("evidence") or []
    if not isinstance(evidence, list):
        return _text_contains_repo_anchor(claim.get("text"))
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        path = _strip_line_suffix(ev.get("path") or "")
        if _is_repo_file_anchor_path(path):
            return True
    return _text_contains_repo_anchor(claim.get("text"))


def claim_uses_only_codebase_map_evidence(claim: Dict[str, Any]) -> bool:
    evidence = claim.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        return False
    has_codebase_map = False
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        path = str(ev.get("path") or "").strip()
        preview = str(ev.get("preview") or "").strip().lower()
        if _is_repo_file_anchor_path(path):
            return False
        if _is_codebase_map_path(path) or ".codebase_map" in preview or "codebase map" in preview:
            has_codebase_map = True
    return has_codebase_map


def _claim_has_captured_evidence(claim: Dict[str, Any]) -> bool:
    evidence = claim.get("evidence") or []
    return isinstance(evidence, list) and any(
        isinstance(ev, dict)
        and (
            str(ev.get("path") or "").strip()
            or str(ev.get("preview") or "").strip()
        )
        for ev in evidence
    )


def claim_is_confirmable(
    claim: Dict[str, Any],
    *,
    repo_grounded_required: bool,
) -> bool:
    if not _claim_has_captured_evidence(claim):
        return False
    if claim_uses_only_codebase_map_evidence(claim):
        return False
    if repo_grounded_required and not claim_has_repo_anchor(claim):
        return False
    return True


def verify_claim_ledger(
    claim_ledger: List[Dict[str, Any]],
    *,
    repo_grounded_required: bool,
) -> Dict[str, List[str]]:
    evidence_gaps: List[str] = []
    codebase_map_gaps: List[str] = []
    for claim in claim_ledger:
        if not isinstance(claim, dict):
            continue
        status = str(claim.get("status") or "").strip().lower()
        final_usage = str(claim.get("allowed_final_usage") or "").strip().lower()
        if status != "confirmed" and final_usage != "fact":
            continue
        text = str(claim.get("text") or "").strip()
        claim_label = "Confirmed" if status == "confirmed" else "Final"
        if not _claim_has_captured_evidence(claim):
            evidence_gaps.append(
                f"{claim_label} claim without captured evidence: {text[:180]}"
            )
            continue
        if claim_uses_only_codebase_map_evidence(claim):
            msg = f"{claim_label} claim relies only on Codebase Map navigation evidence: {text[:180]}"
            codebase_map_gaps.append(msg)
            evidence_gaps.append(msg)
            continue
        if repo_grounded_required and not claim_has_repo_anchor(claim):
            evidence_gaps.append(
                f"{claim_label} repo-grounded claim without repo/file anchor: {text[:180]}"
            )
    return {"evidence_gaps": evidence_gaps, "codebase_map_gaps": codebase_map_gaps}
