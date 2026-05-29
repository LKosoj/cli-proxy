from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

REPO_GAP_LABELS: Dict[str, str] = {
    "issues": "Замечания",
    "weak_sections": "Слабые разделы",
    "missing_counts": "Недобор по количественным требованиям",
    "traceability_gaps": "Пробелы трассируемости",
    "codebase_mismatches": "Несоответствия кодовой базе",
    "unsupported_assumptions": "Неподтвержденные предположения",
    "unverified_claims": "Неподтвержденные product/capability claims",
    "evidence_gaps": "Пробелы evidence/traceability",
    "degraded_modes": "Деградировавшие runtime/CLI режимы",
    "config_contract_gaps": "Пробелы в config-контракте",
    "migration_gaps": "Пробелы миграции",
    "doc_sync_gaps": "Пробелы синхронизации документации",
    "test_gaps": "Пробелы тестового покрытия",
    "external_reference_gaps": "Потеря внешних референсов и implementation guidance",
}

_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*$")
_PLACEHOLDER_MARKERS = (
    ("TODO", re.compile(r"\bTODO\b", re.IGNORECASE)),
    ("TBD", re.compile(r"\bTBD\b", re.IGNORECASE)),
    ("дописать позже", re.compile(r"дописать позже", re.IGNORECASE)),
)
_SKIP_GAP_MARKERS = (
    "requires-validation",
    "requires_validation",
    "validation gate",
    "validation-gate",
    "awaiting verification",
    "artifact needed",
    "hypothesis",
)


def _is_expected_open_question(text: Any) -> bool:
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in _SKIP_GAP_MARKERS)


_NON_ACTIONABLE_HANDOFF_MARKERS = (
    (
        "реализационная деталь",
        re.compile(r"реализацион\w*\s+детал\w*", re.IGNORECASE),
    ),
    (
        "не является source of truth",
        re.compile(r"не\s+явля(?:ет|ются)[^\n]{0,40}source of truth", re.IGNORECASE),
    ),
    (
        "решим в реализации",
        re.compile(r"реш(?:им|ается)\s+в\s+реализац", re.IGNORECASE),
    ),
)
_SPECIFIC_TEST_MARKERS = (
    "pytest",
    ".venv/bin/pytest",
    "test_",
    "tests/",
    "unit",
    "integration",
    "smoke",
    "регресс",
    "acceptance",
    "playwright",
    "команд",
)
_SPECIFIC_EDGE_CASE_MARKERS = (
    "например",
    "timeout",
    "null",
    "none",
    "empty",
    "429",
    "500",
    "403",
    "404",
    "ошиб",
    "пуст",
    "boundary",
)
_HANDOFF_COMPONENT_MARKERS = (
    "компонент",
    "файл",
    "service",
    "handler",
    "screen",
    "module",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    "/",
)
_HANDOFF_CHANGE_MARKERS = (
    "что меняется",
    "меняем",
    "изменяем",
    "обновить",
    "добавить",
    "удалить",
    "перенести",
    "заменить",
)
_HANDOFF_VERIFICATION_MARKERS = (
    "как проверить",
    "проверить",
    "валидац",
    "acceptance",
    "smoke",
)
_HANDOFF_COMMAND_MARKERS = (
    ".venv/bin/pytest",
    "pytest",
    "playwright",
    "npm test",
    "pnpm",
    "yarn",
    "test_",
    "tests/",
    "команд",
)


def _is_blocking_runtime_degraded_mode(text: str) -> bool:
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


def _normalize_heading_title(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _extract_markdown_section_body(text: str, heading_title: str) -> str:
    lines = str(text or "").splitlines()
    target = _normalize_heading_title(heading_title)
    if not target:
        return ""
    start_idx = None
    start_level = 0
    for idx, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line)
        if not match:
            continue
        if _normalize_heading_title(match.group(2)) != target:
            continue
        start_idx = idx + 1
        start_level = len(match.group(1))
        break
    if start_idx is None:
        return ""
    body: List[str] = []
    for line in lines[start_idx:]:
        match = _MARKDOWN_HEADING_RE.match(line)
        if match and len(match.group(1)) <= start_level:
            break
        body.append(line)
    return "\n".join(body).strip()


