from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Dict, Iterable, List, Tuple

import yaml

from utils.paths import cli_proxy_artifact_path

logger = logging.getLogger(__name__)

_SUPPORTED_MODE_IDS: Tuple[str, ...] = ("manager", "webmaster")
_PROMPTS_FILE = "prompts.yaml"
_SYSTEM_PROMPTS_FILE = "system_prompts.yaml"
_LEARNING_FILE = "learning.yaml"
_LEARNING_TEMPLATE: Dict[str, Any] = {
    "patches": [],
    "active_version": 1,
}
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
_WEBMASTER_PROJECT_PROMPT_KEYS: Tuple[str, ...] = (
    "system_base",
    "confirmation",
    "cli_task",
    "validation_task",
    "fix_task",
    "feedback_analysis",
    "prompt_patch",
    "prompt_compact",
)
_REQUIRED_PROJECT_PROMPT_KEYS: Dict[str, Tuple[str, ...]] = {
    "manager": _MANAGER_PROJECT_PROMPT_KEYS,
    "webmaster": _WEBMASTER_PROJECT_PROMPT_KEYS,
}
_REQUIRED_SYSTEM_PROMPT_KEYS: Dict[str, Tuple[str, ...]] = {
    "manager": _MANAGER_SYSTEM_PROMPT_KEYS,
}
_FORBIDDEN_PROJECT_PROMPT_KEYS: Dict[str, Tuple[str, ...]] = {
    "manager": _MANAGER_SYSTEM_PROMPT_KEYS,
}


class InvalidProjectPromptsError(Exception):
    """Raised when project-level mode prompts are missing or invalid."""


def ensure_project_prompts(workdir: str) -> Dict[str, Dict[str, Any]]:
    root = _normalize_workdir(workdir)
    loaded: Dict[str, Dict[str, Any]] = {}
    for mode_id in _SUPPORTED_MODE_IDS:
        try:
            prompts_path, learning_path = _mode_prompt_paths(root, mode_id)
            _ensure_mode_prompt_files(root, mode_id, prompts_path, learning_path)
            loaded[mode_id] = load_mode_prompts(root, mode_id)
            load_mode_learning(root, mode_id)
        except InvalidProjectPromptsError:
            logger.exception(
                "project prompts: ensure failed mode=%s workdir=%s",
                mode_id,
                root,
            )
            raise
        except Exception as exc:
            logger.exception(
                "project prompts: ensure failed unexpectedly mode=%s workdir=%s",
                mode_id,
                root,
            )
            raise InvalidProjectPromptsError(
                f"unexpected project prompts bootstrap failure for mode '{mode_id}'"
            ) from exc
    return loaded


def load_mode_prompts(workdir: str, mode_id: str, lang: str = "ru") -> Dict[str, Any]:
    root = _normalize_workdir(workdir)
    normalized_mode = _normalize_mode_id(mode_id)
    prompts_path, _ = _mode_prompt_paths(root, normalized_mode)
    try:
        project_payload = _read_yaml_file(
            prompts_path,
            mode_id=normalized_mode,
            file_name=_PROMPTS_FILE,
            require_mapping=True,
        )
        project_payload = _validate_prompts_payload(
            project_payload,
            mode_id=normalized_mode,
            source="project",
            file_name=_PROMPTS_FILE,
        )
        if normalized_mode != "manager":
            return project_payload

        system_prompts_path = _default_mode_system_prompts_path_for_lang(normalized_mode, lang)
        if not os.path.exists(system_prompts_path):
            raise InvalidProjectPromptsError(
                f"default system prompts template not found for mode '{normalized_mode}': {system_prompts_path}"
            )
        system_payload = _read_yaml_file(
            system_prompts_path,
            mode_id=normalized_mode,
            file_name=_SYSTEM_PROMPTS_FILE,
            require_mapping=True,
        )
        system_payload = _validate_prompts_payload(
            system_payload,
            mode_id=normalized_mode,
            source="system",
            file_name=_SYSTEM_PROMPTS_FILE,
        )
        merged = dict(system_payload.get("prompts") or {})
        merged.update(project_payload.get("prompts") or {})
        return {"prompts": merged}
    except InvalidProjectPromptsError:
        logger.exception(
            "project prompts: prompts validation failed mode=%s path=%s",
            normalized_mode,
            prompts_path,
        )
        raise


def load_mode_prompt_texts(workdir: str, mode_id: str, lang: str = "ru") -> Dict[str, str]:
    payload = load_mode_prompts(workdir, mode_id, lang)
    prompts = payload.get("prompts")
    if not isinstance(prompts, dict):
        raise InvalidProjectPromptsError(f"prompts section missing for mode '{mode_id}'")
    return {str(k): str(v) for k, v in prompts.items()}


