from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Set


_DEFAULT_BLOCKING_REPO_FIELDS = (
    "codebase_mismatches",
    "unsupported_assumptions",
    "unverified_claims",
    "evidence_gaps",
)

_DEFAULT_NON_BLOCKING_REPO_FIELDS = (
    "issues",
    "weak_sections",
)

_TASK_GAP_HINTS = {
    "config_contract_gaps": (
        "config",
        "configuration",
        "конфиг",
        "настройк",
        "settings",
        "env",
        "environment",
    ),
    "migration_gaps": (
        "migration",
        "migrate",
        "миграц",
        "schema migration",
        "schema change",
        "schema update",
        "миграция схем",
        "изменение схем",
    ),
    "doc_sync_gaps": (
        "readme",
        "docs",
        "documentation",
        "документац",
        "инструк",
        "manual",
        "guide",
    ),
    "test_gaps": (
        "test",
        "tests",
        "testing",
        "тест",
        "приемк",
        "acceptance",
        "smoke",
        "regress",
        "валидац",
        "validation",
    ),
}

_OBLIGATION_STOP_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "в",
    "во",
    "для",
    "и",
    "или",
    "из",
    "к",
    "на",
    "но",
    "о",
    "от",
    "по",
    "под",
    "при",
    "что",
}


def _normalize_string_list(items: List[str] | None) -> List[str]:
    return [str(item).strip() for item in (items or []) if str(item).strip()]


def _normalize_obligation_token(token: str) -> str:
    normalized = str(token or "").strip().lower()
    if len(normalized) > 4 and normalized.endswith("ies"):
        return normalized[:-3] + "y"
    if len(normalized) > 4:
        for suffix in ("ing", "ed", "es", "s"):
            if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 4:
                return normalized[: -len(suffix)]
    return normalized


def _obligation_fingerprint(text: str) -> str:
    raw_tokens = re.findall(r"[0-9a-zа-яё_]+", str(text or "").strip().lower())
    normalized_tokens = [
        _normalize_obligation_token(token)
        for token in raw_tokens
        if token and token not in _OBLIGATION_STOP_WORDS
    ]
    unique_tokens = sorted(set(token for token in normalized_tokens if token))
    if unique_tokens:
        return " ".join(unique_tokens)
    return " ".join(str(text or "").strip().lower().split())


def _obligation_token_set(text: str) -> Set[str]:
    fingerprint = _obligation_fingerprint(text)
    if not fingerprint:
        return set()
    return {token for token in fingerprint.split() if token}


def _stable_obligation_id(prefix: str, text: str) -> str:
    normalized_prefix = str(prefix or "").strip() or "obligation"
    normalized_text = _obligation_fingerprint(text)
    if not normalized_text:
        return normalized_prefix
    digest = hashlib.sha1(normalized_text.encode("utf-8")).hexdigest()[:12]
    return f"{normalized_prefix}:{digest}"


def _normalize_protected_spec_shell(protected_spec_shell: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(protected_spec_shell, dict):
        return {}

    title = str(protected_spec_shell.get("title") or "").strip()
    source_task_section = str(protected_spec_shell.get("source_task_section") or "").strip()
    open_questions_section = str(protected_spec_shell.get("open_questions_section") or "").strip()
    external_references_section = str(protected_spec_shell.get("external_references_section") or "").strip()
    core_sections = _normalize_string_list(protected_spec_shell.get("core_sections"))
    external_reference_targets = _normalize_external_reference_targets(
        protected_spec_shell.get("external_reference_targets")
    )

    normalized: Dict[str, Any] = {}
    if title:
        normalized["title"] = title
    if source_task_section:
        normalized["source_task_section"] = source_task_section
    if core_sections:
        normalized["core_sections"] = core_sections
    if open_questions_section:
        normalized["open_questions_section"] = open_questions_section
    if external_references_section:
        normalized["external_references_section"] = external_references_section
    if external_reference_targets:
        normalized["external_reference_targets"] = external_reference_targets
    return normalized


def _normalize_external_reference_targets(items: Any) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    seen: Set[str] = set()
    if not isinstance(items, list):
        return normalized
    for item in items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        local_mapping = str(item.get("local_mapping") or "").strip()
        adaptation_status = str(item.get("adaptation_status") or "").strip()
        research_artifact = str(item.get("research_artifact") or "").strip()
        if not source and not local_mapping:
            continue
        key = _external_reference_target_key(
            source=source,
            local_mapping=local_mapping,
            adaptation_status=adaptation_status,
        )
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "source": source,
                "local_mapping": local_mapping,
                "adaptation_status": adaptation_status,
                "research_artifact": research_artifact,
            }
        )
    return normalized


