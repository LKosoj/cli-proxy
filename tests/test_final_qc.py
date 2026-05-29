from modes.sdk.runtime.final_qc import (
    REPO_GAP_LABELS,
    apply_runtime_readiness,
    build_assessment_schema,
    build_open_gaps_text,
    collect_implementation_handoff_gaps,
    collect_placeholder_gaps,
    compute_runtime_readiness,
    runtime_readiness_allows_finalization,
    strip_model_readiness_sections,
)
from modes.sdk.runtime.obligations import build_obligation_matrix, build_task_contract


def test_build_assessment_schema_contains_repo_gap_fields() -> None:
    schema = build_assessment_schema(is_large_spec=True)
    props = schema["properties"]
    assert "needs_rework" in props
    assert "missing_sections" in props
    assert "section_contract_gaps" in props
    assert "required_input_gaps" in props
    assert "placeholder_gaps" in props
    assert "implementation_handoff_gaps" in props
    assert "spec_to_plan_gaps" in props
    assert "codebase_mismatches" in props
    assert "degraded_modes" not in props


def test_build_open_gaps_text_splits_structural_and_evidence_layers() -> None:
    text = build_open_gaps_text(
        {
            "qc_layers": {
                "structural": {
                    "issues": ["issue-1"],
                    "missing_sections": ["section-1"],
                    "section_contract_gaps": ["order-1"],
                    "required_input_gaps": ["input-1"],
                    "placeholder_gaps": ["todo-1"],
                    "implementation_handoff_gaps": ["handoff-1"],
                    "spec_to_plan_gaps": ["fr-1"],
                    "weak_sections": [],
                    "missing_counts": [],
                    "traceability_gaps": [],
                },
                "evidence": {
                    "codebase_mismatches": ["mismatch-1"],
                    "evidence_gaps": ["gap-1"],
                },
            }
        },
        REPO_GAP_LABELS,
    )
    assert "# Open Gaps" in text
    assert "## Structural QC" in text
    assert "## Evidence QC" in text
    assert "issue-1" in text
    assert "order-1" in text
    assert "input-1" in text
    assert "todo-1" in text
    assert "handoff-1" in text
    assert "fr-1" in text
    assert "mismatch-1" in text


def test_collect_placeholder_gaps_flags_known_vague_markers() -> None:
    text = """## План

TODO
Нужно добавить тесты.
Обработать edge cases.
Ownership точного helper-вызова остается реализационной деталью.
Exact naming не является source of truth этого ТЗ.
"""

    gaps = collect_placeholder_gaps(text)

    assert any('"TODO"' in item for item in gaps)
    assert any("нужно добавить тесты" in item.lower() for item in gaps)
    assert any("обработать edge cases" in item.lower() for item in gaps)
    assert any("реализационная деталь" in item.lower() for item in gaps)
    assert any("source of truth" in item.lower() for item in gaps)


def test_collect_implementation_handoff_gaps_requires_components_verification_and_commands() -> None:
    gaps = collect_implementation_handoff_gaps(
        """## Контекст

Есть изменение.

## Implementation handoff по компонентам и файлам

- TODO
""",
        required_sections=["Контекст", "Implementation handoff по компонентам и файлам"],
    )

    assert any("слишком короткий" in item.lower() for item in gaps)
    assert any("компоненты или файлы" in item.lower() for item in gaps)
    assert any("что именно меняется" in item.lower() for item in gaps)
    assert any("как проверять" in item.lower() for item in gaps)
    assert any("тесты или команды" in item.lower() for item in gaps)


def test_build_open_gaps_text_includes_blocking_obligations() -> None:
    text = build_open_gaps_text(
        {
            "qc_layers": {
                "structural": {"issues": [], "missing_sections": [], "weak_sections": [], "missing_counts": [], "traceability_gaps": []},
                "evidence": {},
            },
            "blocking_obligations": [
                {"obligation_id": "o1", "statement": "Закрыть repo mismatch.", "status": "open", "blocking": True}
            ],
        },
        REPO_GAP_LABELS,
    )
    assert "## Blocking Obligations" in text
    assert "Закрыть repo mismatch." in text