def load_mode_learning(workdir: str, mode_id: str) -> Dict[str, Any]:
    root = _normalize_workdir(workdir)
    normalized_mode = _normalize_mode_id(mode_id)
    _, learning_path = _mode_prompt_paths(root, normalized_mode)
    try:
        payload = _read_yaml_file(
            learning_path,
            mode_id=normalized_mode,
            file_name=_LEARNING_FILE,
            require_mapping=True,
        )
        return _validate_learning_payload(payload)
    except InvalidProjectPromptsError:
        logger.exception(
            "project prompts: learning validation failed mode=%s path=%s",
            normalized_mode,
            learning_path,
        )
        raise


def save_mode_learning(workdir: str, mode_id: str, payload: Dict[str, Any]) -> None:
    root = _normalize_workdir(workdir)
    normalized_mode = _normalize_mode_id(mode_id)
    _, learning_path = _mode_prompt_paths(root, normalized_mode)
    normalized = _validate_learning_payload(payload)
    try:
        with open(learning_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(normalized, f, allow_unicode=True, sort_keys=False)
    except Exception as exc:
        logger.exception(
            "project prompts: failed to save learning yaml mode=%s path=%s",
            normalized_mode,
            learning_path,
        )
        raise InvalidProjectPromptsError(
            f"failed to save learning yaml for mode '{normalized_mode}'"
        ) from exc


def _normalize_workdir(workdir: str) -> str:
    root = str(workdir or "").strip()
    if not root:
        raise InvalidProjectPromptsError("workdir is empty")
    root_abs = os.path.abspath(root)
    if not os.path.isdir(root_abs):
        raise InvalidProjectPromptsError(f"workdir does not exist: {root_abs}")
    return root_abs


def _normalize_mode_id(mode_id: str) -> str:
    mode = str(mode_id or "").strip().lower()
    if mode not in _SUPPORTED_MODE_IDS:
        raise InvalidProjectPromptsError(f"unsupported mode_id: {mode}")
    return mode


def _mode_prompt_paths(workdir: str, mode_id: str) -> Tuple[str, str]:
    prompt_dir = cli_proxy_artifact_path(workdir, f".{mode_id}/prompt")
    return (
        os.path.join(prompt_dir, _PROMPTS_FILE),
        os.path.join(prompt_dir, _LEARNING_FILE),
    )


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _default_mode_prompts_path(mode_id: str) -> str:
    return os.path.join(_repo_root(), "modes", mode_id, _PROMPTS_FILE)


def _default_mode_system_prompts_path(mode_id: str) -> str:
    return os.path.join(_repo_root(), "modes", mode_id, _SYSTEM_PROMPTS_FILE)


def _default_mode_system_prompts_path_for_lang(mode_id: str, lang: str) -> str:
    """Path to the i18n system_prompts.yaml for mode_id/lang.

    Fallback: _default_mode_system_prompts_path(mode_id) (legacy original).
    """
    p = os.path.join(_repo_root(), "modes", mode_id, "i18n", lang, _SYSTEM_PROMPTS_FILE)
    return p if os.path.isfile(p) else _default_mode_system_prompts_path(mode_id)


def _ensure_mode_prompt_files(workdir: str, mode_id: str, prompts_path: str, learning_path: str) -> None:
    prompt_dir = os.path.dirname(prompts_path)
    try:
        os.makedirs(prompt_dir, exist_ok=True)
    except Exception as exc:
        logger.exception(
            "project prompts: failed to create prompt dir mode=%s dir=%s",
            mode_id,
            prompt_dir,
        )
        raise InvalidProjectPromptsError(
            f"failed to create prompt directory for mode '{mode_id}': {prompt_dir}"
        ) from exc

    if not os.path.exists(prompts_path):
        default_prompts_path = _default_mode_prompts_path(mode_id)
        if not os.path.exists(default_prompts_path):
            raise InvalidProjectPromptsError(
                f"default prompts template not found for mode '{mode_id}': {default_prompts_path}"
            )
        try:
            shutil.copy2(default_prompts_path, prompts_path)
        except Exception as exc:
            logger.exception(
                "project prompts: failed to copy default prompts mode=%s src=%s dst=%s",
                mode_id,
                default_prompts_path,
                prompts_path,
            )
            raise InvalidProjectPromptsError(
                f"failed to copy default prompts for mode '{mode_id}'"
            ) from exc

    if not os.path.exists(learning_path):
        try:
            with open(learning_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(_LEARNING_TEMPLATE, f, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            logger.exception(
                "project prompts: failed to create learning yaml mode=%s path=%s",
                mode_id,
                learning_path,
            )
            raise InvalidProjectPromptsError(
                f"failed to create learning yaml for mode '{mode_id}'"
            ) from exc


def _read_yaml_file(
    path: str,
    *,
    mode_id: str,
    file_name: str,
    require_mapping: bool,
) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as exc:
        logger.exception(
            "project prompts: failed to read yaml mode=%s file=%s path=%s",
            mode_id,
            file_name,
            path,
        )
        raise InvalidProjectPromptsError(
            f"failed to read {file_name} for mode '{mode_id}'"
        ) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.exception(
            "project prompts: invalid yaml mode=%s file=%s path=%s",
            mode_id,
            file_name,
            path,
        )
        raise InvalidProjectPromptsError(
            f"invalid YAML in {file_name} for mode '{mode_id}'"
        ) from exc

    if require_mapping:
        if not isinstance(data, dict):
            raise InvalidProjectPromptsError(
                f"{file_name} for mode '{mode_id}' must be a YAML mapping"
            )
        return data

    if data is None:
        return {}
    return data


def _validate_prompts_payload(
    payload: Dict[str, Any],
    *,
    mode_id: str,
    source: str,
    file_name: str,
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidProjectPromptsError(f"{file_name} for mode '{mode_id}' must be a YAML mapping")
    prompts = payload.get("prompts")
    if not isinstance(prompts, dict):
        raise InvalidProjectPromptsError(f"{file_name} for mode '{mode_id}' must contain 'prompts' mapping")

    required_keys = _required_prompt_keys(mode_id=mode_id, source=source)
    missing: List[str] = []
    invalid_type: List[str] = []
    for key in required_keys:
        value = prompts.get(key)
        if value is None:
            missing.append(key)
            continue
        if not isinstance(value, str) or not value.strip():
            invalid_type.append(key)
    if missing:
        preview = ", ".join(missing[:6])
        if len(missing) > 6:
            preview += f", ... (+{len(missing) - 6})"
        raise InvalidProjectPromptsError(
            f"{file_name} for mode '{mode_id}' is missing required keys: {preview}"
        )
    if invalid_type:
        preview = ", ".join(invalid_type[:6])
        if len(invalid_type) > 6:
            preview += f", ... (+{len(invalid_type) - 6})"
        raise InvalidProjectPromptsError(
            f"{file_name} for mode '{mode_id}' has invalid values for keys: {preview}"
        )

    if source == "project":
        forbidden_keys = _FORBIDDEN_PROJECT_PROMPT_KEYS.get(mode_id, ())
        forbidden_present = [key for key in forbidden_keys if key in prompts]
        if forbidden_present:
            preview = ", ".join(forbidden_present[:6])
            if len(forbidden_present) > 6:
                preview += f", ... (+{len(forbidden_present) - 6})"
            raise InvalidProjectPromptsError(
                f"{file_name} for mode '{mode_id}' contains system-only keys: {preview}"
            )

    normalized = dict(payload)
    normalized["prompts"] = {str(k): str(v) for k, v in prompts.items()}
    return normalized


def _required_prompt_keys(*, mode_id: str, source: str) -> Tuple[str, ...]:
    normalized_source = str(source or "").strip().lower()
    if normalized_source == "project":
        return _REQUIRED_PROJECT_PROMPT_KEYS.get(mode_id, ())
    if normalized_source == "system":
        return _REQUIRED_SYSTEM_PROMPT_KEYS.get(mode_id, ())
    raise InvalidProjectPromptsError(f"unsupported prompts source for validation: {source}")


def _normalize_rule_items(value: Any) -> List[str]:
    values: Iterable[Any]
    if isinstance(value, list):
        values = value
    elif value is None:
        values = ()
    else:
        values = (value,)

    out: List[str] = []
    for item in values:
        raw = str(item or "").replace("\r", "\n")
        for part in raw.split("\n"):
            text = part.strip().lstrip("-").strip()
            if text:
                out.append(text)
    return out


def _validate_learning_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidProjectPromptsError(f"{_LEARNING_FILE} must be a YAML mapping")

    raw_patches = payload.get("patches", [])
    if not isinstance(raw_patches, list):
        raise InvalidProjectPromptsError(f"{_LEARNING_FILE}.patches must be a YAML list")

    patches: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_patches):
        if not isinstance(item, dict):
            raise InvalidProjectPromptsError(
                f"{_LEARNING_FILE}.patches[{idx}] must be a mapping"
            )
        patches.append(
            {
                "added_rules": _normalize_rule_items(item.get("added_rules")),
                "changed_rules": _normalize_rule_items(item.get("changed_rules")),
                "removed_rules": _normalize_rule_items(item.get("removed_rules")),
                "reason": str(item.get("reason") or "").strip(),
                "expected_effect": str(item.get("expected_effect") or "").strip(),
            }
        )

    raw_version = payload.get("active_version", 1)
    try:
        active_version = int(raw_version or 1)
    except Exception as exc:
        raise InvalidProjectPromptsError(f"{_LEARNING_FILE}.active_version must be integer") from exc
    if active_version < 1:
        raise InvalidProjectPromptsError(f"{_LEARNING_FILE}.active_version must be >= 1")

    return {
        "patches": patches,
        "active_version": active_version,
    }
