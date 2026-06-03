from __future__ import annotations

from .json_normalizer import loads_safe
from .ask_user_schema import apply_ask_schema, validate_ask_payload

from config import AppConfig
from .contracts import PlanStep


def _extract_context_json_payload(context: str, key: str):
    marker = f"{key}:\n"
    if marker not in str(context or ""):
        return None
    tail = str(context or "").split(marker, 1)[1].lstrip()
    if not tail:
        return None
    first_line = tail.splitlines()[0].strip()
    if not first_line:
        return None
    try:
        return loads_safe(first_line, strict_first=True)
    except Exception:
        return None


def _is_repo_grounded_analyst_context(context: str) -> bool:
    raw = _extract_context_json_payload(context, "analyst_intent_flags")
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("requires_codebase_grounding"))


def needs_clarification(text: str, config: AppConfig, context: str = "", *, language: str = "ru") -> bool:
    if not config.defaults.clarification_enabled:
        return False
    # Repo-grounded analyst flows должны опираться на intent flags/model decisions,
    # а не на generic keyword heuristic.
    if _is_repo_grounded_analyst_context(context):
        return False
    message = (text or "").lower()
    # NOTE: "ответ пользователя:" — internal sentinel injected by orchestrator_runner.
    # NOT user-facing; intentionally kept in Russian. Do NOT i18n.
    if "ответ пользователя:" in message:
        return False
    # Use per-language keywords with legacy fallback
    by_lang = getattr(config.defaults, "clarification_keywords_by_lang", {})
    keywords = by_lang.get(language) or config.defaults.clarification_keywords
    for kw in keywords:
        if kw and kw in message:
            return True
    return False


def normalize_ask_step(step: PlanStep) -> None:
    step.parallelizable = False
    step.parallel_group = None
    question, options, _issues = apply_ask_schema(step.ask_question or "", list(step.ask_options or []))
    step.ask_question = question
    step.ask_options = options


def ask_step_validation_issues(step: PlanStep) -> list[str]:
    question = str(getattr(step, "ask_question", "") or "").strip()
    options = [str(x).strip() for x in (getattr(step, "ask_options", None) or []) if str(x).strip()]
    return validate_ask_payload(question, options)


def ask_step_needs_rebuild(step: PlanStep) -> bool:
    return bool(ask_step_validation_issues(step))