def _external_reference_target_key(
    *,
    source: str,
    local_mapping: str = "",
    adaptation_status: str = "",
) -> str:
    parts: List[str] = []
    source_text = str(source or "").strip()
    local_mapping_text = str(local_mapping or "").strip()
    adaptation_status_text = str(adaptation_status or "").strip()
    if source_text:
        parts.append(source_text)
    if local_mapping_text:
        parts.append(f"-> {local_mapping_text}")
    if adaptation_status_text:
        parts.append(f"[{adaptation_status_text}]")
    return " ".join(parts).strip()


def _matches_obligation_target(
    target: str,
    *,
    live_items: Set[str],
    live_fingerprints: Set[str],
    live_token_sets: List[Set[str]],
) -> bool:
    normalized_target = str(target or "").strip()
    if not normalized_target:
        return False
    if normalized_target in live_items:
        return True
    fingerprint = _obligation_fingerprint(normalized_target)
    if fingerprint and fingerprint in live_fingerprints:
        return True
    target_tokens = _obligation_token_set(normalized_target)
    if target_tokens and any(target_tokens.issubset(item_tokens) for item_tokens in live_token_sets):
        return True
    return False


def _derive_project_gap_fields(
    *,
    user_query: str,
    template_name: str,
    template_description: str,
    required_sections: List[str],
    required_inputs: List[str],
    traceability_rules: List[str],
    repo_grounded_required: bool,
    is_large_spec: bool,
) -> tuple[List[str], List[str]]:
    blocking = list(_DEFAULT_BLOCKING_REPO_FIELDS) if repo_grounded_required else []
    non_blocking = list(_DEFAULT_NON_BLOCKING_REPO_FIELDS)
    normalized_sections = _normalize_string_list(required_sections)
    normalized_inputs = _normalize_string_list(required_inputs)
    normalized_rules = _normalize_string_list(traceability_rules)
    task_context = " \n".join(
        [
            str(user_query or "").strip(),
            str(template_name or "").strip(),
            str(template_description or "").strip(),
            *normalized_sections,
            *normalized_inputs,
            *normalized_rules,
        ]
    ).lower()

    if normalized_sections:
        non_blocking.append("weak_sections")
    if normalized_rules:
        non_blocking.append("traceability_gaps")
    if normalized_rules or is_large_spec:
        non_blocking.append("missing_counts")

    if repo_grounded_required and any(hint in task_context for hint in _TASK_GAP_HINTS["config_contract_gaps"]):
        blocking.append("config_contract_gaps")
    if repo_grounded_required and any(hint in task_context for hint in _TASK_GAP_HINTS["migration_gaps"]):
        blocking.append("migration_gaps")
    doc_sync_task_relevant = any(hint in task_context for hint in _TASK_GAP_HINTS["doc_sync_gaps"])
    test_task_relevant = any(hint in task_context for hint in _TASK_GAP_HINTS["test_gaps"])
    if doc_sync_task_relevant:
        if repo_grounded_required:
            blocking.append("doc_sync_gaps")
        else:
            non_blocking.append("doc_sync_gaps")
    if test_task_relevant:
        if repo_grounded_required:
            blocking.append("test_gaps")
        else:
            non_blocking.append("test_gaps")

    return list(dict.fromkeys(blocking)), list(dict.fromkeys(non_blocking))


