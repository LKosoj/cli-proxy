from __future__ import annotations

from typing import Any, Dict, List

from modes.analyst.template_service import assess_template_fitness
from .evidence_pipeline import claim_has_repo_anchor
from .final_qc import compute_runtime_readiness, strip_model_readiness_sections


def _routing_quality(template_resolution: Dict[str, Any] | None) -> Dict[str, Any]:
    payload = dict(template_resolution or {}) if isinstance(template_resolution, dict) else {}
    effective = str(payload.get("effective_template_id") or "").strip().lower()
    fitness = assess_template_fitness(
        selected_template_id=str(payload.get("selected_template_id") or "").strip(),
        intent_template_id=str(payload.get("intent_template_id") or "").strip(),
        effective_template_id=effective,
        document_kind=str(payload.get("document_kind") or "").strip(),
        change_scope=str(payload.get("change_scope") or "").strip(),
        runtime_template_id=str(payload.get("runtime_template_id") or "").strip(),
    )
    if not bool(fitness.get("applicable")):
        return {
            "applicable": False,
            "correct": None,
            "expected_template_id": "",
            "effective_template_id": effective,
            "reason": "",
        }
    return {
        "applicable": True,
        "correct": str(fitness.get("status") or "") == "ok",
        "expected_template_id": str(fitness.get("expected_template_id") or ""),
        "effective_template_id": effective,
        "reason": str(fitness.get("reason") or ""),
    }


def build_analyst_quality_metrics(
    *,
    claim_ledger: List[Dict[str, Any]],
    assessment: Dict[str, Any],
    repo_grounded_required: bool,
    required_step_statuses: Dict[str, str],
    model_text_before_runtime: str,
    structured_bundle_calls: int,
    structured_bundle_successes: int,
    cli_fallbacks: int,
    retry_successes: int,
    retry_exhausted: int,
    structured_bundle_stage_stats: Dict[str, Dict[str, int]] | None = None,
    template_resolution: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    confirmed_claims = 0
    confirmed_without_anchor = 0
    invented_claims_by_source: Dict[str, int] = {}
    for claim in claim_ledger or []:
        if not isinstance(claim, dict):
            continue
        status = str(claim.get("status") or "").strip().lower()
        if status != "confirmed":
            continue
        confirmed_claims += 1
        if not claim_has_repo_anchor(claim):
            confirmed_without_anchor += 1
            source_key = str(claim.get("source_step_id") or "(unknown)").strip() or "(unknown)"
            invented_claims_by_source[source_key] = int(invented_claims_by_source.get(source_key) or 0) + 1

    readiness = compute_runtime_readiness(
        assessment,
        repo_grounded_required=repo_grounded_required,
        required_step_statuses=required_step_statuses,
    )
    runtime_verdict = str(readiness.get("verdict") or "").strip()
    original_text = str(model_text_before_runtime or "")
    cleaned_text = strip_model_readiness_sections(original_text)
    optimistic_status_removed = cleaned_text != original_text and "готово к реализации" in original_text.lower()

    fallback_count = max(0, int(cli_fallbacks or 0))
    structured_calls = max(0, int(structured_bundle_calls or 0))
    structured_successes = max(0, int(structured_bundle_successes or 0))
    retry_success_count = max(0, int(retry_successes or 0))
    retry_exhausted_count = max(0, int(retry_exhausted or 0))

    structured_bundle_parse_rate = (
        float(structured_successes) / float(structured_calls) if structured_calls > 0 else 1.0
    )
    cli_fallback_rate = (
        float(fallback_count) / float(structured_calls) if structured_calls > 0 else 0.0
    )
    retry_rate = (
        float(retry_success_count + retry_exhausted_count) / float(structured_calls)
        if structured_calls > 0
        else 0.0
    )
    invented_claim_rate = (
        float(confirmed_without_anchor) / float(confirmed_claims) if confirmed_claims > 0 else 0.0
    )
    false_ready_rate = (
        1.0 if optimistic_status_removed and runtime_verdict != "Готово к реализации" else 0.0
    )
    structured_bundle_stages: Dict[str, Dict[str, float | int]] = {}
    for stage_name, raw_stats in dict(structured_bundle_stage_stats or {}).items():
        stats = dict(raw_stats or {}) if isinstance(raw_stats, dict) else {}
        stage_calls = max(0, int(stats.get("calls") or 0))
        stage_successes = max(0, int(stats.get("successes") or 0))
        stage_fallbacks = max(0, int(stats.get("fallbacks") or 0))
        stage_retry_successes = max(0, int(stats.get("retry_successes") or 0))
        stage_retry_exhausted = max(0, int(stats.get("retry_exhausted") or 0))
        structured_bundle_stages[str(stage_name or "unknown")] = {
            "calls": stage_calls,
            "successes": stage_successes,
            "fallbacks": stage_fallbacks,
            "retry_successes": stage_retry_successes,
            "retry_exhausted": stage_retry_exhausted,
            "parse_rate": float(stage_successes) / float(stage_calls) if stage_calls > 0 else 1.0,
        }
    routing = _routing_quality(template_resolution)

    return {
        "structured_bundle_calls": structured_calls,
        "structured_bundle_successes": structured_successes,
        "structured_bundle_parse_rate": structured_bundle_parse_rate,
        "cli_fallback_count": fallback_count,
        "cli_fallback_rate": cli_fallback_rate,
        "retry_success_count": retry_success_count,
        "retry_exhausted_count": retry_exhausted_count,
        "retry_rate": retry_rate,
        "confirmed_claims": confirmed_claims,
        "confirmed_claims_without_anchor": confirmed_without_anchor,
        "invented_claim_rate": invented_claim_rate,
        "invented_claims_by_source": invented_claims_by_source,
        "runtime_verdict": runtime_verdict,
        "optimistic_status_removed": optimistic_status_removed,
        "false_ready_rate": false_ready_rate,
        "blocking_reasons": list(readiness.get("blocking_reasons") or []),
        "warning_reasons": list(readiness.get("warning_reasons") or []),
        "structured_bundle_stages": structured_bundle_stages,
        "routing": routing,
        "routing_correctness": (
            1.0
            if routing.get("correct") is True
            else 0.0
            if routing.get("correct") is False
            else None
        ),
    }
