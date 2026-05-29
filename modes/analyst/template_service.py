import logging
import os
import threading
from typing import Any, Dict, List

import yaml
from .routing_rules import recommend_template_for_semantics

logger = logging.getLogger(__name__)

# Cache by path: {path: {"mtime": float, "templates": dict}}
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_TEMPLATE_DIAGNOSTIC_MARKER = "[analyst-template-loader]"
_MANDATORY_ANALYST_TEMPLATE_IDS = {
    "default",
    "new_spec",
    "change_spec",
    "refactor_spec",
    "integration_change_spec",
    "narrow_backend_change_spec",
    "audit",
}


def default_templates_path() -> str:
    return os.path.join(os.path.dirname(__file__), "templates", "analyst_config.yaml")


def resolve_templates_path(path: str | None = None) -> str:
    explicit = str(path or "").strip()
    if explicit:
        return explicit
    env_path = str(os.getenv("ANALYST_TEMPLATES_PATH", "") or "").strip()
    if env_path:
        return env_path
    return default_templates_path()


def _normalize_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _normalize_sections(v: Any) -> List[str]:
    if not isinstance(v, list):
        return []
    out: List[str] = []
    for item in v:
        s = _normalize_str(item)
        if s:
            out.append(s)
    return out


def _normalize_optional_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    text = str(v).strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def _normalize_optional_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    text = str(v).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _normalize_protected_spec_shell(v: Any, *, required_sections: List[str]) -> Dict[str, Any]:
    payload = dict(v or {}) if isinstance(v, dict) else {}
    title = _normalize_str(payload.get("title")) or "Техническое задание"
    source_task_section = _normalize_str(payload.get("source_task_section")) or "Исходная задача"
    core_sections = _normalize_sections(payload.get("core_sections")) or list(required_sections)
    open_questions_section = (
        _normalize_str(payload.get("open_questions_section")) or "Открытые вопросы и валидационные шаги"
    )
    external_references_section = (
        _normalize_str(payload.get("external_references_section")) or "Внешние референсы и примеры реализации"
    )
    external_references_conditional = _normalize_optional_bool(payload.get("external_references_conditional"))
    return {
        "title": title,
        "source_task_section": source_task_section,
        "core_sections": core_sections,
        "open_questions_section": open_questions_section,
        "external_references_section": external_references_section,
        "external_references_conditional": True if external_references_conditional is None else external_references_conditional,
    }


def _uses_bundled_templates_path(path: str) -> bool:
    return os.path.abspath(str(path or "")) == os.path.abspath(default_templates_path())


def _invalid_template_reason(
    *,
    name: str,
    description: str,
    qa_prompt: str,
    required_sections: List[str],
) -> str:
    missing_fields: List[str] = []
    if not name:
        missing_fields.append("name")
    if not description:
        missing_fields.append("description")
    if not qa_prompt:
        missing_fields.append("qa_prompt")
    if not required_sections:
        missing_fields.append("required_sections")
    if not missing_fields:
        return ""
    return "missing_or_invalid_fields=" + ",".join(missing_fields)