def _is_blocking_degraded_mode(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "retry_exhausted",
            "invalid_bundle",
            "execution_failed",
            "bundle_incomplete",
            "task_contract_missing",
            "claim_ledger_missing",
            "obligation_matrix_missing",
            "open_gaps_missing",
        )
    )


def build_task_contract(
    *,
    user_query: str,
    required_sections: List[str],
    repo_grounded_required: bool,
    qa_prompt: str = "",
    template_name: str = "",
    template_description: str = "",
    is_large_spec: bool = False,
    required_step_ids: List[str] | None = None,
    required_artifacts: List[str] | None = None,
    required_inputs: List[str] | None = None,
    traceability_rules: List[str] | None = None,
    protected_spec_shell: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized_sections = _normalize_string_list(required_sections)
    normalized_required_inputs = _normalize_string_list(required_inputs)
    normalized_traceability_rules = _normalize_string_list(traceability_rules)
    normalized_protected_spec_shell = _normalize_protected_spec_shell(protected_spec_shell)
    artifact_requirements = _normalize_string_list(
        required_artifacts
        if required_artifacts is not None
        else (
            [
                "task_contract",
                "claim_ledger",
                "fact_pack",
                "draft",
                "artifacts_index",
                "obligation_matrix",
            ]
            if repo_grounded_required
            else []
        )
    )
    blocking_project_gap_fields, non_blocking_project_gap_fields = _derive_project_gap_fields(
        user_query=user_query,
        template_name=template_name,
        template_description=template_description,
        required_sections=normalized_sections,
        required_inputs=normalized_required_inputs,
        traceability_rules=normalized_traceability_rules,
        repo_grounded_required=repo_grounded_required,
        is_large_spec=is_large_spec,
    )
    task_obligations: List[Dict[str, Any]] = [
        {
            "obligation_id": f"section:{section}",
            "statement": f"Заполнить обязательный раздел: {section}",
            "status": "open",
            "blocking": True,
            "source": "task",
            "closure_rule": "Раздел должен присутствовать и быть заполнен конкретным содержанием.",
            "assessment_kind": "missing_section",
            "assessment_target": section,
            "auto_closable": False,
        }
        for section in normalized_sections
    ]
    protected_shell_targets: List[tuple[str, str, str]] = []
    protected_title = str(normalized_protected_spec_shell.get("title") or "").strip()
    external_references_section = str(normalized_protected_spec_shell.get("external_references_section") or "").strip()
    external_reference_targets = list(normalized_protected_spec_shell.get("external_reference_targets") or [])
    if protected_title:
        protected_shell_targets.append(
            (
                _stable_obligation_id("protected_shell_title", protected_title),
                f"Сохранить protected spec shell title: {protected_title}",
                protected_title,
            )
        )
    for section_name in (
        str(normalized_protected_spec_shell.get("source_task_section") or "").strip(),
        *[
            str(item).strip()
            for item in (normalized_protected_spec_shell.get("core_sections") or [])
            if str(item).strip()
        ],
        str(normalized_protected_spec_shell.get("open_questions_section") or "").strip(),
    ):
        if not section_name:
            continue
        protected_shell_targets.append(
            (
                _stable_obligation_id("protected_shell_section", section_name),
                f"Сохранить protected spec shell section: {section_name}",
                section_name,
            )
        )
    if external_references_section and external_reference_targets:
        protected_shell_targets.append(
            (
                _stable_obligation_id("protected_shell_section", external_references_section),
                f"Сохранить protected spec shell section: {external_references_section}",
                external_references_section,
            )
        )
    task_obligations.extend(
        {
            "obligation_id": obligation_id,
            "statement": statement,
            "status": "open",
            "blocking": True,
            "source": "task",
            "closure_rule": (
                "Protected spec shell не должен терять title, `Исходная задача`, "
                "core sections или `Открытые вопросы и валидационные шаги`."
            ),
            "assessment_kind": "protected_shell_loss",
            "assessment_target": target,
            "auto_closable": False,
        }
        for obligation_id, statement, target in protected_shell_targets
    )
    task_obligations.extend(
        {
            "obligation_id": _stable_obligation_id(
                "external_reference",
                _external_reference_target_key(
                    source=str(item.get("source") or "").strip(),
                    local_mapping=str(item.get("local_mapping") or "").strip(),
                    adaptation_status=str(item.get("adaptation_status") or "").strip(),
                ),
            ),
            "statement": (
                "Сохранить implementation guidance для внешнего референса: "
                + _external_reference_target_key(
                    source=str(item.get("source") or "").strip(),
                    local_mapping=str(item.get("local_mapping") or "").strip(),
                    adaptation_status=str(item.get("adaptation_status") or "").strip(),
                )
            ),
            "status": "open",
            "blocking": True,
            "source": "task",
            "closure_rule": (
                "Conditional section `Внешние референсы и примеры реализации` не должна терять "
                "source, local mapping и статус адаптации для подтверждённых implementation examples."
            ),
            "assessment_kind": "external_reference_gap",
            "assessment_target": _external_reference_target_key(
                source=str(item.get("source") or "").strip(),
                local_mapping=str(item.get("local_mapping") or "").strip(),
                adaptation_status=str(item.get("adaptation_status") or "").strip(),
            ),
            "evidence_refs": [
                value
                for value in (
                    str(item.get("source") or "").strip(),
                    str(item.get("local_mapping") or "").strip(),
                    str(item.get("research_artifact") or "").strip(),
                )
                if value
            ],
            "auto_closable": False,
        }
        for item in external_reference_targets
        if _external_reference_target_key(
            source=str(item.get("source") or "").strip(),
            local_mapping=str(item.get("local_mapping") or "").strip(),
            adaptation_status=str(item.get("adaptation_status") or "").strip(),
        )
    )
    task_obligations.extend(
        {
            "obligation_id": _stable_obligation_id("required_input", required_input),
            "statement": f"Явно закрыть обязательный вход задачи: {required_input}",
            "status": "open",
            "blocking": True,
            "source": "task",
            "closure_rule": "Каждый обязательный вход задачи должен быть явно закрыт в финальном документе.",
            "assessment_kind": "required_input_gap",
            "assessment_target": required_input,
            "auto_closable": False,
        }
        for required_input in normalized_required_inputs
    )
    if normalized_traceability_rules:
        task_obligations.append(
            {
                "obligation_id": "task:traceability_rules",
                "statement": "Соблюсти task-specific правила трассируемости: " + "; ".join(normalized_traceability_rules),
                "status": "open",
                "blocking": True,
                "source": "task",
                "closure_rule": "В финальном документе не должно оставаться traceability gaps или недобора по количественным требованиям.",
                "assessment_kind": "traceability_rule_set",
                "assessment_fields": ["traceability_gaps", "missing_counts"],
                "auto_closable": False,
            }
        )
    return {
        "requested_outcome": str(user_query or "").strip(),
        "implementation_readiness_definition": (
            "Документ должен быть implementable без догадок и закрывать все blocking obligations."
        ),
        "project_constraints": [
            "Использовать только project-grounded evidence.",
            "Не выдумывать API, config keys, fallback layers или compatibility wrappers.",
        ],
        "evidence_requirements": ["repo_grounded"] if repo_grounded_required else [],
        "validation_requirements": ["blocking_obligations_closed", "runtime_ready_verdict"],
        "blocking_unknown_policy": (
            'Если evidence недостаточно, использовать формулировки "не подтверждено" '
            'или "требует отдельной проверки", а не догадки.'
        ),
        "required_sections": normalized_sections,
        "repo_grounded_required": bool(repo_grounded_required),
        "qa_prompt": str(qa_prompt or "").strip(),
        "template_name": str(template_name or "").strip(),
        "template_description": str(template_description or "").strip(),
        "is_large_spec": bool(is_large_spec),
        "required_repo_steps": _normalize_string_list(required_step_ids),
        "required_artifacts": artifact_requirements,
        "required_inputs": normalized_required_inputs,
        "traceability_rules": normalized_traceability_rules,
        "protected_spec_shell": normalized_protected_spec_shell,
        "blocking_project_gap_fields": blocking_project_gap_fields,
        "non_blocking_project_gap_fields": non_blocking_project_gap_fields,
        "task_obligations": task_obligations,
    }


def normalize_obligation_items(items: Any) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return normalized
    seen: Set[str] = set()
    for idx, item in enumerate(items, start=1):
        if isinstance(item, dict):
            obligation_id = str(item.get("obligation_id") or item.get("id") or f"obligation_{idx}").strip()
            statement = str(item.get("statement") or item.get("text") or "").strip()
            status = str(item.get("status") or "open").strip().lower() or "open"
            blocking = bool(item.get("blocking", True))
            source = str(item.get("source") or "runtime").strip() or "runtime"
            evidence_refs = [
                str(ref).strip()
                for ref in (item.get("evidence_refs") or item.get("evidence") or [])
                if str(ref).strip()
            ]
        else:
            obligation_id = f"obligation_{idx}"
            statement = str(item or "").strip()
            status = "open"
            blocking = True
            source = "runtime"
            evidence_refs = []
        if not statement:
            continue
        key = obligation_id or f"statement::{statement}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "obligation_id": obligation_id,
                "statement": statement,
                "status": status if status in {"open", "closed", "unverified", "not_applicable"} else "open",
                "blocking": blocking,
                "source": source,
                "closure_rule": str(item.get("closure_rule") if isinstance(item, dict) else "").strip(),
                "evidence_refs": evidence_refs,
                "auto_closable": bool(item.get("auto_closable", False)) if isinstance(item, dict) else False,
                "assessment_kind": str(item.get("assessment_kind") if isinstance(item, dict) else "").strip(),
                "assessment_target": str(item.get("assessment_target") if isinstance(item, dict) else "").strip(),
                "assessment_fields": [
                    str(field).strip()
                    for field in ((item.get("assessment_fields") or []) if isinstance(item, dict) else [])
                    if str(field).strip()
                ],
            }
        )
    return normalized


