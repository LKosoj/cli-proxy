#!/usr/bin/env python3
"""i18n parity checker for analyst templates and manager prompts.

Usage:
    python scripts/check_i18n_parity.py \
        --analyst-dir modes/analyst/templates \
        --manager-dir modes/manager/i18n \
        --languages ru en zh de \
        [--lint-purity]

Returns exit code 0 if all checks pass, non-zero otherwise.
Purity lint (--lint-purity) prints warnings but does not fail.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Set, Tuple

import yaml

_MANDATORY_ANALYST_TEMPLATE_IDS: Set[str] = {
    "default",
    "new_spec",
    "change_spec",
    "refactor_spec",
    "integration_change_spec",
    "narrow_backend_change_spec",
    "audit",
}

_ANALYST_INVARIANT_FIELDS: Tuple[str, ...] = (
    "output_kind",
    "compose_mode",
    "artifact_preferred",
    "target_size_hint",
    "repo_grounded_required",
    "repo_audit_required",
    "final_repo_review_required",
)

_ANALYST_TRANSLATABLE_FIELDS: Tuple[str, ...] = (
    "name",
    "description",
    "system_prompt_addition",
    "qa_prompt",
    "traceability_rules",
)

_MANAGER_PROJECT_PROMPT_KEYS: Tuple[str, ...] = (
    "decompose_instruction",
    "decompose_normalize_system",
    "plan_validation_system",
    "plan_fix_minimal_instruction",
    "plan_fix_system",
    "dev_instruction_template",
    "dev_rework_instruction_template",
    "review_instruction_template",
    "review_normalize_system",
    "decision_system",
    "plan_reconcile_system",
)

_MANAGER_SYSTEM_PROMPT_KEYS: Tuple[str, ...] = (
    "resume_header_paused",
    "resume_header_active",
    "resume_question_template",
    "run_pipeline_codebase_tail",
    "decompose_retry_message",
    "codebase_intro",
    "codebase_stack",
    "codebase_architecture",
    "codebase_structure",
    "codebase_integrations",
    "codebase_conventions",
    "codebase_testing",
    "codebase_concerns",
    "codebase_outro",
    "invariant_policy",
    "commit_message_system",
    "final_report_system",
    "final_spec_audit_task",
    "final_spec_audit_retry_task",
    "manager_prompt_patch_system",
    "manager_prompt_compact_system",
    "work_type_classifier_system",
)


def _load_yaml(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_analyst(analyst_dir: str, lang: str) -> Dict[str, Any]:
    path = os.path.join(analyst_dir, lang, "analyst_config.yaml")
    raw = _load_yaml(path)
    templates = raw.get("templates") if isinstance(raw, dict) else None
    if not isinstance(templates, dict):
        raise ValueError(f"No 'templates' mapping in {path}")
    return templates


def _load_manager_prompts(manager_dir: str, lang: str) -> Dict[str, Any]:
    path = os.path.join(manager_dir, lang, "prompts.yaml")
    raw = _load_yaml(path)
    return raw.get("prompts") if isinstance(raw, dict) else {}


def _load_manager_system_prompts(manager_dir: str, lang: str) -> Dict[str, Any]:
    path = os.path.join(manager_dir, lang, "system_prompts.yaml")
    raw = _load_yaml(path)
    return raw.get("prompts") if isinstance(raw, dict) else {}


def _check_yaml_validity(analyst_dir: str, manager_dir: str, languages: List[str]) -> List[str]:
    errors: List[str] = []
    for lang in languages:
        for path in [
            os.path.join(analyst_dir, lang, "analyst_config.yaml"),
            os.path.join(manager_dir, lang, "prompts.yaml"),
            os.path.join(manager_dir, lang, "system_prompts.yaml"),
        ]:
            try:
                _load_yaml(path)
            except Exception as exc:
                errors.append(f"YAML parse error {path}: {exc}")
    return errors


def _check_analyst_parity(analyst_dir: str, languages: List[str]) -> List[str]:
    errors: List[str] = []
    registries: Dict[str, Dict[str, Any]] = {}
    for lang in languages:
        try:
            registries[lang] = _load_analyst(analyst_dir, lang)
        except Exception as exc:
            errors.append(f"analyst load error lang={lang}: {exc}")

    if not registries:
        return errors

    ru_ids = set(registries.get("ru", {}).keys())

    # 1. Template ID parity
    for lang, reg in registries.items():
        ids = set(reg.keys())
        if ids != ru_ids:
            extra = ids - ru_ids
            missing = ru_ids - ids
            if extra:
                errors.append(f"analyst template_id parity: lang={lang} has extra ids={sorted(extra)}")
            if missing:
                errors.append(f"analyst template_id parity: lang={lang} missing ids={sorted(missing)}")

    # 2. required_sections count parity
    for tid in ru_ids:
        ru_count = len((registries["ru"].get(tid) or {}).get("required_sections") or [])
        for lang, reg in registries.items():
            if lang == "ru":
                continue
            count = len((reg.get(tid) or {}).get("required_sections") or [])
            if count != ru_count:
                errors.append(
                    f"analyst required_sections count: template={tid} lang={lang} "
                    f"has {count} vs ru={ru_count}"
                )

    # 3. required_inputs count parity
    for tid in ru_ids:
        ru_count = len((registries["ru"].get(tid) or {}).get("required_inputs") or [])
        for lang, reg in registries.items():
            if lang == "ru":
                continue
            count = len((reg.get(tid) or {}).get("required_inputs") or [])
            if count != ru_count:
                errors.append(
                    f"analyst required_inputs count: template={tid} lang={lang} "
                    f"has {count} vs ru={ru_count}"
                )

    # 4. Mandatory IDs present
    for lang, reg in registries.items():
        for mandatory_id in _MANDATORY_ANALYST_TEMPLATE_IDS:
            if mandatory_id not in reg:
                errors.append(f"analyst mandatory id missing: lang={lang} id={mandatory_id}")

    # 5. Invariant fields match ru
    for tid in ru_ids:
        ru_tmpl = registries["ru"].get(tid) or {}
        for lang, reg in registries.items():
            if lang == "ru":
                continue
            tmpl = reg.get(tid) or {}
            for field in _ANALYST_INVARIANT_FIELDS:
                if field in ru_tmpl or field in tmpl:
                    if ru_tmpl.get(field) != tmpl.get(field):
                        errors.append(
                            f"analyst invariant field mismatch: template={tid} field={field} "
                            f"lang={lang} value={tmpl.get(field)!r} vs ru={ru_tmpl.get(field)!r}"
                        )

    return errors


def _check_manager_parity(manager_dir: str, languages: List[str]) -> List[str]:
    errors: List[str] = []

    for lang in languages:
        # prompts.yaml keys
        try:
            prompts = _load_manager_prompts(manager_dir, lang)
            if not isinstance(prompts, dict):
                errors.append(f"manager prompts.yaml lang={lang}: 'prompts' key missing or not a mapping")
            else:
                for key in _MANAGER_PROJECT_PROMPT_KEYS:
                    if key not in prompts:
                        errors.append(f"manager prompts.yaml lang={lang}: missing key={key}")
        except Exception as exc:
            errors.append(f"manager prompts load error lang={lang}: {exc}")

        # system_prompts.yaml keys
        try:
            sys_prompts = _load_manager_system_prompts(manager_dir, lang)
            if not isinstance(sys_prompts, dict):
                errors.append(f"manager system_prompts.yaml lang={lang}: 'prompts' key missing or not a mapping")
            else:
                for key in _MANAGER_SYSTEM_PROMPT_KEYS:
                    if key not in sys_prompts:
                        errors.append(f"manager system_prompts.yaml lang={lang}: missing key={key}")
        except Exception as exc:
            errors.append(f"manager system_prompts load error lang={lang}: {exc}")

    return errors


def _purity_warning(lang: str, text: str) -> str | None:
    """Detect untranslated content in a target-language field.

    The source language is Russian, so the dominant — and reliably detectable —
    failure mode is Russian prose leaking through untranslated. Character-ratio
    metrics (ASCII-word ratio, CJK coverage) are unreliable here: these prompt
    fields legitimately embed large amounts of must-not-translate Latin (file
    paths, JSON schema literals, ``{placeholders}``, code identifiers, enum
    values), so a low non-Latin ratio is not evidence of a translation defect.
    We therefore flag only residual Cyrillic, which has effectively no false
    positives for non-ru targets.
    """
    cyrillic = len(re.findall(r"[Ѐ-ӿ]", text))
    if cyrillic > 0:
        return f"residual Cyrillic ({cyrillic} chars) — likely untranslated Russian"
    return None


def _lint_purity_analyst(analyst_dir: str, languages: List[str]) -> None:
    for lang in (lng for lng in languages if lng != "ru"):
        try:
            reg = _load_analyst(analyst_dir, lang)
        except Exception as exc:
            print(f"  [purity] analyst load error lang={lang}: {exc}", file=sys.stderr)
            continue
        for tid, tmpl in reg.items():
            for field in _ANALYST_TRANSLATABLE_FIELDS:
                value = tmpl.get(field)
                if not value:
                    continue
                text = " ".join(value) if isinstance(value, list) else str(value)
                warning = _purity_warning(lang, text)
                if warning:
                    print(
                        f"  [purity warning] analyst lang={lang} template={tid} field={field}: {warning}"
                    )


def _lint_purity_manager(manager_dir: str, languages: List[str]) -> None:
    for lang in (lng for lng in languages if lng != "ru"):
        for file_type, loader, keys in [
            ("prompts", _load_manager_prompts, _MANAGER_PROJECT_PROMPT_KEYS),
            ("system_prompts", _load_manager_system_prompts, _MANAGER_SYSTEM_PROMPT_KEYS),
        ]:
            try:
                prompts = loader(manager_dir, lang)
            except Exception as exc:
                print(f"  [purity] manager {file_type} load error lang={lang}: {exc}", file=sys.stderr)
                continue
            if not isinstance(prompts, dict):
                continue
            for key in keys:
                value = prompts.get(key)
                if not value:
                    continue
                warning = _purity_warning(lang, str(value))
                if warning:
                    print(
                        f"  [purity warning] manager lang={lang} file={file_type} key={key}: {warning}"
                    )


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check i18n parity for analyst/manager templates.")
    parser.add_argument("--analyst-dir", required=True, help="Path to analyst templates dir (contains lang subdirs)")
    parser.add_argument("--manager-dir", required=True, help="Path to manager i18n dir (contains lang subdirs)")
    parser.add_argument("--languages", nargs="+", default=["ru", "en", "zh", "de"], help="Language codes to check")
    parser.add_argument("--lint-purity", action="store_true", help="Run script-aware purity lint (warnings only)")
    args = parser.parse_args(argv)

    errors: List[str] = []

    # 7. YAML validity
    errors.extend(_check_yaml_validity(args.analyst_dir, args.manager_dir, args.languages))

    # 1-5. Analyst parity
    errors.extend(_check_analyst_parity(args.analyst_dir, args.languages))

    # 6. Manager key parity
    errors.extend(_check_manager_parity(args.manager_dir, args.languages))

    # Purity lint (non-blocking)
    if args.lint_purity:
        print("[purity lint] running...")
        _lint_purity_analyst(args.analyst_dir, list(args.languages))
        _lint_purity_manager(args.manager_dir, list(args.languages))

    if errors:
        print("i18n parity check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print("i18n parity check PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
