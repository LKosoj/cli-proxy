from modes.sdk.runtime.obligations import build_obligation_matrix, build_task_contract


def test_build_obligation_matrix_marks_missing_required_artifacts_open() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
        required_artifacts=["task_contract", "claim_ledger"],
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"missing_required_artifacts": ["claim_ledger"]},
        required_step_statuses={},
    )

    by_id = {item["obligation_id"]: item for item in obligations}
    assert by_id["artifact:task_contract"]["status"] == "closed"
    assert by_id["artifact:claim_ledger"]["status"] == "open"
    assert by_id["artifact:claim_ledger"]["blocking"] is True


def test_build_obligation_matrix_keeps_live_project_gap_open_even_if_selector_is_marked_closed() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
        required_step_ids=["use_cli_repo_grounding"],
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "codebase_mismatches": ["Уточнить неподтвержденное capability claim."],
            "fix_closed_obligations": ["repo_step:use_cli_repo_grounding"],
        },
        required_step_statuses={"use_cli_repo_grounding": "failed"},
    )

    mismatch_id = next(
        item["obligation_id"]
        for item in obligations
        if item["statement"] == "Уточнить неподтвержденное capability claim."
    )
    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "codebase_mismatches": ["Уточнить неподтвержденное capability claim."],
            "fix_closed_obligations": [
                mismatch_id,
                "repo_step:use_cli_repo_grounding",
            ],
        },
        required_step_statuses={"use_cli_repo_grounding": "failed"},
    )

    by_id = {item["obligation_id"]: item for item in obligations}
    assert by_id[mismatch_id]["status"] == "open"
    assert by_id["repo_step:use_cli_repo_grounding"]["status"] == "open"


def test_build_obligation_matrix_prefers_verifier_reopened_obligation() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "fix_closed_obligations": ["capability:desktop"],
            "fix_remaining_obligations": [
                {
                    "obligation_id": "capability:desktop",
                    "statement": "Подтвердить desktop capability claim.",
                    "status": "open",
                    "blocking": True,
                    "source": "spec_fixer",
                }
            ],
            "followup_false_closures": [
                {
                    "obligation_id": "capability:desktop",
                    "statement": "Подтвердить desktop capability claim.",
                    "status": "open",
                    "blocking": True,
                    "source": "verifier",
                }
            ],
        },
        required_step_statuses={},
    )

    obligation = next(item for item in obligations if item["obligation_id"] == "capability:desktop")
    assert obligation["status"] == "open"
    assert obligation["source"] == "verifier"


def test_build_obligation_matrix_merges_same_obligation_id_with_updated_statement() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "fix_remaining_obligations": [
                {
                    "obligation_id": "capability:desktop",
                    "statement": "Подтвердить desktop claim.",
                    "status": "open",
                    "blocking": True,
                    "source": "spec_fixer",
                }
            ],
            "followup_open_blocking_obligations": [
                {
                    "obligation_id": "capability:desktop",
                    "statement": "Подтвердить claim для desktop path.",
                    "status": "open",
                    "blocking": True,
                    "source": "verifier",
                }
            ],
        },
        required_step_statuses={},
    )

    desktop_obligations = [item for item in obligations if item["obligation_id"] == "capability:desktop"]
    assert len(desktop_obligations) == 1
    assert desktop_obligations[0]["statement"] == "Подтвердить claim для desktop path."
    assert desktop_obligations[0]["source"] == "verifier"


def test_build_obligation_matrix_keeps_repo_gap_identity_stable_across_reordering() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
    )

    initial = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"codebase_mismatches": ["A mismatch", "B mismatch"]},
        required_step_statuses={},
    )
    b_obligation = next(item for item in initial if item["statement"] == "B mismatch")

    reordered = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "codebase_mismatches": ["B mismatch", "A mismatch"],
            "fix_closed_obligations": [b_obligation["obligation_id"]],
        },
        required_step_statuses={},
    )

    by_statement = {item["statement"]: item for item in reordered if item["statement"] in {"A mismatch", "B mismatch"}}
    assert by_statement["B mismatch"]["status"] == "open"
    assert by_statement["A mismatch"]["status"] == "open"


def test_build_obligation_matrix_keeps_repo_gap_identity_stable_across_equivalent_rephrasing() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
    )

    initial = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"evidence_gaps": ["Need repo anchor for desktop claim."]},
        required_step_statuses={},
    )
    initial_obligation = next(item for item in initial if item["statement"] == "Need repo anchor for desktop claim.")

    rephrased = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "evidence_gaps": ["Desktop claim needs repo anchor."],
            "fix_closed_obligations": [initial_obligation["obligation_id"]],
        },
        required_step_statuses={},
    )

    desktop_obligations = [item for item in rephrased if "desktop" in item["statement"].lower()]
    assert len(desktop_obligations) == 1
    assert desktop_obligations[0]["status"] == "open"
    assert desktop_obligations[0]["statement"] == "Desktop claim needs repo anchor."


