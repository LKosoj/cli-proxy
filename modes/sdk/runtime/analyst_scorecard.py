from __future__ import annotations

from typing import Any, Dict, Optional


DEFAULT_RELEASE_THRESHOLDS: Dict[str, float] = {
    "false_ready_rate": 0.0,
    "invented_confirmed_claim_rate_max": 0.05,
    "correct_template_routing_min": 0.90,
    "structured_bundle_parse_rate_min": 0.90,
    "golden_pass_rate": 1.0,
}


def _quality_payload(observed_payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = observed_payload.get("quality") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def evaluate_golden_scenario(
    *,
    name: str,
    observed: Dict[str, Any],
    expectations: Dict[str, Any],
) -> Dict[str, Any]:
    observed_payload = dict(observed or {}) if isinstance(observed, dict) else {}
    expected_payload = dict(expectations or {}) if isinstance(expectations, dict) else {}
    checks: Dict[str, Dict[str, Any]] = {}

    def _record(key: str, *, passed: bool, actual: Any, expected: Any) -> None:
        checks[key] = {
            "passed": bool(passed),
            "actual": actual,
            "expected": expected,
        }

    if "expected_template_id" in expected_payload:
        actual = str(observed_payload.get("effective_template_id") or "")
        expected = str(expected_payload.get("expected_template_id") or "")
        _record("routing_correctness", passed=actual == expected, actual=actual, expected=expected)

    if "expected_pause" in expected_payload:
        actual = bool(observed_payload.get("paused"))
        expected = bool(expected_payload.get("expected_pause"))
        _record("clarification_pause", passed=actual == expected, actual=actual, expected=expected)

    if "expected_artifact_spill" in expected_payload:
        actual = bool(observed_payload.get("artifact_spill"))
        expected = bool(expected_payload.get("expected_artifact_spill"))
        _record("artifact_spill", passed=actual == expected, actual=actual, expected=expected)

    if "expected_retry_recovery" in expected_payload:
        actual = bool(observed_payload.get("retry_recovered"))
        expected = bool(expected_payload.get("expected_retry_recovery"))
        _record("retry_recovery", passed=actual == expected, actual=actual, expected=expected)

    if "max_invented_claims" in expected_payload:
        quality = _quality_payload(observed_payload)
        actual = int((quality or {}).get("confirmed_claims_without_anchor") or 0)
        expected = int(expected_payload.get("max_invented_claims") or 0)
        _record("invented_claims", passed=actual <= expected, actual=actual, expected=expected)

    if "expected_runtime_verdict" in expected_payload:
        quality = _quality_payload(observed_payload)
        actual = str((quality or {}).get("runtime_verdict") or "")
        expected = str(expected_payload.get("expected_runtime_verdict") or "")
        _record("readiness_correctness", passed=actual == expected, actual=actual, expected=expected)

    if "max_false_ready_rate" in expected_payload:
        quality = _quality_payload(observed_payload)
        actual = float((quality or {}).get("false_ready_rate") or 0.0)
        expected = float(expected_payload.get("max_false_ready_rate") or 0.0)
        _record("false_ready_rate", passed=actual <= expected, actual=actual, expected=expected)

    if "min_routing_correctness" in expected_payload:
        quality = _quality_payload(observed_payload)
        raw_actual = (quality or {}).get("routing_correctness")
        actual = None if raw_actual is None else float(raw_actual)
        expected = float(expected_payload.get("min_routing_correctness") or 0.0)
        _record(
            "routing_metric",
            passed=actual is not None and actual >= expected,
            actual=actual,
            expected=expected,
        )

    if "min_structured_bundle_parse_rate" in expected_payload:
        quality = _quality_payload(observed_payload)
        actual = float((quality or {}).get("structured_bundle_parse_rate") or 0.0)
        expected = float(expected_payload.get("min_structured_bundle_parse_rate") or 0.0)
        _record(
            "structured_bundle_parse_rate",
            passed=actual >= expected,
            actual=actual,
            expected=expected,
        )

    passed = all(item.get("passed") is True for item in checks.values()) if checks else True
    return {
        "scenario": str(name or "").strip() or "(unknown)",
        "passed": passed,
        "checks": checks,
    }


def summarize_golden_scorecards(*scorecards: Dict[str, Any]) -> Dict[str, Any]:
    items = [dict(item or {}) for item in scorecards if isinstance(item, dict)]
    total = len(items)
    passed = sum(1 for item in items if bool(item.get("passed")))
    failed_names = [
        str(item.get("scenario") or "").strip()
        for item in items
        if not bool(item.get("passed")) and str(item.get("scenario") or "").strip()
    ]
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": float(passed) / float(total) if total > 0 else 1.0,
        "failed_scenarios": failed_names,
    }


def evaluate_release_gate(
    *,
    quality_metrics: Dict[str, Any],
    golden_summary: Dict[str, Any],
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    limits = dict(DEFAULT_RELEASE_THRESHOLDS)
    if isinstance(thresholds, dict):
        limits.update({str(k): float(v) for k, v in thresholds.items()})

    quality = dict(quality_metrics or {}) if isinstance(quality_metrics, dict) else {}
    golden = dict(golden_summary or {}) if isinstance(golden_summary, dict) else {}

    checks = {
        "false_ready_rate": {
            "actual": float(quality.get("false_ready_rate") or 0.0),
            "expected": limits["false_ready_rate"],
        },
        "invented_confirmed_claim_rate": {
            "actual": float(quality.get("invented_claim_rate") or 0.0),
            "expected": limits["invented_confirmed_claim_rate_max"],
        },
        "correct_template_routing": {
            "actual": float(quality.get("routing_correctness") or 0.0),
            "expected": limits["correct_template_routing_min"],
            "skip": quality.get("routing_correctness") is None,
        },
        "structured_bundle_parse_rate": {
            "actual": float(quality.get("structured_bundle_parse_rate") or 0.0),
            "expected": limits["structured_bundle_parse_rate_min"],
        },
        "golden_scenarios_pass_rate": {
            "actual": float(golden.get("pass_rate") or 0.0),
            "expected": limits["golden_pass_rate"],
        },
    }
    checks["false_ready_rate"]["passed"] = checks["false_ready_rate"]["actual"] <= checks["false_ready_rate"]["expected"]
    checks["invented_confirmed_claim_rate"]["passed"] = (
        checks["invented_confirmed_claim_rate"]["actual"] <= checks["invented_confirmed_claim_rate"]["expected"]
    )
    checks["correct_template_routing"]["passed"] = (
        bool(checks["correct_template_routing"].get("skip"))
        or checks["correct_template_routing"]["actual"] >= checks["correct_template_routing"]["expected"]
    )
    checks["structured_bundle_parse_rate"]["passed"] = (
        checks["structured_bundle_parse_rate"]["actual"] >= checks["structured_bundle_parse_rate"]["expected"]
    )
    checks["golden_scenarios_pass_rate"]["passed"] = (
        checks["golden_scenarios_pass_rate"]["actual"] >= checks["golden_scenarios_pass_rate"]["expected"]
    )
    release_ready = all(bool(item.get("passed")) for item in checks.values())
    return {
        "release_ready": release_ready,
        "thresholds": limits,
        "checks": checks,
    }
