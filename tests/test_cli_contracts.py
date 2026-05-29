import json

from modes.sdk.runtime.cli_contracts import (
    CLIResponseFormat,
    collect_repo_review_runtime_gaps_from_outputs,
    parse_bundle_for_response_format,
    repo_review_bundle_to_outputs,
)
from modes.sdk.runtime.cli_review_prompts import build_repo_final_review_instruction


def test_parse_claim_bundle_accepts_json_inside_markdown_fence_with_surrounding_text() -> None:
    raw = (
        "Короткий комментарий перед structured output.\n"
        "```json\n"
        + json.dumps(
            {
                "final_text": "Финальное ТЗ",
                "claims": [
                    {
                        "claim_id": "claim_1",
                        "status": "confirmed",
                        "text": "Подтвержден только header.",
                        "evidence": [{"type": "repo_evidence", "path": "views/header.py", "preview": "read_file"}],
                    }
                ],
                "evidence": [{"type": "repo_evidence", "path": "views/header.py", "preview": "read_file"}],
                "open_gaps": ["Проверить mobile state"],
            },
            ensure_ascii=False,
        )
        + "\n```\n"
        "Комментарий после JSON, который раньше ломал parse."
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.CLAIM_BUNDLE_JSON)

    assert bundle is not None
    assert bundle["final_text"] == "Финальное ТЗ"
    assert bundle["open_gaps"] == ["Проверить mobile state"]
    assert [item["text"] for item in bundle["claims"]] == ["Подтвержден только header."]


def test_parse_repo_review_bundle_ignores_ansi_prefix_and_suffix_text() -> None:
    raw = (
        "\x1b[31mwarning:\x1b[0m structured mode degraded?\n"
        + json.dumps(
            {
                "verdict": "Критичных расхождений не осталось.",
                "mismatches": [],
                "unverified_claims": ["MiniApp integration не подтверждена"],
                "corrections": ["Оставить формулировку 'не подтверждено'."],
                "claims": [],
                "evidence": [],
                "open_gaps": [],
            },
            ensure_ascii=False,
        )
        + "\nfinished"
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON)

    assert bundle is not None
    assert bundle["verdict"] == "Критичных расхождений не осталось."
    assert bundle["unverified_claims"] == ["MiniApp integration не подтверждена"]
    assert "VERDICT" in bundle["final_text"]
    assert "UNVERIFIED_CLAIMS" in bundle["final_text"]


def test_parse_claim_bundle_prefers_last_payload_over_echoed_schema_example() -> None:
    raw = (
        "Сначала модель повторила пример схемы:\n"
        '{"final_text":"строка","claims":[],"evidence":[],"open_gaps":[]}\n'
        "А затем вернула реальный ответ:\n"
        + json.dumps(
            {
                "final_text": "REAL",
                "claims": [],
                "evidence": [],
                "open_gaps": [],
            },
            ensure_ascii=False,
        )
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.CLAIM_BUNDLE_JSON)

    assert bundle is not None
    assert bundle["final_text"] == "REAL"


def test_collect_repo_review_runtime_gaps_maps_open_gaps_to_issues() -> None:
    outputs = repo_review_bundle_to_outputs(
        {
            "verdict": "ok",
            "mismatches": [],
            "unverified_claims": [],
            "corrections": [],
            "claims": [],
            "evidence": [],
            "open_gaps": ["Нужно уточнить один оставшийся gap."],
        }
    )

    gaps = collect_repo_review_runtime_gaps_from_outputs([outputs])

    assert gaps["issues"] == ["Нужно уточнить один оставшийся gap."]


def test_repo_final_review_instruction_no_longer_requests_text_blocks() -> None:
    prompt = build_repo_final_review_instruction(
        base_instruction="Проверь ТЗ",
        draft_path="/tmp/draft.md",
        repo_root="/repo",
    )

    assert "VERDICT\nMISMATCHES\nUNVERIFIED_CLAIMS\nCORRECTIONS" not in prompt
    assert "structured-output" in prompt
    assert "verdict, mismatches, unverified_claims, corrections" in prompt
    assert "Не возвращай markdown-блоки VERDICT/MISMATCHES" in prompt