def collect_placeholder_gaps(text: str) -> List[str]:
    gaps: List[str] = []
    seen: set[str] = set()
    in_code_block = False
    for lineno, raw_line in enumerate(str(text or "").splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue
        normalized = " ".join(stripped.split())
        lowered = normalized.casefold()
        # пропускаем строки с requires-validation / hypothesis / validation gate — это ожидаемые открытые вопросы
        if any(marker in lowered for marker in _SKIP_GAP_MARKERS):
            continue
        for marker_name, pattern in _PLACEHOLDER_MARKERS:
            if pattern.search(normalized):
                gap = f'Плейсхолдер "{marker_name}" в строке {lineno}: {normalized}'
                if gap not in seen:
                    seen.add(gap)
                    gaps.append(gap)
        if "нужно добавить тесты" in lowered and not any(marker in lowered for marker in _SPECIFIC_TEST_MARKERS):
            gap = (
                f'Неконкретная формулировка "нужно добавить тесты" в строке {lineno}: '
                f"{normalized}"
            )
            if gap not in seen:
                seen.add(gap)
                gaps.append(gap)
        if "обработать edge cases" in lowered and not any(
            marker in lowered for marker in _SPECIFIC_EDGE_CASE_MARKERS
        ):
            gap = (
                f'Неконкретная формулировка "обработать edge cases" в строке {lineno}: '
                f"{normalized}"
            )
            if gap not in seen:
                seen.add(gap)
                gaps.append(gap)
        for marker_name, pattern in _NON_ACTIONABLE_HANDOFF_MARKERS:
            if pattern.search(normalized):
                gap = (
                    f'Недостаточно конкретная handoff-формулировка "{marker_name}" '
                    f"в строке {lineno}: {normalized}"
                )
                if gap not in seen:
                    seen.add(gap)
                    gaps.append(gap)
    return gaps


def collect_implementation_handoff_gaps(text: str, *, required_sections: List[str]) -> List[str]:
    handoff_sections = [
        str(title).strip()
        for title in (required_sections or [])
        if "implementation handoff" in str(title or "").casefold()
    ]
    if not handoff_sections:
        return []
    gaps: List[str] = []
    seen: set[str] = set()
    for section_title in handoff_sections:
        body = _extract_markdown_section_body(text, section_title)
        if not body:
            continue
        lowered = body.casefold()
        body_word_count = len(re.findall(r"[0-9a-zа-яё_]+", lowered, flags=re.IGNORECASE))
        checks = (
            (
                body_word_count >= 25,
                f'Раздел "{section_title}" слишком короткий и не выглядит как исполнимый handoff.',
            ),
            (
                any(marker in lowered for marker in _HANDOFF_COMPONENT_MARKERS),
                f'Раздел "{section_title}" не перечисляет конкретные компоненты или файлы.',
            ),
            (
                any(marker in lowered for marker in _HANDOFF_CHANGE_MARKERS),
                f'Раздел "{section_title}" не объясняет, что именно меняется.',
            ),
            (
                any(marker in lowered for marker in _HANDOFF_VERIFICATION_MARKERS),
                f'Раздел "{section_title}" не описывает, как проверять изменения.',
            ),
            (
                any(marker in lowered for marker in _HANDOFF_COMMAND_MARKERS),
                f'Раздел "{section_title}" не содержит тесты или команды для запуска.',
            ),
        )
        for passed, message in checks:
            if passed or message in seen:
                continue
            seen.add(message)
            gaps.append(message)
    return gaps


def strip_model_readiness_sections(text: str) -> str:
    lines = str(text or "").splitlines()
    if not lines:
        return str(text or "")

    def _is_status_heading(value: str) -> bool:
        stripped = value.strip().lower()
        return stripped in {
            "# статус готовности",
            "## статус готовности",
            "# статус",
            "## статус",
        }

    def _is_status_verdict_line(value: str) -> bool:
        stripped = value.strip().lower()
        return stripped.startswith("статус:") or stripped in {
            "**готово к реализации.**",
            "**требует проверки перед реализацией.**",
            "**не готово к реализации.**",
        }

    first_nonempty = next((idx for idx, line in enumerate(lines) if line.strip()), None)
    if first_nonempty is None:
        return str(text or "")

    start = None
    if _is_status_heading(lines[first_nonempty]) or _is_status_verdict_line(lines[first_nonempty]):
        start = first_nonempty
    if start is None:
        return str(text or "")

    def _is_status_detail_line(value: str) -> bool:
        stripped = value.strip()
        lowered = stripped.lower()
        return (
            _is_status_heading(value)
            or _is_status_verdict_line(value)
            or lowered in {"основание:", "что ещё требует внимания:", "что еще требует внимания:"}
            or lowered.startswith("_этот статус вычислен runtime")
            or stripped.startswith("- ")
            or stripped.startswith("* ")
        )

    end = len(lines)
    for idx in range(start, len(lines)):
        if not lines[idx].strip():
            continue
        if _is_status_detail_line(lines[idx]):
            continue
        end = idx
        break

    cleaned = lines[:start] + lines[end:]
    return "\n".join(cleaned).lstrip()


def build_assessment_schema(*, is_large_spec: bool) -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["needs_rework", "issues", "missing_sections"],
        "properties": {
            "needs_rework": {"type": "boolean"},
            "issues": {"type": "array", "items": {"type": "string"}, "default": []},
            "missing_sections": {"type": "array", "items": {"type": "string"}, "default": []},
            "section_contract_gaps": {"type": "array", "items": {"type": "string"}, "default": []},
            "required_input_gaps": {"type": "array", "items": {"type": "string"}, "default": []},
            "placeholder_gaps": {"type": "array", "items": {"type": "string"}, "default": []},
            "implementation_handoff_gaps": {"type": "array", "items": {"type": "string"}, "default": []},
            "spec_to_plan_gaps": {"type": "array", "items": {"type": "string"}, "default": []},
            "weak_sections": {"type": "array", "items": {"type": "string"}, "default": []},
            "missing_counts": {"type": "array", "items": {"type": "string"}, "default": []},
            "traceability_gaps": {"type": "array", "items": {"type": "string"}, "default": []},
            "codebase_mismatches": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "unsupported_assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "unverified_claims": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "evidence_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "config_contract_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "migration_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "doc_sync_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "test_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "external_reference_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "blocking_obligations": {
                "type": "array",
                "items": {"type": "object"},
                "default": [],
            },
            "non_blocking_obligations": {
                "type": "array",
                "items": {"type": "object"},
                "default": [],
            },
        },
        "additionalProperties": True,
    }