def test_build_obligation_matrix_preserves_negation_in_repo_gap_identity() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=[],
        repo_grounded_required=True,
    )

    positive = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"evidence_gaps": ["Desktop parity подтверждена."]},
        required_step_statuses={},
    )
    negative = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"evidence_gaps": ["Desktop parity не подтверждена."]},
        required_step_statuses={},
    )

    positive_gap = next(item for item in positive if item["statement"].startswith("Desktop parity"))
    negative_gap = next(item for item in negative if item["statement"].startswith("Desktop parity"))
    assert positive_gap["obligation_id"] != negative_gap["obligation_id"]


def test_build_obligation_matrix_respects_explicit_empty_project_gap_field_lists() -> None:
    task_contract = build_task_contract(
        user_query="Обычный анализ без repo-grounding",
        required_sections=[],
        repo_grounded_required=False,
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"codebase_mismatches": ["repo mismatch should stay outside obligation model"]},
        required_step_statuses={},
    )

    assert not any(item["statement"] == "repo mismatch should stay outside obligation model" for item in obligations)


def test_build_task_contract_does_not_derive_docs_tests_or_migrations_from_generic_prompt_text() -> None:
    contract = build_task_contract(
        user_query="Поправить кнопку в header",
        required_sections=["Контекст"],
        repo_grounded_required=True,
        template_name="change_spec",
        template_description="Локальное изменение UI",
        required_inputs=["Что меняем"],
        qa_prompt="Проверь, что документ конкретный и краткий.",
    )

    assert contract["blocking_project_gap_fields"] == [
        "codebase_mismatches",
        "unsupported_assumptions",
        "unverified_claims",
        "evidence_gaps",
    ]
    assert contract["non_blocking_project_gap_fields"] == ["issues", "weak_sections"]


def test_build_task_contract_derives_task_specific_gap_fields_and_traceability_obligation() -> None:
    change_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec для локальной backend-доработки",
        required_sections=["Контекст", "План реализации"],
        repo_grounded_required=True,
        template_name="ТЗ на локальную backend-доработку",
        template_description="Узкий repo-grounded шаблон для локальных server-side изменений.",
        required_inputs=["Что меняем и что нельзя сломать"],
        traceability_rules=[],
    )

    assert "migration_gaps" not in change_contract["blocking_project_gap_fields"]
    assert "doc_sync_gaps" not in change_contract["non_blocking_project_gap_fields"]
    assert "test_gaps" not in change_contract["non_blocking_project_gap_fields"]

    migration_contract = build_task_contract(
        user_query="Подготовить план миграции схемы с безопасным прерыванием и backward compatibility",
        required_sections=["Стратегия перехода", "План безопасного прерывания"],
        repo_grounded_required=True,
        template_name="План миграции",
        template_description="Шаблон миграции технологий/схем/контрактов.",
        required_inputs=["Допустимый downtime"],
        traceability_rules=["Каждый этап MUST иметь критерии остановки/прерывания."],
    )

    assert "migration_gaps" in migration_contract["blocking_project_gap_fields"]
    assert "traceability_gaps" in migration_contract["non_blocking_project_gap_fields"]
    obligations = build_obligation_matrix(
        task_contract=migration_contract,
        assessment={"traceability_gaps": ["Для этапа нет критериев остановки/прерывания."]},
        required_step_statuses={},
    )

    traceability_obligation = next(item for item in obligations if item["obligation_id"] == "task:traceability_rules")
    assert traceability_obligation["status"] == "open"
    assert traceability_obligation["source"] == "task"


def test_build_task_contract_includes_stage_artifacts_for_repo_grounded_runs() -> None:
    contract = build_task_contract(
        user_query="Подготовить implementation-ready spec для изменения существующего проекта",
        required_sections=["Контекст"],
        repo_grounded_required=True,
    )

    assert contract["required_artifacts"] == [
        "task_contract",
        "claim_ledger",
        "fact_pack",
        "draft",
        "artifacts_index",
        "obligation_matrix",
    ]

    obligations = build_obligation_matrix(
        task_contract=contract,
        assessment={"missing_required_artifacts": ["obligation_matrix"]},
        required_step_statuses={},
    )

    obligation_matrix_artifact = next(item for item in obligations if item["obligation_id"] == "artifact:obligation_matrix")
    assert not any(item["obligation_id"] == "artifact:open_gaps" for item in obligations)
    assert obligation_matrix_artifact["status"] == "open"