def test_parse_spec_fix_bundle_accepts_remaining_obligations() -> None:
    raw = json.dumps(
        {
            "final_text": "Исправленный документ",
            "closed_obligations": ["repo_step:use_cli_repo_grounding"],
            "remaining_obligations": [
                {
                    "obligation_id": "obligation_1",
                    "statement": "Проверить незакрытый integration gap.",
                    "status": "open",
                    "blocking": True,
                }
            ],
            "corrections_applied": ["Удалено неподтвержденное утверждение."],
            "claims": [],
            "evidence": [],
            "degraded_modes": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.SPEC_FIX_BUNDLE_JSON)

    assert bundle is not None
    assert bundle["final_text"] == "Исправленный документ"
    assert bundle["closed_obligations"] == ["repo_step:use_cli_repo_grounding"]
    assert bundle["remaining_obligations"][0]["statement"] == "Проверить незакрытый integration gap."


def test_parse_spec_fix_bundle_truncates_auxiliary_lists_but_keeps_final_text() -> None:
    raw = json.dumps(
        {
            "final_text": "# Final\n\nПолный исправленный документ",
            "closed_obligations": [f"closed-{idx}" for idx in range(60)],
            "remaining_obligations": [
                {
                    "obligation_id": f"obligation_{idx}",
                    "statement": f"Незакрытое обязательство {idx} " + ("x" * 400),
                    "status": "open",
                    "blocking": True,
                }
                for idx in range(40)
            ],
            "corrections_applied": [f"correction-{idx} " + ("y" * 400) for idx in range(40)],
            "claims": [
                {
                    "claim_id": f"claim_{idx}",
                    "status": "confirmed",
                    "text": f"Claim {idx} " + ("z" * 400),
                    "evidence": [
                        {
                            "type": "repo_evidence",
                            "path": f"/srv/git_projects/cli-proxy/src/file_{idx}_{evid}.py",
                            "preview": "preview " + ("p" * 400),
                        }
                        for evid in range(8)
                    ],
                }
                for idx in range(30)
            ],
            "evidence": [
                {
                    "type": "repo_evidence",
                    "path": f"/srv/git_projects/cli-proxy/src/extra_{idx}.py",
                    "preview": "preview " + ("q" * 400),
                }
                for idx in range(40)
            ],
            "degraded_modes": [f"mode-{idx} " + ("d" * 400) for idx in range(20)],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.SPEC_FIX_BUNDLE_JSON)

    assert bundle is not None
    assert bundle["final_text"] == "# Final\n\nПолный исправленный документ"
    assert len(bundle["closed_obligations"]) == 32
    assert len(bundle["remaining_obligations"]) == 24
    assert len(bundle["corrections_applied"]) == 24
    assert len(bundle["claims"]) == 20
    assert len(bundle["claims"][0]["evidence"]) == 4
    assert len(bundle["evidence"]) == 24
    assert len(bundle["degraded_modes"]) == 12
    assert bundle["remaining_obligations"][0]["statement"].endswith("…")
    assert bundle["claims"][0]["text"].endswith("…")


def test_parse_obligation_review_bundle_accepts_false_closures() -> None:
    raw = json.dumps(
        {
            "verdict": "Остался один blocking obligation.",
            "closed_blocking_obligations": ["repo_step:use_cli_repo_grounding"],
            "open_blocking_obligations": [
                {
                    "obligation_id": "obligation_2",
                    "statement": "Подтвердить repo anchor для capability claim.",
                    "status": "open",
                    "blocking": True,
                }
            ],
            "false_closures": [
                {
                    "obligation_id": "obligation_3",
                    "statement": "Гэп объявлен закрытым без evidence.",
                    "status": "open",
                    "blocking": True,
                }
            ],
            "unsupported_assertions": ["Desktop parity не подтверждён."],
            "required_corrections": ["Вернуть маркировку 'не подтверждено'."],
            "claims": [],
            "evidence": [],
            "degraded_modes": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON)

    assert bundle is not None
    assert bundle["verdict"] == "Остался один blocking obligation."
    assert bundle["open_blocking_obligations"][0]["statement"] == "Подтвердить repo anchor для capability claim."
    assert bundle["false_closures"][0]["statement"] == "Гэп объявлен закрытым без evidence."


def test_parse_spec_fix_bundle_rejects_malformed_remaining_obligation_item() -> None:
    raw = json.dumps(
        {
            "final_text": "Исправленный документ",
            "closed_obligations": [],
            "remaining_obligations": [{"obligation_id": "obligation_1"}],
            "corrections_applied": [],
            "claims": [],
            "evidence": [],
            "degraded_modes": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.SPEC_FIX_BUNDLE_JSON)

    assert bundle is None


def test_parse_obligation_review_bundle_rejects_malformed_open_obligation_item() -> None:
    raw = json.dumps(
        {
            "verdict": "Остался один blocking obligation.",
            "closed_blocking_obligations": [],
            "open_blocking_obligations": [{"obligation_id": "obligation_2"}],
            "false_closures": [],
            "unsupported_assertions": [],
            "required_corrections": [],
            "claims": [],
            "evidence": [],
            "degraded_modes": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON)

    assert bundle is None


def test_parse_spec_fix_bundle_rejects_malformed_claim_or_evidence_payload() -> None:
    raw = json.dumps(
        {
            "final_text": "Исправленный документ",
            "closed_obligations": [],
            "remaining_obligations": [],
            "corrections_applied": [],
            "claims": [{"claim_id": "claim_1", "status": "confirmed"}],
            "evidence": "not-a-list",
            "degraded_modes": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.SPEC_FIX_BUNDLE_JSON)

    assert bundle is None


def test_parse_claim_bundle_rejects_malformed_claim_or_evidence_payload() -> None:
    raw = json.dumps(
        {
            "final_text": "Финальное ТЗ",
            "claims": [{"claim_id": "claim_1", "status": "confirmed"}],
            "evidence": "not-a-list",
            "open_gaps": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.CLAIM_BUNDLE_JSON)

    assert bundle is None


def test_parse_repo_review_bundle_rejects_malformed_claim_payload() -> None:
    raw = json.dumps(
        {
            "verdict": "Критичных расхождений не осталось.",
            "mismatches": [],
            "unverified_claims": [],
            "corrections": [],
            "claims": [{"claim_id": "claim_1", "status": "confirmed"}],
            "evidence": [],
            "open_gaps": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.REPO_REVIEW_BUNDLE_JSON)

    assert bundle is None


def test_parse_obligation_review_bundle_rejects_malformed_claim_or_evidence_payload() -> None:
    raw = json.dumps(
        {
            "verdict": "Остался один blocking obligation.",
            "closed_blocking_obligations": [],
            "open_blocking_obligations": [],
            "false_closures": [],
            "unsupported_assertions": [],
            "required_corrections": [],
            "claims": [{"claim_id": "claim_1", "status": "confirmed"}],
            "evidence": "not-a-list",
            "degraded_modes": [],
        },
        ensure_ascii=False,
    )

    bundle = parse_bundle_for_response_format(raw, CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON)

    assert bundle is None