def build_open_gaps_text(
    assessment: Dict[str, Any],
    repo_gap_labels: Optional[Dict[str, str]] = None,
) -> str:
    labels = repo_gap_labels or REPO_GAP_LABELS
    lines = ["# Open Gaps", ""]
    qc_layers = assessment.get("qc_layers") or {}
    structural_layer = qc_layers.get("structural") if isinstance(qc_layers, dict) else None
    evidence_layer = qc_layers.get("evidence") if isinstance(qc_layers, dict) else None
    if isinstance(structural_layer, dict) or isinstance(evidence_layer, dict):
        if isinstance(structural_layer, dict):
            lines.extend(["## Structural QC", ""])
            has_structural = False
            for label, items in (
                ("Issues", structural_layer.get("issues") or []),
                ("Missing Sections", structural_layer.get("missing_sections") or []),
                ("Section Contract Gaps", structural_layer.get("section_contract_gaps") or []),
                ("Required Input Gaps", structural_layer.get("required_input_gaps") or []),
                ("Placeholder Gaps", structural_layer.get("placeholder_gaps") or []),
                ("Implementation Handoff Gaps", structural_layer.get("implementation_handoff_gaps") or []),
                ("Spec-to-Plan Gaps", structural_layer.get("spec_to_plan_gaps") or []),
                ("Weak Sections", structural_layer.get("weak_sections") or []),
                ("Missing Counts", structural_layer.get("missing_counts") or []),
                ("Traceability Gaps", structural_layer.get("traceability_gaps") or []),
            ):
                if not items:
                    continue
                has_structural = True
                lines.append(f"### {label}")
                lines.append("")
                for item in items:
                    lines.append(f"- {str(item).strip()}")
                lines.append("")
            if not has_structural:
                lines.extend(["- gaps not detected", ""])
        if isinstance(evidence_layer, dict):
            lines.extend(["## Evidence QC", ""])
            has_evidence = False
            for field_name, label in labels.items():
                items = evidence_layer.get(field_name) or []
                if not items:
                    continue
                has_evidence = True
                lines.append(f"### {label}")
                lines.append("")
                for item in items:
                    lines.append(f"- {str(item).strip()}")
                lines.append("")
            if not has_evidence:
                lines.extend(["- gaps not detected", ""])
        blocking_obligations = assessment.get("blocking_obligations") or []
        if isinstance(blocking_obligations, list):
            open_blocking = [
                str(item.get("statement") or "").strip()
                for item in blocking_obligations
                if isinstance(item, dict)
                and str(item.get("statement") or "").strip()
                and str(item.get("status") or "").strip().lower() in {"open", "unverified"}
            ]
            if open_blocking:
                lines.extend(["## Blocking Obligations", ""])
                for item in open_blocking:
                    lines.append(f"- {item}")
                lines.append("")
        return "\n".join(lines).strip()
    sections = [
        ("Missing Sections", assessment.get("missing_sections") or []),
        ("Section Contract Gaps", assessment.get("section_contract_gaps") or []),
        ("Required Input Gaps", assessment.get("required_input_gaps") or []),
        ("Placeholder Gaps", assessment.get("placeholder_gaps") or []),
        ("Implementation Handoff Gaps", assessment.get("implementation_handoff_gaps") or []),
        ("Spec-to-Plan Gaps", assessment.get("spec_to_plan_gaps") or []),
    ]
    for field_name, label in labels.items():
        sections.append((label, assessment.get(field_name) or []))
    has_any = False
    for label, items in sections:
        if not items:
            continue
        has_any = True
        lines.append(f"## {label}")
        lines.append("")
        for item in items:
            lines.append(f"- {str(item).strip()}")
        lines.append("")
    if not has_any:
        lines.extend(["- gaps not detected", ""])
    return "\n".join(lines).strip()


