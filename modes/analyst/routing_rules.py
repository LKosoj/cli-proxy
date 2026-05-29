from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


_GREENFIELD_SPEC_CUES = (
    "с нуля",
    "greenfield",
    "новый сервис",
    "нового сервиса",
    "новое приложение",
    "нового приложения",
    "новый продукт",
    "нового продукта",
    "новая система",
    "новую систему",
    "новой системы",
    "создать новый",
    "спроектировать новый",
    "mvp",
)
_EXISTING_SYSTEM_SPEC_CUES = (
    "доработ",
    "изменен",
    "изменить",
    "расшир",
    "улучш",
    "модифиц",
    "адаптир",
    "обнов",
    "рефактор",
    "в текущ",
    "текущ",
    "существующ",
    "в существ",
    "в проект",
    "в системе",
    "в приложени",
    "в сервис",
    "в модул",
    "в продукт",
    "по текущ",
    "по существ",
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_whole_word(text: str, cue: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(cue)}(?![\w-])", text))


def build_routing_rules_prompt_text() -> str:
    return (
        "5. Если пользователь просит ТЗ/спецификацию для новой системы, сервиса, приложения или разработки с нуля "
        "и не описывает изменение существующего решения → предпочитай template_hint=new_spec.\n"
        "6. Если пользователь просит ТЗ/спецификацию на доработку, изменение, расширение или адаптацию "
        "существующего проекта/системы → предпочитай template_hint=change_spec.\n"
        "7. Локальная доработка существующего UI/UX-компонента (header, menu, dropdown, кнопка, форма, экран, "
        "layout, локальный пользовательский сценарий) → предпочитай template_hint=ui_change_spec.\n"
        "8. Более широкая доработка существующего проекта/системы с затрагиванием нескольких подсистем, "
        "контрактов, данных или интеграций → предпочитай template_hint=change_spec.\n"
        "9. Исправление конкретного бага/регрессии в существующей системе с ожидаемым планом исправления → "
        "предпочитай template_hint=bugfix_spec.\n"
        "10. Изменение внешней интеграции, API/контракта, webhook, провайдера или обмена данными с внешней системой → "
        "предпочитай template_hint=integration_change_spec.\n"
        "11. Узкая серверная/бэкенд-доработка существующей логики, сервиса, handler, контроллера, доменной модели "
        "или внутреннего контракта без широкого product-change scope → предпочитай template_hint=narrow_backend_change_spec.\n"
        "12. Если есть project_root/workdir и запрос про анализ текущей системы → template_hint=project_analysis или audit.\n"
    )


def classify_underspecified_spec_template(
    *,
    selected_template_id: str | None = None,
    source_user_text: Any = None,
) -> Tuple[str, str]:
    selected = _norm(selected_template_id)
    if selected in {"new_spec", "change_spec"}:
        return selected, "selected_spec_template"

    user_text = _norm(source_user_text)
    if any(cue in user_text for cue in _EXISTING_SYSTEM_SPEC_CUES):
        return "change_spec", "underspecified_existing_system_spec_template"
    if any(cue in user_text for cue in _GREENFIELD_SPEC_CUES):
        return "new_spec", "underspecified_greenfield_spec_template"
    return "new_spec", "underspecified_greenfield_spec_template"


def recommend_template_for_semantics(
    *,
    selected_template_id: str | None = None,
    semantic_template_id: str | None = None,
    document_kind: str | None = None,
    change_scope: str | None = None,
    runtime_template_id: str | None = None,
    source_user_text: Any = None,
) -> Tuple[str, str]:
    selected = _norm(selected_template_id)
    semantic = _norm(semantic_template_id)
    doc_kind = _norm(document_kind)
    scope = _norm(change_scope)
    runtime_tid = _norm(runtime_template_id)
    if runtime_tid:
        return "", "runtime_override"

    can_remap = semantic in {
        "",
        "default",
        "change_spec",
        "project_analysis",
        "refactor_spec",
        "technical_debt",
        "bug_investigation",
        "integration_contract",
        "bugfix_spec",
        "integration_change_spec",
        "narrow_backend_change_spec",
    }

    if can_remap and selected != "audit" and doc_kind != "audit" and scope == "local_ui":
        return "ui_change_spec", "oversized_local_ui_template"
    if semantic in {"bugfix_spec", "bug_investigation"} and doc_kind == "spec":
        return "bugfix_spec", "bugfix_scope_template"
    if semantic in {"integration_change_spec", "integration_contract"} and doc_kind == "spec":
        return "integration_change_spec", "integration_scope_template"
    if semantic in {"narrow_backend_change_spec", "refactor_spec", "technical_debt"} and doc_kind == "spec" and scope != "local_ui":
        return "narrow_backend_change_spec", "backend_scope_template"
    if semantic in {"", "default"} and doc_kind == "audit":
        return "audit", "underspecified_audit_template"
    if semantic in {"", "default", "spec"} and doc_kind == "spec" and selected != "audit":
        if scope == "broad_change":
            return "change_spec", "underspecified_broad_change_spec_template"
        return classify_underspecified_spec_template(
            selected_template_id=selected,
            source_user_text=source_user_text,
        )
    return "", ""


_WEB_RESEARCH_CUES = (
    "исследуй",
    "исследовать",
    "исследовани",
    "research",
    "сравни",
    "сравнить",
    "сравнение",
    "проанализируй рынок",
    "анализ рынка",
    "рыночный анализ",
    "best practice",
    "лучшие практики",
    "обзор решений",
    "обзор подходов",
    "web research",
    "веб-исследован",
)

_AUDIT_CUES = (
    "аудит",
    "audit",
    "ревью кода",
    "code review",
    "проверка качества",
    "quality check",
    "проверить код",
)


def classify_profile_from_text(
    user_text: str,
    project_root: Optional[str] = None,
    workdir: Optional[str] = None,
) -> str:
    """Deterministic analysis_profile classification by cue-words.

    Returns one of: greenfield, codebase, codebase_plus_research,
    research_only, audit.
    """
    text = _norm(user_text)
    has_project = bool(
        (project_root and str(project_root).strip())
        or (workdir and str(workdir).strip())
    )

    has_greenfield = any(cue in text for cue in _GREENFIELD_SPEC_CUES)
    has_existing = any(cue in text for cue in _EXISTING_SYSTEM_SPEC_CUES)
    has_web = any(cue in text for cue in _WEB_RESEARCH_CUES)
    has_audit = any(cue in text for cue in _AUDIT_CUES)

    if has_audit and has_project:
        return "audit"

    if has_existing and has_web and has_project:
        return "codebase_plus_research"

    if has_greenfield and not has_existing:
        return "greenfield"

    if has_existing and has_project:
        return "codebase"

    if has_web and not has_existing:
        return "research_only"

    # Default
    return "codebase" if has_project else "greenfield"


# profile + document_kind → base template_id
TEMPLATE_ROUTING: Dict[Tuple[str, str], str] = {
    ("greenfield", "spec"): "new_spec",
    ("greenfield", "analysis"): "default",
    ("greenfield", "audit"): "default",
    ("codebase", "spec"): "change_spec",
    ("codebase", "analysis"): "default",
    ("codebase", "audit"): "audit",
    ("codebase_plus_research", "spec"): "change_spec",
    ("codebase_plus_research", "analysis"): "default",
    ("codebase_plus_research", "audit"): "audit",
    ("research_only", "spec"): "new_spec",
    ("research_only", "analysis"): "default",
    ("research_only", "audit"): "default",
    ("audit", "spec"): "audit",
    ("audit", "analysis"): "audit",
    ("audit", "audit"): "audit",
}

# Specialized template cue-word sets for deterministic adjustments
_BUGFIX_CUES = (
    "баг",
    "bug",
    "регрессия",
    "regression",
    "исправить",
    "fix",
    "починить",
    "сломал",
    "broken",
)
_UI_CHANGE_CUES = (
    "ui",
    "ux",
    "header",
    "menu",
    "dropdown",
    "кнопк",
    "экран",
    "layout",
    "интерфейс",
)
_UI_CHANGE_WORD_CUES = (
    "форма",
    "формы",
)
_INTEGRATION_CUES = (
    "интеграц",
    "integration",
    "api",
    "webhook",
    "провайдер",
    "provider",
    "внешн",
    "external",
    "контракт",
)
_BACKEND_NARROW_CUES = (
    "handler",
    "контроллер",
    "controller",
    "сервис",
    "service",
    "доменн",
    "domain",
    "модел",
    "model",
    "эндпоинт",
    "endpoint",
)


def build_template_from_profile(
    profile: str,
    document_kind: str,
    user_text: Optional[str] = None,
) -> str:
    """Resolve template_id from analysis_profile + document_kind.

    Applies deterministic cue-word adjustments on top of the base mapping
    (bugfix_spec, ui_change_spec, integration_change_spec,
    narrow_backend_change_spec).

    Returns the resolved template_id string.
    """
    base_key = (_norm(profile), _norm(document_kind))
    template_id = TEMPLATE_ROUTING.get(base_key, "default")

    # Deterministic adjustments apply only for spec document_kind
    # on profiles that work with existing code
    if _norm(document_kind) != "spec":
        return template_id
    if _norm(profile) not in ("codebase", "codebase_plus_research"):
        return template_id

    text = _norm(user_text) if user_text else ""
    if not text:
        return template_id

    # Priority order matters: more specific wins
    if any(cue in text for cue in _UI_CHANGE_CUES) or any(
        _contains_whole_word(text, cue) for cue in _UI_CHANGE_WORD_CUES
    ):
        return "ui_change_spec"
    if any(cue in text for cue in _BUGFIX_CUES):
        return "bugfix_spec"
    if any(cue in text for cue in _INTEGRATION_CUES):
        return "integration_change_spec"
    if any(cue in text for cue in _BACKEND_NARROW_CUES):
        return "narrow_backend_change_spec"

    return template_id