def split_obligations_by_blocking(obligations: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    blocking: List[Dict[str, Any]] = []
    non_blocking: List[Dict[str, Any]] = []
    for item in obligations:
        if not isinstance(item, dict):
            continue
        if bool(item.get("blocking")):
            blocking.append(item)
        else:
            non_blocking.append(item)
    return {"blocking": blocking, "non_blocking": non_blocking}


def collect_open_blocking_obligations(obligations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in obligations
        if isinstance(item, dict)
        and bool(item.get("blocking"))
        and str(item.get("status") or "").strip().lower() in {"open", "unverified"}
    ]


def _is_advisory_validation_gate(statement: str, *, status: str) -> bool:
    normalized = str(statement or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    if not normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "artifact index",
            "artifacts_index",
            "stale persisted draft artifact",
            "false closure",
            "path mismatch",
            "expected_sha1",
            "actual_sha1",
        )
    ):
        return False
    if normalized_status == "unverified":
        return True
    return any(
        marker in normalized
        for marker in (
            "requires-validation",
            "requires validation",
            "требует отдельной проверки",
            "manual validation gate",
            "out of scope",
            "вне scope",
            "вне рамок этой задачи",
            "не входит в scope",
            "phase 2",
            "phase 1 cli resumability",
            "не подтверждено",
        )
    )