def compute_runtime_readiness(
    assessment: Dict[str, Any],
    *,
    repo_grounded_required: bool,
    required_step_statuses: Dict[str, str],
    repo_gap_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not repo_grounded_required:
        return {"verdict": "", "blocking_reasons": [], "warning_reasons": []}
    labels = repo_gap_labels or REPO_GAP_LABELS
    missing_required = [
        step_id
        for step_id, status in required_step_statuses.items()
        if str(status or "").strip().lower() != "ok"
    ]
    blocking_repo_fields = (
        "codebase_mismatches",
        "unsupported_assumptions",
        "unverified_claims",
        "evidence_gaps",
        "config_contract_gaps",
        "migration_gaps",
        "external_reference_gaps",
    )
    blocking_reasons: List[str] = []
    warning_reasons: List[str] = []
    required_input_gaps = [str(x).strip() for x in (assessment.get("required_input_gaps") or []) if str(x).strip()]
    section_contract_gaps = [
        str(x).strip()
        for x in (assessment.get("section_contract_gaps") or [])
        if str(x).strip() and not _is_expected_open_question(x)
    ]
    placeholder_gaps = [
        str(x).strip()
        for x in (assessment.get("placeholder_gaps") or [])
        if str(x).strip() and not _is_expected_open_question(x)
    ]
    implementation_handoff_gaps = [
        str(x).strip()
        for x in (assessment.get("implementation_handoff_gaps") or [])
        if str(x).strip() and not _is_expected_open_question(x)
    ]
    spec_to_plan_gaps = [
        str(x).strip()
        for x in (assessment.get("spec_to_plan_gaps") or [])
        if str(x).strip() and not _is_expected_open_question(x)
    ]
    if bool(assessment.get("assessment_error")):
        blocking_reasons.append("Финальный QC не смог построить валидную assessment model")
    if missing_required:
        blocking_reasons.append(
            "Не выполнены обязательные repo-grounded шаги: " + ", ".join(sorted(missing_required))
        )
    if section_contract_gaps:
        blocking_reasons.append("Нарушен контракт обязательных разделов: " + "; ".join(section_contract_gaps))
    if placeholder_gaps:
        blocking_reasons.append(
            "Документ содержит placeholder или недетерминированные формулировки: "
            + "; ".join(placeholder_gaps)
        )
    if implementation_handoff_gaps:
        blocking_reasons.append(
            "Implementation handoff недостаточно конкретен: " + "; ".join(implementation_handoff_gaps)
        )
    if spec_to_plan_gaps:
        blocking_reasons.append(
            "Есть требования без конкретного способа реализации или проверки: "
            + "; ".join(spec_to_plan_gaps)
        )
    blocking_obligations_raw = assessment.get("blocking_obligations") or []
    has_obligation_model = bool(assessment.get("obligation_model_active")) or isinstance(
        assessment.get("obligation_matrix"),
        list,
    ) or (
        isinstance(blocking_obligations_raw, list) and any(isinstance(item, dict) for item in blocking_obligations_raw)
    )
    if has_obligation_model:
        open_blocking = [
            item
            for item in blocking_obligations_raw
            if isinstance(item, dict)
            and str(item.get("status") or "").strip().lower() in {"open", "unverified"}
        ]
        if open_blocking:
            blocking_reasons.append(f"Незакрытые blocking obligations: {len(open_blocking)}")
        followup_false_closures = [
            item
            for item in (assessment.get("followup_false_closures") or [])
            if (
                (isinstance(item, dict) and str(item.get("statement") or item.get("text") or "").strip())
                or str(item or "").strip()
            )
        ]
        if followup_false_closures:
            blocking_reasons.append(f"Verifier false closures: {len(followup_false_closures)}")
        retry_exhausted = [
            str(item).strip()
            for item in (assessment.get("degraded_modes") or [])
            if "retry_exhausted" in str(item or "").strip().lower()
        ]
        if retry_exhausted:
            blocking_reasons.append(f"Blocking-step retry exhausted: {len(retry_exhausted)}")
        critical_degraded = [
            str(item).strip()
            for item in (assessment.get("degraded_modes") or [])
            if any(
                marker in str(item or "").strip().lower()
                for marker in (
                    "bundle_incomplete",
                    "task_contract_missing",
                    "claim_ledger_missing",
                    "open_gaps_missing",
                    "obligation_matrix_missing",
                )
            )
        ]
        if critical_degraded:
            blocking_reasons.append(f"Критичные runtime artifacts отсутствуют: {len(critical_degraded)}")
        blocking_degraded = [
            str(item).strip()
            for item in (assessment.get("degraded_modes") or [])
            if _is_blocking_runtime_degraded_mode(item)
        ]
        if blocking_degraded and not retry_exhausted and not critical_degraded:
            blocking_reasons.append(f"Критичные degraded runtime/CLI режимы: {len(blocking_degraded)}")
        open_non_blocking = [
            item
            for item in (assessment.get("non_blocking_obligations") or [])
            if isinstance(item, dict)
            and str(item.get("status") or "").strip().lower() in {"open", "unverified"}
        ]
        if open_non_blocking:
            warning_reasons.append(f"Открытые non-blocking obligations: {len(open_non_blocking)}")
    else:
        missing_sections = [str(x).strip() for x in (assessment.get("missing_sections") or []) if str(x).strip()]
        if missing_sections:
            blocking_reasons.append("Отсутствуют обязательные разделы: " + ", ".join(missing_sections))
        if required_input_gaps:
            blocking_reasons.append("Не закрыты обязательные входы задачи: " + ", ".join(required_input_gaps))
        for field_name in blocking_repo_fields:
            items = [str(x).strip() for x in (assessment.get(field_name) or []) if str(x).strip()]
            if items:
                blocking_reasons.append(f"{labels[field_name]}: {len(items)}")
    for field_name in ("issues", "weak_sections", "missing_counts", "traceability_gaps", "doc_sync_gaps", "test_gaps"):
        label = labels.get(field_name, field_name)
        items = [str(x).strip() for x in (assessment.get(field_name) or []) if str(x).strip()]
        if items:
            warning_reasons.append(f"{label}: {len(items)}")
    degraded_modes = [str(x).strip() for x in (assessment.get("degraded_modes") or []) if str(x).strip()]
    if degraded_modes:
        warning_reasons.append(f"{labels['degraded_modes']}: {len(degraded_modes)}")
    if blocking_reasons:
        verdict = "Не готово к реализации"
    elif warning_reasons:
        verdict = "Требует проверки перед реализацией"
    else:
        verdict = "Готово к реализации"
    return {
        "verdict": verdict,
        "blocking_reasons": blocking_reasons,
        "warning_reasons": warning_reasons,
    }


def apply_runtime_readiness(
    text: str,
    assessment: Dict[str, Any],
    *,
    repo_grounded_required: bool,
    required_step_statuses: Dict[str, str],
    repo_gap_labels: Optional[Dict[str, str]] = None,
) -> str:
    # Runtime readiness is still computed and persisted separately in analyst metrics.
    # User-facing deliverables should not be prefixed with QC status boilerplate.
    return strip_model_readiness_sections(text).strip()


def runtime_readiness_allows_finalization(payload: Dict[str, Any] | None) -> bool:
    quality = dict(payload or {}) if isinstance(payload, dict) else {}
    blocking_reasons = [str(item).strip() for item in (quality.get("blocking_reasons") or []) if str(item).strip()]
    if blocking_reasons:
        return False
    verdict = str(quality.get("runtime_verdict") or quality.get("verdict") or "").strip()
    if verdict and verdict not in {"Готово к реализации", "Требует проверки перед реализацией"}:
        return False
    return True