def load_analyst_templates(path: str) -> Dict[str, Dict[str, Any]]:
    """
    Load Analyst templates from YAML file at `path`.

    Expected YAML structure:
      templates:
        <template_id>:
          name: str
          description: str
          required_sections: list[str]
          system_prompt_addition: str (optional)
          qa_prompt: str
          compose_mode: str (optional)
          output_kind: str (optional)
          artifact_preferred: bool (optional)
          min_user_scenarios: int (optional)
          min_functional_requirements: int (optional)
          min_nfr: int (optional)
          min_api_contracts: int (optional)
          min_acceptance_checks: int (optional)
          target_size_hint: str (optional)
          repo_grounded_required: bool (optional)
          repo_audit_required: bool (optional)
          final_repo_review_required: bool (optional)
          traceability_rules: list[str] (optional)
          required_inputs: list[str] (optional)
          protected_spec_shell: mapping (optional for spec templates; defaults are auto-filled)

    Returns a registry with required fields and any supported optional fields present in YAML.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        logger.exception("Failed to read analyst templates YAML: %s", path)
        logger.info("YAML read error: %r", e)
        raise RuntimeError(f"Failed to read analyst templates YAML: {path}") from e

    if not isinstance(raw, dict):
        logger.error("Analyst templates YAML root must be a mapping: %s", path)
        raise RuntimeError(f"Analyst templates YAML root must be a mapping: {path}")

    templates_raw = raw.get("templates")
    if not isinstance(templates_raw, dict):
        logger.error("Analyst templates YAML must have 'templates' mapping: %s", path)
        raise RuntimeError(f"Analyst templates YAML must have templates mapping: {path}")

    registry: Dict[str, Dict[str, Any]] = {}
    invalid_templates: Dict[str, str] = {}
    for template_id, tmpl_raw in templates_raw.items():
        tid = _normalize_str(template_id)
        if not tid:
            logger.warning(
                "%s invalid template skipped template_id=%r reason=empty_template_id path=%s",
                _TEMPLATE_DIAGNOSTIC_MARKER,
                template_id,
                path,
            )
            continue
        if not isinstance(tmpl_raw, dict):
            invalid_templates[tid] = "template_payload_must_be_mapping"
            logger.warning(
                "%s invalid template skipped template_id=%s reason=%s path=%s",
                _TEMPLATE_DIAGNOSTIC_MARKER,
                tid,
                invalid_templates[tid],
                path,
            )
            continue

        name = _normalize_str(tmpl_raw.get("name"))
        description = _normalize_str(tmpl_raw.get("description"))
        required_sections = _normalize_sections(tmpl_raw.get("required_sections"))
        system_prompt_addition = _normalize_str(tmpl_raw.get("system_prompt_addition"))
        qa_prompt = _normalize_str(tmpl_raw.get("qa_prompt"))
        compose_mode = _normalize_str(tmpl_raw.get("compose_mode"))
        output_kind = _normalize_str(tmpl_raw.get("output_kind"))
        artifact_preferred = _normalize_optional_bool(tmpl_raw.get("artifact_preferred"))
        min_user_scenarios = _normalize_optional_int(tmpl_raw.get("min_user_scenarios"))
        min_functional_requirements = _normalize_optional_int(tmpl_raw.get("min_functional_requirements"))
        min_nfr = _normalize_optional_int(tmpl_raw.get("min_nfr"))
        min_api_contracts = _normalize_optional_int(tmpl_raw.get("min_api_contracts"))
        min_acceptance_checks = _normalize_optional_int(tmpl_raw.get("min_acceptance_checks"))
        target_size_hint = _normalize_str(tmpl_raw.get("target_size_hint"))
        repo_grounded_required = _normalize_optional_bool(tmpl_raw.get("repo_grounded_required"))
        repo_audit_required = _normalize_optional_bool(tmpl_raw.get("repo_audit_required"))
        final_repo_review_required = _normalize_optional_bool(tmpl_raw.get("final_repo_review_required"))
        traceability_rules = _normalize_sections(tmpl_raw.get("traceability_rules"))
        required_inputs = _normalize_sections(tmpl_raw.get("required_inputs"))
        protected_spec_shell = None
        if output_kind == "spec":
            protected_spec_shell = _normalize_protected_spec_shell(
                tmpl_raw.get("protected_spec_shell"),
                required_sections=required_sections,
            )

        invalid_reason = _invalid_template_reason(
            name=name,
            description=description,
            qa_prompt=qa_prompt,
            required_sections=required_sections,
        )
        if invalid_reason:
            invalid_templates[tid] = invalid_reason
            logger.warning(
                "%s invalid template skipped template_id=%s reason=%s path=%s",
                _TEMPLATE_DIAGNOSTIC_MARKER,
                tid,
                invalid_reason,
                path,
            )
            continue

        template_entry: Dict[str, Any] = {
            "name": name,
            "description": description,
            "required_sections": required_sections,
            "system_prompt_addition": system_prompt_addition,
            "qa_prompt": qa_prompt,
        }
        if compose_mode:
            template_entry["compose_mode"] = compose_mode
        if output_kind:
            template_entry["output_kind"] = output_kind
        if artifact_preferred is not None:
            template_entry["artifact_preferred"] = artifact_preferred
        if min_user_scenarios is not None:
            template_entry["min_user_scenarios"] = min_user_scenarios
        if min_functional_requirements is not None:
            template_entry["min_functional_requirements"] = min_functional_requirements
        if min_nfr is not None:
            template_entry["min_nfr"] = min_nfr
        if min_api_contracts is not None:
            template_entry["min_api_contracts"] = min_api_contracts
        if min_acceptance_checks is not None:
            template_entry["min_acceptance_checks"] = min_acceptance_checks
        if target_size_hint:
            template_entry["target_size_hint"] = target_size_hint
        if repo_grounded_required is not None:
            template_entry["repo_grounded_required"] = repo_grounded_required
        if repo_audit_required is not None:
            template_entry["repo_audit_required"] = repo_audit_required
        if final_repo_review_required is not None:
            template_entry["final_repo_review_required"] = final_repo_review_required
        if traceability_rules:
            template_entry["traceability_rules"] = traceability_rules
        if required_inputs:
            template_entry["required_inputs"] = required_inputs
        if protected_spec_shell is not None:
            template_entry["protected_spec_shell"] = protected_spec_shell

        registry[tid] = template_entry

    missing_mandatory_templates = sorted(
        template_id for template_id in _MANDATORY_ANALYST_TEMPLATE_IDS if template_id not in registry
    )
    if _uses_bundled_templates_path(path) and missing_mandatory_templates:
        diagnostic_parts = [
            f"{template_id}:{invalid_templates.get(template_id, 'missing_template_entry')}"
            for template_id in missing_mandatory_templates
        ]
        diagnostic_text = "; ".join(diagnostic_parts)
        logger.error(
            "%s mandatory templates invalid_or_missing path=%s details=%s",
            _TEMPLATE_DIAGNOSTIC_MARKER,
            path,
            diagnostic_text,
        )
        raise RuntimeError(f"Mandatory analyst templates invalid or missing: {diagnostic_text}")

    if not registry:
        logger.error("No valid analyst templates found in YAML: %s", path)
        raise RuntimeError(f"No valid analyst templates found in YAML: {path}")

    return registry


def get_analyst_templates_cached(path: str | None = None) -> Dict[str, Dict[str, Any]]:
    """
    Cached loader with hot-reload by file mtime.
    """
    path = resolve_templates_path(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    with _CACHE_LOCK:
        entry = _CACHE.get(path)
        if entry is not None and entry.get("mtime") == mtime:
            return entry["templates"]

    templates = load_analyst_templates(path)

    with _CACHE_LOCK:
        _CACHE[path] = {"mtime": mtime, "templates": templates}

    return templates


def resolve_template_id_from_session(session: Any) -> str:
    """
    Resolve analyst template_id from session.

    This function intentionally does not validate existence in registry; use resolve_template().
    """
    try:
        raw = getattr(getattr(session, "modes", None), "analyst_template_id", getattr(session, "analyst_template_id", None))
    except Exception:
        raw = None
    tid = _normalize_str(raw)
    return tid or "default"


def resolve_effective_template_id(
    templates_registry: Any,
    *,
    runtime_template_id: str | None = None,
    intent_template_id: str | None = None,
    session: Any = None,
    session_template_id: str | None = None,
    default_template_id: str = "default",
) -> str:
    """
    Resolve effective template id by priority:
    runtime override -> intent -> session -> default.

    Only template ids present in the loaded registry are considered valid.
    Empty or unknown candidates are skipped.
    """
    reg = templates_registry if isinstance(templates_registry, dict) else {}

    session_tid = _normalize_str(session_template_id)
    if not session_tid and session is not None:
        try:
            raw = getattr(getattr(session, "modes", None), "analyst_template_id", getattr(session, "analyst_template_id", None))
        except Exception:
            raw = None
        session_tid = _normalize_str(raw)

    candidates = [
        _normalize_str(runtime_template_id),
        _normalize_str(intent_template_id),
        session_tid,
        _normalize_str(default_template_id) or "default",
    ]
    for tid in candidates:
        if tid and tid in reg and isinstance(reg.get(tid), dict):
            return tid

    raise RuntimeError("Analyst effective template not found in registry")


def assess_template_fitness(
    *,
    selected_template_id: str | None = None,
    intent_template_id: str | None = None,
    effective_template_id: str | None = None,
    document_kind: str | None = None,
    change_scope: str | None = None,
    runtime_template_id: str | None = None,
    source_user_text: Any = None,
) -> Dict[str, Any]:
    selected = _normalize_str(selected_template_id)
    intent_tid = _normalize_str(intent_template_id)
    effective = _normalize_str(effective_template_id)
    doc_kind = _normalize_str(document_kind)
    scope = _normalize_str(change_scope)
    runtime_tid = _normalize_str(runtime_template_id)
    if runtime_tid:
        return {
            "status": "forced_runtime_override",
            "applicable": False,
            "reason": "runtime_override",
            "expected_template_id": effective,
            "suggested_template_id": "",
        }
    expected, reason = recommend_template_for_semantics(
        selected_template_id=selected,
        semantic_template_id=intent_tid,
        document_kind=doc_kind,
        change_scope=scope,
        runtime_template_id=runtime_tid,
        source_user_text=source_user_text,
    )
    if not expected:
        return {
            "status": "not_applicable",
            "applicable": False,
            "reason": "",
            "expected_template_id": "",
            "suggested_template_id": "",
        }
    return {
        "status": "ok" if effective == expected else "needs_adjustment",
        "applicable": True,
        "reason": reason,
        "expected_template_id": expected,
        "suggested_template_id": expected if effective != expected else "",
    }


def resolve_template(
    templates_registry: Any,
    template_id: str,
    *,
    return_id: bool = False,
) -> Any:
    """
    Resolve a template dict from registry with strict in-registry resolution.
    """
    def _with_id(tid_local: str, tmpl: Any) -> Any:
        if not isinstance(tmpl, dict):
            return tmpl
        out = dict(tmpl)
        # Non-breaking: callers may ignore this metadata.
        out.setdefault("_id", tid_local)
        return out

    reg = templates_registry if isinstance(templates_registry, dict) else {}
    tid = _normalize_str(template_id)
    if tid and tid in reg and isinstance(reg.get(tid), dict):
        return (tid, _with_id(tid, reg[tid])) if return_id else _with_id(tid, reg[tid])
    # Legacy fallback resolution is intentionally removed: unknown template id is a hard error.
    raise RuntimeError(f"Analyst template not found in registry: {tid or '(empty)'}")


def get_effective_template(
    templates_registry: Any,
    *,
    runtime_template_id: str | None = None,
    intent_template_id: str | None = None,
    session: Any = None,
    session_template_id: str | None = None,
    default_template_id: str = "default",
    return_id: bool = False,
) -> Any:
    """
    Resolve the effective template dict by priority:
    runtime override -> intent -> session -> default.
    """
    template_id = resolve_effective_template_id(
        templates_registry,
        runtime_template_id=runtime_template_id,
        intent_template_id=intent_template_id,
        session=session,
        session_template_id=session_template_id,
        default_template_id=default_template_id,
    )
    return resolve_template(templates_registry, template_id, return_id=return_id)


def get_template_for_session(
    session: Any,
    *,
    templates_path: str | None = None,
) -> Dict[str, Any]:
    """
    Single point to load + resolve the active template for a given session.
    """
    registry = get_analyst_templates_cached(templates_path)
    template_id = resolve_template_id_from_session(session)
    return resolve_template(registry, template_id)