def test_compute_runtime_readiness_blocks_on_missing_steps_and_evidence() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": ["Acceptance criteria"],
            "evidence_gaps": ["Claim without repo anchor"],
            "issues": ["Minor wording issue"],
            "degraded_modes": ["gap_closure invalid_bundle_fallback_to_text"],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok", "use_cli_repo_final_review": "failed"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("use_cli_repo_final_review" in item for item in readiness["blocking_reasons"])
    assert any("Пробелы evidence/traceability" in item for item in readiness["blocking_reasons"])
    assert any("Деградировавшие runtime/CLI режимы" in item for item in readiness["warning_reasons"])


def test_compute_runtime_readiness_blocks_on_partial_required_step_and_unverified_claims() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "unverified_claims": ["Telegram WebApp not confirmed"],
            "codebase_mismatches": ["Document claims avatar is missing but repo shows account component"],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "partial", "use_cli_repo_final_review": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("use_cli_repo_grounding" in item for item in readiness["blocking_reasons"])
    assert any("Неподтвержденные product/capability claims" in item for item in readiness["blocking_reasons"])
    assert any("Несоответствия кодовой базе" in item for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_uses_blocking_obligations_when_present() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "blocking_obligations": [
                {"obligation_id": "o1", "statement": "Закрыть repo mismatch.", "status": "open", "blocking": True}
            ],
            "degraded_modes": ["followup_obligation_review retry_exhausted invalid_bundle"],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("Незакрытые blocking obligations" in item for item in readiness["blocking_reasons"])
    assert any("retry exhausted" in item.lower() for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_does_not_hide_live_repo_gap_with_closed_selector() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
        required_step_ids=["use_cli_repo_grounding"],
    )
    assessment = {
        "missing_sections": [],
        "required_input_gaps": [],
        "issues": [],
        "weak_sections": [],
        "missing_counts": [],
        "traceability_gaps": [],
        "codebase_mismatches": [],
        "unsupported_assumptions": [],
        "unverified_claims": ["Неподтвержденный claim про API X"],
        "evidence_gaps": [],
        "missing_required_artifacts": [],
        "fix_closed_obligations": ["Неподтвержденный claim про API X"],
        "fix_remaining_obligations": [],
        "degraded_modes": [],
        "followup_closed_blocking_obligations": [],
        "followup_open_blocking_obligations": [],
        "followup_false_closures": [],
        "obligation_model_active": True,
    }
    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment=assessment,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
    )

    readiness = compute_runtime_readiness(
        {
            **assessment,
            "blocking_obligations": obligations,
            "non_blocking_obligations": [],
            "obligation_matrix": obligations,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )

    assert readiness["verdict"] == "Не готово к реализации"
    assert any("Незакрытые blocking obligations" in item for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_required_input_gaps_without_obligation_model() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "required_input_gaps": ["Требования к совместимости UX"],
            "issues": [],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("обязательные входы задачи" in item.lower() for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_section_contract_gaps() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "section_contract_gaps": [
                "Нарушен порядок обязательных разделов: `Контекст` должен идти раньше `Требования`."
            ],
            "issues": [],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("контракт обязательных разделов" in item.lower() for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_placeholder_handoff_and_spec_to_plan_gaps() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "placeholder_gaps": ['Плейсхолдер "TODO" в строке 4: TODO'],
            "implementation_handoff_gaps": [
                'Раздел "Implementation handoff по компонентам и файлам" не содержит тесты или команды для запуска.'
            ],
            "spec_to_plan_gaps": ["FR-001 не связан с конкретным способом реализации или проверки."],
            "issues": [],
            "obligation_model_active": True,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )

    assert readiness["verdict"] == "Не готово к реализации"
    assert any("placeholder" in item.lower() for item in readiness["blocking_reasons"])
    assert any("implementation handoff" in item.lower() for item in readiness["blocking_reasons"])
    assert any("способа реализации" in item.lower() for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_protected_shell_false_closure() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=["Контекст"],
        repo_grounded_required=True,
        protected_spec_shell={
            "title": "Техническое задание",
            "source_task_section": "Исходная задача",
            "core_sections": ["Контекст"],
            "open_questions_section": "Открытые вопросы и валидационные шаги",
        },
    )
    assessment = {
        "missing_sections": ["Исходная задача"],
        "required_input_gaps": [],
        "issues": [],
        "weak_sections": [],
        "missing_counts": [],
        "traceability_gaps": [],
        "codebase_mismatches": [],
        "unsupported_assumptions": [],
        "unverified_claims": [],
        "evidence_gaps": [],
        "missing_required_artifacts": [],
        "fix_closed_obligations": ["Verifier passed stale shell regression"],
        "fix_remaining_obligations": [],
        "degraded_modes": [],
        "followup_closed_blocking_obligations": [],
        "followup_open_blocking_obligations": [],
        "followup_false_closures": [
            {
                "obligation_id": "protected_shell:false_closure",
                "statement": "Verifier passed document without `Исходная задача` as ready.",
                "status": "open",
                "blocking": True,
            }
        ],
        "obligation_model_active": True,
    }
    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment=assessment,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
    )

    readiness = compute_runtime_readiness(
        {
            **assessment,
            "blocking_obligations": obligations,
            "non_blocking_obligations": [],
            "obligation_matrix": obligations,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )

    assert readiness["verdict"] == "Не готово к реализации"
    assert any("Незакрытые blocking obligations" in item for item in readiness["blocking_reasons"])
    assert any("Verifier false closures" in item for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_stale_followup_artifact_binding() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "blocking_obligations": [
                {
                    "obligation_id": "followup_review:artifact_binding",
                    "statement": "Verifier validated stale persisted draft artifact: path=/tmp/draft.md, expected_sha1=a, actual_sha1=b.",
                    "status": "open",
                    "blocking": True,
                }
            ],
            "followup_false_closures": [
                {
                    "obligation_id": "followup_review:artifact_binding",
                    "statement": "Verifier validated stale persisted draft artifact: path=/tmp/draft.md, expected_sha1=a, actual_sha1=b.",
                    "status": "open",
                    "blocking": True,
                }
            ],
            "degraded_modes": ["followup_obligation_review stale_artifact_validation"],
            "obligation_model_active": True,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_audit": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("Незакрытые blocking obligations" in item for item in readiness["blocking_reasons"])
    assert any("Verifier false closures" in item for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_critical_missing_runtime_artifacts() -> None:
    readiness = compute_runtime_readiness(
        {
            "missing_sections": [],
            "blocking_obligations": [],
            "degraded_modes": [
                "final_artifact_bundle_incomplete",
                "final_task_contract_missing",
            ],
            "obligation_model_active": True,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("runtime artifacts" in item.lower() for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_execution_failed_degraded_even_with_assessment_error() -> None:
    readiness = compute_runtime_readiness(
        {
            "issues": ["qc_assessment_parse_error"],
            "assessment_error": True,
            "blocking_obligations": [],
            "non_blocking_obligations": [],
            "degraded_modes": ["final_cli_gap_closure execution_failed_cli_unavailable"],
            "obligation_model_active": True,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("degraded runtime/cli" in item.lower() for item in readiness["blocking_reasons"])


def test_compute_runtime_readiness_blocks_on_assessment_error_without_other_runtime_gaps() -> None:
    readiness = compute_runtime_readiness(
        {
            "issues": ["qc_assessment_parse_error"],
            "assessment_error": True,
            "blocking_obligations": [],
            "non_blocking_obligations": [],
            "degraded_modes": [],
            "obligation_model_active": True,
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert readiness["verdict"] == "Не готово к реализации"
    assert any("assessment model" in item for item in readiness["blocking_reasons"])


def test_apply_runtime_readiness_keeps_document_without_status_prefix() -> None:
    out = apply_runtime_readiness(
        "Документ",
        {
            "issues": ["issue-1"],
            "missing_sections": [],
            "weak_sections": [],
            "missing_counts": [],
            "traceability_gaps": [],
            "degraded_modes": ["memory trimmed"],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert out == "Документ"
    assert "Статус готовности" not in out


def test_strip_model_readiness_sections_preserves_plain_body_after_status_line() -> None:
    cleaned = strip_model_readiness_sections(
        """Статус: Готово к реализации

Детали ниже.
"""
    )
    assert cleaned == "Детали ниже."


def test_strip_model_readiness_sections_removes_existing_status_block() -> None:
    raw = """## Статус готовности

**Готово к реализации.**

Основание:
- old

## Основное

Содержимое
"""
    cleaned = strip_model_readiness_sections(raw)
    assert "Статус готовности" not in cleaned
    assert "**Готово к реализации.**" not in cleaned
    assert "## Основное" in cleaned
    assert "Содержимое" in cleaned


def test_apply_runtime_readiness_replaces_model_status_block() -> None:
    out = apply_runtime_readiness(
        """Статус: Готово к реализации

Детали ниже.
""",
        {
            "issues": [],
            "missing_sections": ["Acceptance"],
            "weak_sections": [],
            "missing_counts": [],
            "traceability_gaps": [],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert out == "Детали ниже."
    assert "Статус готовности" not in out
    assert "**Не готово к реализации.**" not in out


def test_apply_runtime_readiness_overrides_optimistic_model_text() -> None:
    out = apply_runtime_readiness(
        """## Статус готовности

**Готово к реализации.**

## Итог

Все готово.
""",
        {
            "missing_sections": [],
            "unsupported_assumptions": ["Notifications table existence not confirmed"],
            "weak_sections": [],
            "missing_counts": [],
            "traceability_gaps": [],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok", "use_cli_repo_final_review": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert "## Статус готовности" not in out
    assert "**Не готово к реализации.**" not in out
    assert "Готово к реализации." not in out
    assert "## Итог" in out
    assert "Все готово." in out


def test_apply_runtime_readiness_keeps_internal_runtime_markers_out_of_user_markdown() -> None:
    out = apply_runtime_readiness(
        """## Итог

Все готово.
""",
        {
            "runtime_verdict": "Не готово к реализации",
            "blocking_reasons": [
                "Не закрыты обязательные входы задачи: Какие сценарии нельзя сломать",
            ],
            "warning_reasons": ["Деградировавшие runtime/CLI режимы: 1"],
            "required_input_gaps": ["Какие сценарии нельзя сломать"],
        },
        repo_grounded_required=True,
        required_step_statuses={"use_cli_repo_grounding": "ok"},
        repo_gap_labels=REPO_GAP_LABELS,
    )
    assert out == "## Итог\n\nВсе готово."
    assert "runtime_verdict" not in out
    assert "blocking_reasons" not in out
    assert "warning_reasons" not in out
    assert "Не готово к реализации" not in out


def test_runtime_readiness_allows_finalization_for_ready_or_unscored_payload() -> None:
    assert runtime_readiness_allows_finalization({}) is True
    assert runtime_readiness_allows_finalization({"runtime_verdict": "Готово к реализации"}) is True
    assert runtime_readiness_allows_finalization({"runtime_verdict": "Требует проверки перед реализацией"}) is True


def test_runtime_readiness_allows_finalization_blocks_not_ready_or_blocking_payload() -> None:
    assert runtime_readiness_allows_finalization({"runtime_verdict": "Не готово к реализации"}) is False
    assert (
        runtime_readiness_allows_finalization(
            {
                "runtime_verdict": "Готово к реализации",
                "blocking_reasons": ["Незакрытые blocking obligations: 2"],
            }
        )
        is False
    )
