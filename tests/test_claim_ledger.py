from modes.sdk.runtime.claim_ledger import normalize_claim_ledger, validate_claim_ledger


def test_normalize_claim_ledger_fills_required_fields() -> None:
    ledger = normalize_claim_ledger(
        [
            {
                "claim_id": "c1",
                "task_id": "step1",
                "status": "confirmed",
                "text": "Header exists",
                "evidence": [{"path": "/repo/views/header.blade.php", "preview": "header"}],
            }
        ]
    )
    assert len(ledger) == 1
    claim = ledger[0]
    assert claim["claim_id"] == "c1"
    assert claim["source_step_id"] == "step1"
    assert claim["component_scope"] == "general"
    assert claim["allowed_final_usage"] == "fact"


def test_normalize_claim_ledger_deduplicates_claim_ids() -> None:
    ledger = normalize_claim_ledger(
        [
            {"claim_id": "dup", "task_id": "step1", "status": "confirmed", "text": "One"},
            {"claim_id": "dup", "task_id": "step2", "status": "needs_check", "text": "Two"},
        ]
    )
    assert ledger[0]["claim_id"] == "dup"
    assert ledger[1]["claim_id"] == "dup_2"


def test_normalize_claim_ledger_syncs_final_usage_to_status() -> None:
    ledger = normalize_claim_ledger(
        [
            {
                "claim_id": "needs-check",
                "status": "needs_check",
                "text": "Need verification",
                "allowed_final_usage": "fact",
            },
            {
                "claim_id": "unconfirmed",
                "status": "unconfirmed",
                "text": "Blocked claim",
                "allowed_final_usage": "fact",
            },
            {
                "claim_id": "confirmed",
                "status": "confirmed",
                "text": "Confirmed claim",
                "allowed_final_usage": "open_question",
            },
        ]
    )

    assert ledger[0]["allowed_final_usage"] == "open_question"
    assert ledger[1]["allowed_final_usage"] == "blocked_item"
    assert ledger[2]["allowed_final_usage"] == "fact"


def test_validate_claim_ledger_reports_missing_source_and_invalid_usage() -> None:
    result = validate_claim_ledger(
        [
            {
                "claim_id": "c1",
                "status": "confirmed",
                "text": "Claim",
                "evidence": [],
                "allowed_final_usage": "bad",
            }
        ]
    )
    assert result["errors"]
    assert any("invalid allowed_final_usage" in item for item in result["errors"])
    assert result["warnings"]
    assert any("missing source_step_id" in item for item in result["warnings"])