def test_build_task_contract_does_not_treat_schema_review_as_migration_scope() -> None:
    contract = build_task_contract(
        user_query="Подготовить обзор текущей database schema без изменений данных",
        required_sections=["Контекст"],
        repo_grounded_required=True,
        template_name="Исследование текущей схемы",
        template_description="Нужно описать текущую schema проекта и связанные ограничения.",
        required_inputs=["Какие таблицы покрываем"],
    )

    assert "migration_gaps" not in contract["blocking_project_gap_fields"]


def test_build_task_contract_promotes_test_and_doc_gaps_to_blocking_when_task_explicitly_requires_them() -> None:
    contract = build_task_contract(
        user_query="Подготовить implementation-ready spec, обновить README и конкретный план тестов для bugfix",
        required_sections=["Контекст"],
        repo_grounded_required=True,
        template_name="bugfix",
        template_description="Нужно описать fix, документацию и тесты.",
    )

    assert "doc_sync_gaps" in contract["blocking_project_gap_fields"]
    assert "test_gaps" in contract["blocking_project_gap_fields"]
    assert "doc_sync_gaps" not in contract["non_blocking_project_gap_fields"]
    assert "test_gaps" not in contract["non_blocking_project_gap_fields"]

    obligations = build_obligation_matrix(
        task_contract=contract,
        assessment={"test_gaps": ["Не указано, какие существующие тесты меняем или добавляем"]},
        required_step_statuses={},
    )

    test_gap = next(item for item in obligations if item["statement"] == "Не указано, какие существующие тесты меняем или добавляем")
    assert test_gap["blocking"] is True
    assert test_gap["status"] == "open"


def test_build_obligation_matrix_tracks_required_input_gaps_as_task_obligations() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec для локальной backend-доработки",
        required_sections=["Контекст"],
        repo_grounded_required=True,
        required_inputs=["Что меняем и что нельзя сломать", "Какие ограничения по совместимости UX"],
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"required_input_gaps": ["Какие ограничения по совместимости UX"]},
        required_step_statuses={},
    )

    by_statement = {item["statement"]: item for item in obligations if "обязательный вход задачи" in item["statement"]}
    assert by_statement["Явно закрыть обязательный вход задачи: Что меняем и что нельзя сломать"]["status"] == "closed"
    assert by_statement["Явно закрыть обязательный вход задачи: Какие ограничения по совместимости UX"]["status"] == "open"


def test_build_obligation_matrix_matches_required_input_gaps_by_semantic_fingerprint() -> None:
    task_contract = build_task_contract(
        user_query="Локальная доработка",
        required_sections=[],
        repo_grounded_required=True,
        required_inputs=["Требования к совместимости UX"],
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"required_input_gaps": ["Требования к совместимости UX."]},
        required_step_statuses={},
    )

    obligation = next(item for item in obligations if item["obligation_id"].startswith("required_input:"))
    assert obligation["status"] == "open"


def test_build_obligation_matrix_matches_required_input_gap_with_explanatory_prefix() -> None:
    task_contract = build_task_contract(
        user_query="Локальная доработка",
        required_sections=[],
        repo_grounded_required=True,
        required_inputs=["Требования к совместимости UX"],
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"required_input_gaps": ["Не закрыты требования к совместимости UX"]},
        required_step_statuses={},
    )

    obligation = next(item for item in obligations if item["obligation_id"].startswith("required_input:"))
    assert obligation["status"] == "open"


def test_build_obligation_matrix_tracks_protected_shell_loss_as_blocking_obligation() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=["Требования"],
        repo_grounded_required=True,
        protected_spec_shell={
            "title": "Техническое задание",
            "source_task_section": "Исходная задача",
            "core_sections": ["Контекст", "Требования"],
            "open_questions_section": "Открытые вопросы и валидационные шаги",
        },
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={"missing_sections": ["Техническое задание", "Исходная задача", "Контекст"]},
        required_step_statuses={},
    )

    by_statement = {item["statement"]: item for item in obligations}
    assert by_statement["Сохранить protected spec shell title: Техническое задание"]["status"] == "open"
    assert by_statement["Сохранить protected spec shell title: Техническое задание"]["blocking"] is True
    assert by_statement["Сохранить protected spec shell section: Исходная задача"]["status"] == "open"
    assert by_statement["Сохранить protected spec shell section: Контекст"]["status"] == "open"
    assert by_statement["Сохранить protected spec shell section: Требования"]["status"] == "closed"
    assert (
        by_statement[
            "Сохранить protected spec shell section: Открытые вопросы и валидационные шаги"
        ]["status"]
        == "closed"
    )


