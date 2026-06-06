from modes.sdk.runtime.evidence_pipeline import (
    claim_has_repo_anchor,
    claim_is_confirmable,
    claim_uses_only_codebase_map_evidence,
    collect_step_evidence,
    verify_claim_ledger,
)


def test_collect_step_evidence_deduplicates_outputs_and_artifacts() -> None:
    evidence = collect_step_evidence(
        {
            "outputs": [
                {"type": "text", "path": "/tmp/a.txt", "content_preview": "alpha"},
                {"type": "text", "path": "/tmp/a.txt", "content_preview": "alpha"},
            ],
            "artifacts": [
                {"type": "file", "path": "/tmp/b.md"},
                {"type": "file", "path": "/tmp/b.md"},
            ],
        }
    )
    assert len(evidence) == 2
    assert any(item["path"] == "/tmp/a.txt" for item in evidence)
    assert any(item["path"] == "/tmp/b.md" for item in evidence)


def test_claim_has_repo_anchor_checks_evidence_paths() -> None:
    assert claim_has_repo_anchor({"evidence": [{"path": "/repo/file.py"}]}) is True
    assert claim_has_repo_anchor({"evidence": [{"preview": "read_file"}]}) is False
    assert claim_has_repo_anchor({"evidence": [{"path": "/repo/.cli-proxy/.codebase_map/STACK.md"}]}) is False
    assert claim_has_repo_anchor({"evidence": [{"path": ".cli-proxy/.codebase_map/STACK.md"}]}) is False
    assert claim_has_repo_anchor({"evidence": [{"path": ".cli-proxy\\.codebase_map\\STACK.md"}]}) is False
    assert claim_has_repo_anchor({"evidence": [{"path": "/repo/_orchestrator/step1.md"}]}) is False
    assert (
        claim_has_repo_anchor(
            {
                "evidence": [
                    {"path": "/repo/.cli-proxy/runs/desktop:s1/analyst/run_x/artifacts/step1.md"}
                ]
            }
        )
        is False
    )


def test_claim_has_repo_anchor_accepts_local_markdown_link_in_claim_text() -> None:
    claim = {
        "text": (
            "Reader registry не содержит codex в "
            "[service.py](/srv/git_projects/cli-proxy/app/services/session_transfer/service.py:15)."
        ),
        "evidence": [{"preview": "summary only"}],
    }

    assert claim_has_repo_anchor(claim) is True


def test_claim_has_repo_anchor_rejects_orchestrator_artifact_link_in_claim_text() -> None:
    claim = {
        "text": (
            "Промежуточный вывод есть в "
            "[draft](/srv/git_projects/cli-proxy/.cli-proxy/runs/chat:1:s1/analyst/run_x/artifacts/step1.md:12)."
        ),
        "evidence": [{"preview": "summary only"}],
    }

    assert claim_has_repo_anchor(claim) is False


def test_claim_uses_only_codebase_map_evidence_detects_navigation_only_claim() -> None:
    assert (
        claim_uses_only_codebase_map_evidence(
            {
                "evidence": [
                    {
                        "path": "/repo/.cli-proxy/.codebase_map/INTEGRATIONS.md",
                        "preview": "Codebase Map integration summary",
                    }
                ]
            }
        )
        is True
    )
    assert (
        claim_uses_only_codebase_map_evidence(
            {
                "evidence": [
                    {
                        "path": "/repo/views/header.blade.php",
                        "preview": "read_file: header",
                    }
                ]
            }
        )
        is False
    )


def test_claim_is_confirmable_rejects_codebase_map_only_evidence() -> None:
    claim = {
        "evidence": [
            {
                "path": ".cli-proxy/.codebase_map/INTEGRATIONS.md",
                "preview": "Codebase Map integrations",
            }
        ]
    }

    assert claim_is_confirmable(claim, repo_grounded_required=False) is False


def test_claim_is_confirmable_accepts_mixed_real_repo_evidence() -> None:
    claim = {
        "evidence": [
            {
                "path": ".cli-proxy/.codebase_map/INTEGRATIONS.md",
                "preview": "Codebase Map integrations",
            },
            {
                "path": "/repo/app/services/session_transfer/service.py",
                "preview": "reader registry",
            },
        ]
    }

    assert claim_is_confirmable(claim, repo_grounded_required=True) is True


def test_verify_claim_ledger_reports_repo_grounded_anchor_gap() -> None:
    result = verify_claim_ledger(
        [
            {
                "status": "confirmed",
                "text": "Claim",
                "evidence": [{"preview": "read_file: foo"}],
            }
        ],
        repo_grounded_required=True,
    )
    assert result["evidence_gaps"]
    assert "repo-grounded claim without repo/file anchor" in result["evidence_gaps"][0]


def test_verify_claim_ledger_accepts_repo_grounded_claim_with_text_citation() -> None:
    result = verify_claim_ledger(
        [
            {
                "status": "confirmed",
                "text": (
                    "Reader registry не содержит codex в "
                    "[service.py](/srv/git_projects/cli-proxy/app/services/session_transfer/service.py:15)."
                ),
                "evidence": [{"preview": "summary only"}],
            }
        ],
        repo_grounded_required=True,
    )

    assert result["evidence_gaps"] == []


def test_verify_claim_ledger_reports_codebase_map_only_claim() -> None:
    result = verify_claim_ledger(
        [
            {
                "status": "confirmed",
                "text": "Telegram integration exists",
                "evidence": [
                    {
                        "path": "/repo/.cli-proxy/.codebase_map/INTEGRATIONS.md",
                        "preview": "Codebase Map integrations",
                    }
                ],
            }
        ],
        repo_grounded_required=True,
    )
    assert result["codebase_map_gaps"]
    assert "Codebase Map navigation evidence" in result["codebase_map_gaps"][0]


def test_verify_claim_ledger_reports_fact_usage_without_evidence() -> None:
    result = verify_claim_ledger(
        [
            {
                "status": "needs_check",
                "allowed_final_usage": "fact",
                "text": "Claim",
                "evidence": [],
            }
        ],
        repo_grounded_required=False,
    )

    assert result["evidence_gaps"]
    assert "Final claim without captured evidence" in result["evidence_gaps"][0]
