"""Tests for T3 i18n scaffold: localized analyst/manager templates."""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any

import yaml

# --- constants ---
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ANALYST_DIR = os.path.join(_REPO_ROOT, "modes", "analyst", "templates")
_MANAGER_DIR = os.path.join(_REPO_ROOT, "modes", "manager", "i18n")
_LANGUAGES = ("ru", "en", "zh", "de")
_PARITY_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "check_i18n_parity.py")

_MANDATORY_ANALYST_IDS = {
    "default",
    "new_spec",
    "change_spec",
    "refactor_spec",
    "integration_change_spec",
    "narrow_backend_change_spec",
    "audit",
}
_ANALYST_INVARIANT_FIELDS = (
    "output_kind",
    "compose_mode",
    "artifact_preferred",
    "target_size_hint",
    "repo_grounded_required",
    "repo_audit_required",
    "final_repo_review_required",
)
_MANAGER_PROJECT_PROMPT_KEYS = (
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
_MANAGER_SYSTEM_PROMPT_KEYS = (
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


# --- helpers ---
def _load_analyst_registry(lang: str) -> dict:
    path = os.path.join(_ANALYST_DIR, lang, "analyst_config.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw["templates"]


def _load_manager_prompts(lang: str) -> dict:
    path = os.path.join(_MANAGER_DIR, lang, "prompts.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw["prompts"]


def _load_manager_system_prompts(lang: str) -> dict:
    path = os.path.join(_MANAGER_DIR, lang, "system_prompts.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw["prompts"]


# --- analyst parity tests ---

def test_analyst_template_ids_parity() -> None:
    registries = {lang: _load_analyst_registry(lang) for lang in _LANGUAGES}
    ru_ids = set(registries["ru"].keys())
    for lang in _LANGUAGES:
        assert set(registries[lang].keys()) == ru_ids, (
            f"template_id set mismatch for lang={lang}"
        )


def test_analyst_required_sections_count_parity() -> None:
    registries = {lang: _load_analyst_registry(lang) for lang in _LANGUAGES}
    for tid in registries["ru"]:
        ru_count = len((registries["ru"][tid] or {}).get("required_sections") or [])
        for lang in _LANGUAGES:
            if lang == "ru":
                continue
            count = len((registries[lang].get(tid) or {}).get("required_sections") or [])
            assert count == ru_count, (
                f"required_sections count mismatch: template={tid} lang={lang} "
                f"has={count} ru={ru_count}"
            )


def test_analyst_required_inputs_count_parity() -> None:
    registries = {lang: _load_analyst_registry(lang) for lang in _LANGUAGES}
    for tid in registries["ru"]:
        ru_count = len((registries["ru"][tid] or {}).get("required_inputs") or [])
        for lang in _LANGUAGES:
            if lang == "ru":
                continue
            count = len((registries[lang].get(tid) or {}).get("required_inputs") or [])
            assert count == ru_count, (
                f"required_inputs count mismatch: template={tid} lang={lang} "
                f"has={count} ru={ru_count}"
            )


def test_analyst_mandatory_ids_present_all_langs() -> None:
    for lang in _LANGUAGES:
        reg = _load_analyst_registry(lang)
        for mid in _MANDATORY_ANALYST_IDS:
            assert mid in reg, f"mandatory id={mid} missing for lang={lang}"


def test_analyst_invariant_fields_unchanged() -> None:
    registries = {lang: _load_analyst_registry(lang) for lang in _LANGUAGES}
    for tid, ru_tmpl in registries["ru"].items():
        for lang in _LANGUAGES:
            if lang == "ru":
                continue
            tmpl = registries[lang].get(tid) or {}
            for field in _ANALYST_INVARIANT_FIELDS:
                if field in ru_tmpl or field in tmpl:
                    assert ru_tmpl.get(field) == tmpl.get(field), (
                        f"invariant field mismatch: template={tid} field={field} "
                        f"lang={lang} value={tmpl.get(field)!r} vs ru={ru_tmpl.get(field)!r}"
                    )


# --- analyst loader tests ---

def test_analyst_loader_selects_by_lang() -> None:
    from modes.analyst.template_service import get_analyst_templates_for_lang
    from modes.analyst.template_service import localized_templates_path

    # Verify the path resolves correctly to the lang-specific file
    en_path = localized_templates_path("en")
    assert os.path.join("templates", "en", "analyst_config.yaml") in en_path.replace("\\", "/"), (
        f"Expected en path, got: {en_path}"
    )

    # Verify data loads and is a dict with expected keys
    reg = get_analyst_templates_for_lang("en")
    assert isinstance(reg, dict)
    assert "default" in reg


def test_analyst_loader_fallback_unknown_lang() -> None:
    from modes.analyst.template_service import get_analyst_templates_for_lang

    # Unknown lang "fr" should not raise — falls back to legacy original
    reg = get_analyst_templates_for_lang("fr")
    assert isinstance(reg, dict)
    assert "default" in reg


def test_analyst_loader_fallback_missing_file(tmp_path: Any) -> None:
    """If the lang file is temporarily absent, loader falls back to legacy original."""
    from modes.analyst.template_service import get_analyst_templates_for_lang

    # Use a non-existent lang dir to force fallback
    reg = get_analyst_templates_for_lang("xx")
    assert isinstance(reg, dict)
    assert "default" in reg


# --- manager parity tests ---

def test_manager_prompt_keys_parity() -> None:
    for lang in _LANGUAGES:
        prompts = _load_manager_prompts(lang)
        for key in _MANAGER_PROJECT_PROMPT_KEYS:
            assert key in prompts, f"manager prompts.yaml lang={lang} missing key={key}"


def test_manager_system_prompt_keys_parity() -> None:
    for lang in _LANGUAGES:
        sys_prompts = _load_manager_system_prompts(lang)
        for key in _MANAGER_SYSTEM_PROMPT_KEYS:
            assert key in sys_prompts, f"manager system_prompts.yaml lang={lang} missing key={key}"


# --- manager loader tests ---

def _setup_manager_workdir(tmp_path: Any) -> str:
    """Create a tmp workdir with manager prompts.yaml in the expected location."""
    from utils.paths import cli_proxy_artifact_path
    prompt_dir = cli_proxy_artifact_path(str(tmp_path), ".manager/prompt")
    os.makedirs(prompt_dir, exist_ok=True)
    src = os.path.join(_MANAGER_DIR, "ru", "prompts.yaml")
    shutil.copy2(src, os.path.join(prompt_dir, "prompts.yaml"))
    return str(tmp_path)


def test_manager_loader_selects_by_lang(tmp_path: Any) -> None:
    from app.services.project_prompts_service import (
        load_mode_prompts,
        _default_mode_system_prompts_path_for_lang,
    )

    # Check that the lang-aware path returns the i18n file
    de_path = _default_mode_system_prompts_path_for_lang("manager", "de")
    assert os.path.join("i18n", "de", "system_prompts.yaml") in de_path.replace("\\", "/"), (
        f"Expected de i18n path, got: {de_path}"
    )

    workdir = _setup_manager_workdir(tmp_path)

    # Load with lang=de — system prompts come from i18n/de/
    result = load_mode_prompts(workdir, "manager", lang="de")
    assert isinstance(result.get("prompts"), dict)
    assert "decompose_instruction" in result["prompts"]


def test_manager_load_prompts_threads_session_language(tmp_path: Any) -> None:
    """ManagerMode._load_prompts must resolve the session's language and serve
    localized system prompts — not always Russian. Guards the lang-threading fix
    for the 5 _load_prompts call sites (all call it without an explicit lang)."""
    import types

    from modes.manager.mode import ManagerMode

    workdir = _setup_manager_workdir(tmp_path)

    def _make_config(user_lang: str) -> Any:
        return types.SimpleNamespace(
            telegram=types.SimpleNamespace(user_languages={777: user_lang}),
            defaults=types.SimpleNamespace(default_language="ru"),
        )

    session = types.SimpleNamespace(chat_id=777, workdir=workdir)

    # codebase_intro is a system-prompt key (not overridden by the project
    # prompts.yaml), so it reflects the lang-aware system prompts source.
    de_expected = _load_manager_system_prompts("de")["codebase_intro"]
    ru_expected = _load_manager_system_prompts("ru")["codebase_intro"]
    assert de_expected != ru_expected, "fixture sanity: de/ru codebase_intro should differ"

    mode = ManagerMode()
    mode.config = _make_config("de")
    de_prompts = mode._load_prompts(session=session)
    assert de_prompts["codebase_intro"] == de_expected

    mode.config = _make_config("ru")
    ru_prompts = mode._load_prompts(session=session)
    assert ru_prompts["codebase_intro"] == ru_expected


def test_manager_load_prompts_falls_back_to_ru_without_config(tmp_path: Any) -> None:
    """If language resolution fails (e.g. config is None), _load_prompts must
    fall back to Russian rather than raising."""
    import types

    from modes.manager.mode import ManagerMode

    workdir = _setup_manager_workdir(tmp_path)
    session = types.SimpleNamespace(chat_id=777, workdir=workdir)

    mode = ManagerMode()
    mode.config = None  # resolution raises -> fallback "ru"
    prompts = mode._load_prompts(session=session)
    assert prompts["codebase_intro"] == _load_manager_system_prompts("ru")["codebase_intro"]


def test_manager_loader_fallback_unknown_lang(tmp_path: Any) -> None:
    from app.services.project_prompts_service import (
        load_mode_prompts,
        _default_mode_system_prompts_path_for_lang,
    )

    # Unknown lang falls back to legacy original
    fr_path = _default_mode_system_prompts_path_for_lang("manager", "fr")
    legacy_path = os.path.join(_REPO_ROOT, "modes", "manager", "system_prompts.yaml")
    assert os.path.abspath(fr_path) == os.path.abspath(legacy_path), (
        f"Expected fallback to legacy, got: {fr_path}"
    )

    workdir = _setup_manager_workdir(tmp_path)

    result = load_mode_prompts(workdir, "manager", lang="fr")
    assert isinstance(result.get("prompts"), dict)


# --- commit_message_system test ---

def test_commit_message_system_language_directive() -> None:
    # Since en/zh/de are ru-copies, all contain the Russian directive.
    # This test just verifies the key exists in all langs.
    # Translation agents will update the directive per-language later.
    for lang in _LANGUAGES:
        sys_prompts = _load_manager_system_prompts(lang)
        assert "commit_message_system" in sys_prompts, (
            f"commit_message_system missing in lang={lang}"
        )
        value = sys_prompts["commit_message_system"]
        assert isinstance(value, str) and len(value) > 0, (
            f"commit_message_system empty in lang={lang}"
        )


# --- parity script tests ---

def test_check_i18n_parity_script_passes() -> None:
    """Parity script exits 0 for the current (all-ru) files."""
    sys.path.insert(0, _REPO_ROOT)
    from scripts.check_i18n_parity import main  # type: ignore
    rc = main([
        "--analyst-dir", _ANALYST_DIR,
        "--manager-dir", _MANAGER_DIR,
        "--languages", "ru", "en", "zh", "de",
    ])
    assert rc == 0, "check_i18n_parity.py should exit 0 with current files"


def test_check_i18n_parity_script_fails_on_mismatch(tmp_path: Any) -> None:
    """Parity script returns non-zero when analyst template_id sets differ."""
    # Copy analyst dir to tmp and add a spurious template to en only
    analyst_tmp = tmp_path / "analyst_templates"
    shutil.copytree(_ANALYST_DIR, str(analyst_tmp))

    en_config_path = analyst_tmp / "en" / "analyst_config.yaml"
    with open(str(en_config_path), encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Add a fake template that doesn't exist in other langs
    raw["templates"]["__fake_mismatch_template__"] = raw["templates"]["default"].copy()
    with open(str(en_config_path), "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)

    from scripts.check_i18n_parity import main  # type: ignore
    rc = main([
        "--analyst-dir", str(analyst_tmp),
        "--manager-dir", _MANAGER_DIR,
        "--languages", "ru", "en", "zh", "de",
    ])
    assert rc != 0, "check_i18n_parity.py should exit non-zero on template_id mismatch"