def test_build_obligation_matrix_tracks_external_reference_loss_as_blocking_obligation() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec с внешним референсом",
        required_sections=["Контекст"],
        repo_grounded_required=True,
        protected_spec_shell={
            "title": "Техническое задание",
            "source_task_section": "Исходная задача",
            "core_sections": ["Контекст"],
            "open_questions_section": "Открытые вопросы и валидационные шаги",
            "external_references_section": "Внешние референсы и примеры реализации",
            "external_reference_targets": [
                {
                    "source": "https://github.com/vakovalskii/codedash",
                    "local_mapping": "app/services/session_transfer/service.py",
                    "adaptation_status": "requires-validation",
                }
            ],
        },
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "missing_sections": ["Внешние референсы и примеры реализации"],
            "external_reference_gaps": [
                "https://github.com/vakovalskii/codedash "
                "-> app/services/session_transfer/service.py [requires-validation]"
            ],
        },
        required_step_statuses={},
    )

    by_statement = {item["statement"]: item for item in obligations}
    assert (
        by_statement["Сохранить protected spec shell section: Внешние референсы и примеры реализации"]["status"]
        == "open"
    )
    guidance_obligation = by_statement[
        "Сохранить implementation guidance для внешнего референса: "
        "https://github.com/vakovalskii/codedash "
        "-> app/services/session_transfer/service.py [requires-validation]"
    ]
    assert guidance_obligation["status"] == "open"
    assert guidance_obligation["blocking"] is True
    assert guidance_obligation["source"] == "task"


def test_build_obligation_matrix_keeps_stale_artifact_binding_from_verifier_open() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=["Контекст"],
        repo_grounded_required=True,
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "followup_open_blocking_obligations": [
                {
                    "obligation_id": "followup_review:artifact_binding",
                    "statement": "Verifier validated stale persisted draft artifact: path mismatch.",
                    "status": "open",
                    "blocking": True,
                    "source": "verifier",
                }
            ],
            "followup_false_closures": [
                {
                    "obligation_id": "followup_review:artifact_binding",
                    "statement": "Verifier validated stale persisted draft artifact: path mismatch.",
                    "status": "open",
                    "blocking": True,
                    "source": "verifier",
                }
            ],
        },
        required_step_statuses={},
    )

    obligation = next(item for item in obligations if item["obligation_id"] == "followup_review:artifact_binding")
    assert obligation["status"] == "open"
    assert obligation["blocking"] is True
    assert obligation["source"] == "verifier"


def test_build_obligation_matrix_downgrades_advisory_followup_validation_gate_to_non_blocking() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=["Контекст"],
        repo_grounded_required=True,
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "followup_open_blocking_obligations": [
                {
                    "obligation_id": "followup_review:manual_validation_gate",
                    "statement": (
                        "Не подтверждена совместимость hand-written `rollout-*.jsonl`; "
                        "это остаётся manual validation gate для phase 1."
                    ),
                    "status": "unverified",
                    "blocking": True,
                    "source": "verifier",
                }
            ],
        },
        required_step_statuses={},
    )

    obligation = next(
        item for item in obligations if item["obligation_id"] == "followup_review:manual_validation_gate"
    )
    assert obligation["status"] == "unverified"
    assert obligation["blocking"] is False
    assert obligation["source"] == "verifier"


def test_build_obligation_matrix_downgrades_out_of_scope_followup_obligation_to_non_blocking() -> None:
    task_contract = build_task_contract(
        user_query="Подготовить implementation-ready spec",
        required_sections=["Контекст"],
        repo_grounded_required=True,
    )

    obligations = build_obligation_matrix(
        task_contract=task_contract,
        assessment={
            "followup_open_blocking_obligations": [
                {
                    "obligation_id": "followup_review:out_of_scope_surface",
                    "statement": "MiniApp parity вне scope этой задачи и не должен блокировать текущую реализацию.",
                    "status": "open",
                    "blocking": True,
                    "source": "verifier",
                }
            ],
        },
        required_step_statuses={},
    )

    obligation = next(
        item for item in obligations if item["obligation_id"] == "followup_review:out_of_scope_surface"
    )
    assert obligation["status"] == "open"
    assert obligation["blocking"] is False
    assert obligation["source"] == "verifier"
