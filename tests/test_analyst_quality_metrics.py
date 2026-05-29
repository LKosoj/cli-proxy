from modes.sdk.runtime.analyst_quality_metrics import build_analyst_quality_metrics


def test_build_analyst_quality_metrics_computes_rates_and_false_ready() -> None:
    metrics = build_analyst_quality_metrics(
        claim_ledger=[
            {
                "claim_id": "c1",
                "status": "confirmed",
                "text": "Confirmed without anchor",
                "evidence": [{"type": "repo_evidence", "path": "", "preview": "preview only"}],
            },
            {
                "claim_id": "c2",
                "status": "confirmed",
                "text": "Confirmed with anchor",
                "evidence": [{"type": "repo_evidence", "path": "/tmp/file.php", "preview": "line"}],
            },
        ],
        assessment={"evidence_gaps": ["gap-1"]},
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        model_text_before_runtime="## Статус готовности\n\n**Готово к реализации.**\n\nТекст",
        structured_bundle_calls=4,
        structured_bundle_successes=3,
        cli_fallbacks=1,
        retry_successes=1,
        retry_exhausted=1,
        structured_bundle_stage_stats={
            "gap_closure": {"calls": 2, "successes": 1, "fallbacks": 1, "retry_successes": 1, "retry_exhausted": 0},
            "followup_review": {"calls": 2, "successes": 2, "fallbacks": 0, "retry_successes": 0, "retry_exhausted": 1},
        },
        template_resolution={
            "selected_template_id": "default",
            "intent_template_id": "default",
            "effective_template_id": "change_spec",
            "document_kind": "spec",
            "change_scope": "broad_change",
        },
    )

    assert metrics["structured_bundle_parse_rate"] == 0.75
    assert metrics["cli_fallback_rate"] == 0.25
    assert metrics["retry_rate"] == 0.5
    assert metrics["invented_claim_rate"] == 0.5
    assert metrics["invented_claims_by_source"] == {"(unknown)": 1}
    assert metrics["optimistic_status_removed"] is True
    assert metrics["runtime_verdict"] == "Не готово к реализации"
    assert metrics["false_ready_rate"] == 1.0
    assert metrics["structured_bundle_stages"]["gap_closure"]["parse_rate"] == 0.5
    assert metrics["structured_bundle_stages"]["followup_review"]["retry_exhausted"] == 1
    assert metrics["routing"]["applicable"] is True
    assert metrics["routing"]["expected_template_id"] == "change_spec"
    assert metrics["routing_correctness"] == 1.0


def test_build_analyst_quality_metrics_handles_zero_denominators() -> None:
    metrics = build_analyst_quality_metrics(
        claim_ledger=[],
        assessment={},
        repo_grounded_required=False,
        required_step_statuses={},
        model_text_before_runtime="Текст",
        structured_bundle_calls=0,
        structured_bundle_successes=0,
        cli_fallbacks=0,
        retry_successes=0,
        retry_exhausted=0,
        structured_bundle_stage_stats={},
        template_resolution={},
    )

    assert metrics["structured_bundle_parse_rate"] == 1.0
    assert metrics["cli_fallback_rate"] == 0.0
    assert metrics["retry_rate"] == 0.0
    assert metrics["invented_claim_rate"] == 0.0
    assert metrics["false_ready_rate"] == 0.0
    assert metrics["routing_correctness"] is None