def build_obligation_matrix(
    *,
    task_contract: Dict[str, Any],
    assessment: Dict[str, Any],
    required_step_statuses: Dict[str, str],
) -> List[Dict[str, Any]]:
    obligations: List[Dict[str, Any]] = []
    closed_selectors: Set[str] = set()
    closed_fingerprints: Set[str] = set()
    for field_name in ("fix_closed_obligations", "followup_closed_blocking_obligations"):
        for item in (assessment.get(field_name) or []):
            text = str(item or "").strip()
            if text:
                closed_selectors.add(text)
                fingerprint = _obligation_fingerprint(text)
                if fingerprint:
                    closed_fingerprints.add(fingerprint)

    def _append(
        *,
        obligation_id: str,
        statement: str,
        blocking: bool,
        status: str,
        source: str,
        closure_rule: str,
        evidence_refs: List[str] | None = None,
        auto_closable: bool = False,
        allow_external_close: bool = False,
    ) -> None:
        text = str(statement or "").strip()
        if not text:
            return
        normalized_status = status if status in {"open", "closed", "unverified", "not_applicable"} else "open"
        selector = str(obligation_id or "").strip()
        if allow_external_close and normalized_status in {"open", "unverified"} and (
            selector in closed_selectors
            or text in closed_selectors
            or _obligation_fingerprint(text) in closed_fingerprints
        ):
            normalized_status = "closed"
        obligations.append(
            {
                "obligation_id": selector or f"obligation_{len(obligations) + 1}",
                "statement": text,
                "blocking": bool(blocking),
                "status": normalized_status,
                "source": str(source or "runtime").strip() or "runtime",
                "closure_rule": str(closure_rule or "").strip(),
                "evidence_refs": [str(item).strip() for item in (evidence_refs or []) if str(item).strip()],
                "auto_closable": bool(auto_closable),
            }
        )

    blocking_field_candidates = task_contract.get("blocking_project_gap_fields")
    if not isinstance(blocking_field_candidates, (list, tuple, set)):
        blocking_field_candidates = _DEFAULT_BLOCKING_REPO_FIELDS
    non_blocking_field_candidates = task_contract.get("non_blocking_project_gap_fields")
    if not isinstance(non_blocking_field_candidates, (list, tuple, set)):
        non_blocking_field_candidates = _DEFAULT_NON_BLOCKING_REPO_FIELDS

    blocking_repo_fields = [
        field_name
        for field_name in blocking_field_candidates
        if any(str(item).strip() for item in (assessment.get(field_name) or []))
    ]
    non_blocking_repo_fields = [
        field_name
        for field_name in non_blocking_field_candidates
        if any(str(item).strip() for item in (assessment.get(field_name) or []))
    ]
    required_artifacts = [
        str(item).strip()
        for item in (task_contract.get("required_artifacts") or [])
        if str(item).strip()
    ]
    missing_required_artifacts = {
        str(item).strip()
        for item in (assessment.get("missing_required_artifacts") or [])
        if str(item).strip()
    }
    missing_sections = {str(item).strip() for item in (assessment.get("missing_sections") or []) if str(item).strip()}
    missing_section_fingerprints = {
        _obligation_fingerprint(item)
        for item in missing_sections
        if _obligation_fingerprint(item)
    }
    missing_section_token_sets = [
        _obligation_token_set(item)
        for item in missing_sections
        if _obligation_token_set(item)
    ]
    required_input_gaps = {
        str(item).strip()
        for item in (assessment.get("required_input_gaps") or [])
        if str(item).strip()
    }
    required_input_gap_fingerprints = {
        _obligation_fingerprint(item)
        for item in required_input_gaps
        if _obligation_fingerprint(item)
    }
    required_input_gap_token_sets = [
        _obligation_token_set(item)
        for item in required_input_gaps
        if _obligation_token_set(item)
    ]
    external_reference_gaps = {
        str(item).strip()
        for item in (assessment.get("external_reference_gaps") or [])
        if str(item).strip()
    }
    external_reference_gap_fingerprints = {
        _obligation_fingerprint(item)
        for item in external_reference_gaps
        if _obligation_fingerprint(item)
    }
    external_reference_gap_token_sets = [
        _obligation_token_set(item)
        for item in external_reference_gaps
        if _obligation_token_set(item)
    ]
    task_obligations = normalize_obligation_items(task_contract.get("task_obligations") or [])
    for task_item in task_obligations:
        assessment_kind = str(task_item.get("assessment_kind") or "").strip()
        status = str(task_item.get("status") or "open").strip().lower() or "open"
        if assessment_kind in {"missing_section", "protected_shell_loss"}:
            target = str(task_item.get("assessment_target") or "").strip()
            status = (
                "open"
                if _matches_obligation_target(
                    target,
                    live_items=missing_sections,
                    live_fingerprints=missing_section_fingerprints,
                    live_token_sets=missing_section_token_sets,
                )
                else "closed"
            )
        elif assessment_kind == "required_input_gap":
            target = str(task_item.get("assessment_target") or "").strip()
            status = (
                "open"
                if _matches_obligation_target(
                    target,
                    live_items=required_input_gaps,
                    live_fingerprints=required_input_gap_fingerprints,
                    live_token_sets=required_input_gap_token_sets,
                )
                else "closed"
            )
        elif assessment_kind == "external_reference_gap":
            target = str(task_item.get("assessment_target") or "").strip()
            status = (
                "open"
                if _matches_obligation_target(
                    target,
                    live_items=external_reference_gaps,
                    live_fingerprints=external_reference_gap_fingerprints,
                    live_token_sets=external_reference_gap_token_sets,
                )
                else "closed"
            )
        elif assessment_kind == "traceability_rule_set":
            fields = [
                str(field_name).strip()
                for field_name in (task_item.get("assessment_fields") or [])
                if str(field_name).strip()
            ]
            has_gaps = any(any(str(item).strip() for item in (assessment.get(field_name) or [])) for field_name in fields)
            status = "open" if has_gaps else "closed"
        _append(
            obligation_id=str(task_item.get("obligation_id") or "").strip(),
            statement=str(task_item.get("statement") or "").strip(),
            blocking=bool(task_item.get("blocking", True)),
            status=status,
            source=str(task_item.get("source") or "task").strip() or "task",
            closure_rule=str(task_item.get("closure_rule") or "").strip(),
            evidence_refs=list(task_item.get("evidence_refs") or []),
            auto_closable=bool(task_item.get("auto_closable", False)),
            allow_external_close=False,
        )

    for artifact_name in required_artifacts:
        _append(
            obligation_id=f"artifact:{artifact_name}",
            statement=f"Подготовить обязательный runtime artifact: {artifact_name}",
            blocking=True,
            status="open" if artifact_name in missing_required_artifacts else "closed",
            source="constraint",
            closure_rule="Обязательный artifact должен быть доступен для CLI fix/review loop.",
            evidence_refs=[artifact_name],
            auto_closable=False,
            allow_external_close=False,
        )

    for step_id, status in sorted((required_step_statuses or {}).items()):
        normalized = str(status or "").strip().lower()
        _append(
            obligation_id=f"repo_step:{step_id}",
            statement=f"Обязательный repo-grounded шаг должен завершиться со статусом ok: {step_id}",
            blocking=True,
            status="closed" if normalized == "ok" else "open",
            source="constraint",
            closure_rule="Статус обязательного шага должен быть ok.",
            evidence_refs=[step_id],
            auto_closable=False,
            allow_external_close=False,
        )

    for field_name in blocking_repo_fields:
        for item in assessment.get(field_name) or []:
            text = str(item or "").strip()
            if not text:
                continue
            _append(
                obligation_id=_stable_obligation_id(field_name, text),
                statement=text,
                blocking=True,
                status="open",
                source="project",
                closure_rule="Пробел должен быть устранён или явно доказан кодовой базой.",
                auto_closable=False,
                allow_external_close=False,
            )

    for field_name in non_blocking_repo_fields:
        for item in assessment.get(field_name) or []:
            text = str(item or "").strip()
            if not text:
                continue
            _append(
                obligation_id=_stable_obligation_id(field_name, text),
                statement=text,
                blocking=False,
                status="open",
                source="project",
                closure_rule="Пробел должен быть учтён или помечен как follow-up.",
                auto_closable=False,
                allow_external_close=False,
            )

    for idx, item in enumerate(assessment.get("fix_remaining_obligations") or [], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("statement") or item.get("text") or "").strip()
        if not text:
            continue
        status = str(item.get("status") or "open")
        blocking = bool(item.get("blocking", True))
        if blocking and _is_advisory_validation_gate(text, status=status):
            blocking = False
        _append(
            obligation_id=str(item.get("obligation_id") or f"fix_remaining:{idx}"),
            statement=text,
            blocking=blocking,
            status=status,
            source=str(item.get("source") or "spec_fixer"),
            closure_rule=str(item.get("closure_rule") or "Spec fixer must either close or preserve this obligation."),
            evidence_refs=list(item.get("evidence_refs") or []),
            auto_closable=bool(item.get("auto_closable", False)),
            allow_external_close=True,
        )

    for item in assessment.get("followup_open_blocking_obligations") or []:
        text = str(item.get("statement") or item.get("text") or item or "").strip()
        if not text:
            continue
        status = str(item.get("status") or "open") if isinstance(item, dict) else "open"
        blocking = True
        if _is_advisory_validation_gate(text, status=status):
            blocking = False
        _append(
            obligation_id=(
                str(item.get("obligation_id") or "").strip()
                if isinstance(item, dict) and str(item.get("obligation_id") or "").strip()
                else _stable_obligation_id("followup_open", text)
            ),
            statement=text,
            blocking=blocking,
            status=status,
            source="verifier",
            closure_rule=(
                str(item.get("closure_rule") or "Verifier must confirm closure.")
                if isinstance(item, dict)
                else "Verifier must confirm closure."
            ),
            evidence_refs=list(item.get("evidence_refs") or []) if isinstance(item, dict) else [],
            auto_closable=False,
            allow_external_close=False,
        )

    for item in assessment.get("followup_false_closures") or []:
        text = str(item.get("statement") or item.get("text") or item or "").strip()
        if not text:
            continue
        _append(
            obligation_id=(
                str(item.get("obligation_id") or "").strip()
                if isinstance(item, dict) and str(item.get("obligation_id") or "").strip()
                else _stable_obligation_id("false_closure", text)
            ),
            statement=text,
            blocking=True,
            status="open",
            source="verifier",
            closure_rule="Verifier must no longer detect false closure.",
            evidence_refs=list(item.get("evidence_refs") or []) if isinstance(item, dict) else [],
            auto_closable=False,
            allow_external_close=False,
        )

    for item in assessment.get("degraded_modes") or []:
        text = str(item or "").strip()
        if not text:
            continue
        _append(
            obligation_id=_stable_obligation_id("runtime_mode", text),
            statement=f"Устранить деградировавший runtime/CLI режим: {text}",
            blocking=_is_blocking_degraded_mode(text),
            status="open",
            source="constraint",
            closure_rule="Критический degraded mode не должен присутствовать в финальном run.",
            auto_closable=False,
            allow_external_close=False,
        )

    def _status_rank(status: str) -> int:
        normalized = str(status or "").strip().lower()
        if normalized == "open":
            return 4
        if normalized == "unverified":
            return 3
        if normalized == "closed":
            return 2
        if normalized == "not_applicable":
            return 1
        return 0

    seen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in obligations:
        obligation_id = str(item.get("obligation_id") or "").strip()
        key = obligation_id or f"statement::{item['statement']}"
        existing = seen.get(key)
        if existing is None:
            seen[key] = dict(item)
            order.append(key)
            continue
        merged = dict(existing)
        if bool(item.get("blocking")):
            merged["blocking"] = True
        if _status_rank(item.get("status")) > _status_rank(existing.get("status")) or (
            item.get("source") == "verifier" and item.get("status") == existing.get("status")
        ):
            merged["statement"] = item.get("statement") or existing.get("statement", "")
            merged["status"] = item.get("status")
            merged["source"] = item.get("source")
            merged["closure_rule"] = item.get("closure_rule") or existing.get("closure_rule", "")
            merged["auto_closable"] = bool(item.get("auto_closable", False))
        merged["evidence_refs"] = list(
            dict.fromkeys(
                [
                    str(ref).strip()
                    for ref in list(existing.get("evidence_refs") or []) + list(item.get("evidence_refs") or [])
                    if str(ref).strip()
                ]
            )
        )
        seen[key] = merged
    return [seen[key] for key in order]
