from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from modes.sdk.runtime import openai_client
from modes.sdk.runtime.json_normalizer import loads_safe
from agent.plugins.base import ToolPlugin
from modes.sdk.runtime.tooling.spec import ToolSpec
from modes.analyst.routing_rules import build_routing_rules_prompt_text

logger = logging.getLogger(__name__)


def _load_repo_structure(repo_root: str) -> str:
    """Load repo structure: ARCHITECTURE.md if available, else directory tree."""
    if not repo_root:
        return ""
    arch_path = os.path.join(repo_root, ".cli-proxy", ".codebase_map", "ARCHITECTURE.md")
    try:
        with open(arch_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        if text:
            return text
    except (OSError, IOError):
        pass
    # Fallback: walk directory tree (dirs only, skip hidden/generated)
    _SKIP = frozenset({
        "__pycache__", "node_modules", ".git", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".eggs", "*.egg-info",
    })
    lines: list[str] = []
    for dirpath, dirnames, _files in os.walk(repo_root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in _SKIP and not d.endswith(".egg-info")
        )
        rel = os.path.relpath(dirpath, repo_root)
        if rel == ".":
            continue
        depth = rel.count(os.sep)
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(dirpath)}/")
    if not lines:
        return ""
    return "Структура каталогов проекта:\n" + "\n".join(lines)


SUPPORTED_TEMPLATE_IDS = {
    "default",
    "new_spec",
    "audit",
    "change_spec",
    "ui_change_spec",
    "bugfix_spec",
    "integration_change_spec",
    "narrow_backend_change_spec",
    "project_analysis",
    "bug_investigation",
    "incident_rca",
    "impact_analysis",
    "refactor_spec",
    "migration_plan",
    "integration_contract",
    "security_review",
    "performance_analysis",
    "test_strategy",
    "release_readiness",
    "technical_debt",
}

SUPPORTED_ANALYSIS_PROFILES = {
    "greenfield",
    "codebase",
    "codebase_plus_research",
    "research_only",
    "audit",
}

REMOVED_INTENT_FIELDS = {
    "task_type",
    "template_id",
    "confidence",
    "reason",
    "needs_cli",
    "needs_clarification",
    "requires_codebase_grounding",
    "requires_repo_audit",
    "requires_final_repo_review",
    "change_scope",
    "clarification_is_blocking",
    "clarification_topic",
    "clarification_question",
    "clarification_options",
    "focus_paths",
}


def _normalize_list(values: Any, *, limit: int = 6) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for item in values:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out[:limit]


def _normalize_template_id(value: Any, *, fallback: str) -> str:
    template_id = str(value or "").strip().lower() or fallback
    if template_id not in SUPPORTED_TEMPLATE_IDS:
        return fallback
    return template_id


def _normalize_analysis_profile(value: Any, *, fallback: str) -> str:
    analysis_profile = str(value or "").strip().lower()
    fallback_value = str(fallback or "").strip().lower()
    if analysis_profile not in SUPPORTED_ANALYSIS_PROFILES:
        return fallback_value if fallback_value in SUPPORTED_ANALYSIS_PROFILES else ""
    return analysis_profile


def _normalize_detail_level(value: Any, *, fallback: str) -> str:
    detail_level = str(value or "").strip().lower()
    fallback_value = str(fallback or "").strip().lower()
    if detail_level not in {"brief", "standard", "full"}:
        return fallback_value if fallback_value in {"brief", "standard", "full"} else ""
    return detail_level


def _normalize_document_kind(value: Any, *, fallback: str) -> str:
    document_kind = str(value or "").strip().lower()
    fallback_value = str(fallback or "").strip().lower()
    if document_kind not in {"analysis", "spec", "audit"}:
        return fallback_value if fallback_value in {"analysis", "spec", "audit"} else ""
    return document_kind


class AnalystIntentPluginTool(ToolPlugin):
    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name="analyst_intent_plugin",
            description=(
                "Classify analyst request into the schema-first Analyst intent contract."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_text": {"type": "string", "description": "Original user request"},
                    "clarification_answers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Already collected clarification answers from the user",
                    },
                    "selected_template": {
                        "type": "string",
                        "description": "User-selected analyst template id (optional)",
                    },
                    "project_root": {
                        "type": "string",
                        "description": "Connected project root path (optional)",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Current session workdir path (optional)",
                    },
                    "has_codebase_map": {
                        "type": "boolean",
                        "description": (
                            "True when .cli-proxy/.codebase_map/ artifacts are available "
                            "for the project (architecture, structure, integrations, etc.)"
                        ),
                    },
                },
                "required": ["user_text"],
            },
            parallelizable=False,
        )

    async def execute(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        user_text = str(args.get("user_text") or "").strip()
        if not user_text:
            return {"success": False, "error": "user_text required"}

        config = getattr(self, "config", None)
        defaults = getattr(config, "defaults", None) if config is not None else None
        if not defaults or not getattr(defaults, "openai_api_key", None) or not getattr(defaults, "openai_model", None):
            return {"success": False, "error": "analyst_intent_plugin requires configured OpenAI credentials"}

        selected_template = _normalize_template_id(args.get("selected_template"), fallback="default")
        project_root = str(args.get("project_root") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        clarification_answers = _normalize_list(args.get("clarification_answers"), limit=8)
        repo_structure = _load_repo_structure(project_root or workdir)
        has_codebase_map = bool(args.get("has_codebase_map")) or bool(repo_structure)

        system = (
            "<role>\n"
            "Ты — классификатор задач режима Аналитик. "
            "Все semantic decisions принимает модель, а не runtime.\n"
            "</role>\n\n"

            "<output_format>\n"
            "Верни ТОЛЬКО JSON-объект без markdown-обёртки.\n"
            "Схема:\n"
            "{\n"
            '  "analysis_profile": "greenfield|codebase|codebase_plus_research|research_only|audit",\n'
            '  "document_kind": "spec|analysis|audit",\n'
            '  "detail_level": "brief|standard|full",\n'
            '  "summary": "краткое понимание задачи в 1-2 предложениях",\n'
            '  "clarification_questions": ["..."],\n'
            '  "template_hint": "default|new_spec|change_spec|ui_change_spec|bugfix_spec|'
            'integration_change_spec|narrow_backend_change_spec|project_analysis|bug_investigation|audit"\n'
            "}\n"
            "</output_format>\n\n"

            "<rules>\n"
            "КРИТИЧНЫЕ (нарушение = невалидный результат):\n"
            "1. Обязательные поля: analysis_profile, document_kind, detail_level, summary.\n"
            "2. Не возвращай task_type, template_id, confidence, requires_* или другие поля старого контракта.\n"
            "3. Если без ответа пользователя задача неоднозначна, верни clarification_questions (1-3 вопроса).\n\n"

            "ЗАПРЕТЫ НА УТОЧНЯЮЩИЕ ВОПРОСЫ (clarification_questions):\n"
            "Q1. НЕ задавай вопросов, ответ на которые уже явно присутствует в user_text или "
            "clarification_answers. Перед каждым вопросом мысленно проверь: «пользователь уже сказал это?». "
            "Если да — вопрос не нужен.\n"
            "Q2. Если has_codebase_map=true или предоставлен repo_structure, НЕ задавай вопросов, "
            "ответ на которые можно получить, прочитав карту кодовой базы или сами файлы проекта. "
            "Такие вопросы относятся к внутренним фактам репозитория (существует ли компонент X, "
            "какие файлы затрагивает доработка, какой внутренний формат используется, есть ли заготовка, "
            "какие CLI/адаптеры уже поддержаны, совместимо ли с текущим поведением, нужен ли "
            "reverse-engineering). Эти факты выясняются самостоятельным анализом репозитория, а не у пользователя.\n"
            "Q3. Задавай ТОЛЬКО те вопросы, ответ на которые знает только пользователь: цели, приоритеты, "
            "ограничения бизнеса/времени, ожидаемое поведение в неоднозначных местах, выбор между "
            "несколькими допустимыми альтернативами, нефункциональные требования.\n"
            "Q4. Если ни один такой вопрос не нужен — верни пустой список clarification_questions.\n\n"

            "МАРШРУТИЗАЦИЯ:\n"
            "4. Если пользователь явно просит ТЗ/спецификацию/техническое задание → document_kind=spec. "
            "Не ставь template_hint=default, если запрос не просит именно краткий общий анализ.\n"
            f"{build_routing_rules_prompt_text()}\n"

            "КАЛИБРОВКА:\n"
            "12. analysis_profile=codebase, если задача про существующий проект/репозиторий/кодовую базу.\n"
            "13. analysis_profile=greenfield ТОЛЬКО для новой разработки без опоры на существующий проект. "
            "Если проект существует — это НЕ greenfield, даже если в тексте есть слова «новая функция», «новый модуль», «создать».\n"
            "13a. Для любого ТЗ (spec) про существующую кодовую базу с requires_codebase_grounding=true "
            "→ template_hint=change_spec. template_hint=narrow_backend_change_spec ставится ТОЛЬКО если: "
            "(a) задача явно про внутреннюю логику одного сервиса/handler/доменной модели, "
            "(b) НЕ затрагивает межмодульные контракты, API, форматы данных, transfer, CLI-интеграции, "
            "(c) в user_text нет слов «доработка», «расширение», «перенос», «интеграция», «CLI», «сессия».\n"
            "14. analysis_profile=audit и document_kind=audit для аудита/ревью текущего проекта.\n"
            "15. detail_level=full для подробного ТЗ; standard для обычного анализа; brief для краткого обзора.\n"
            "16. summary — коротко, по делу.\n"
            "17. Если clarification_answers уже переданы, используй их как уже полученные ответы пользователя "
            "и не переспрашивай тот же аспект повторно без новой причины.\n"
            "</rules>"
        )
        user_payload: Dict[str, Any] = {
            "user_text": user_text,
            "clarification_answers": clarification_answers,
            "selected_template": selected_template,
            "project_root": project_root,
            "workdir": workdir,
            "has_codebase_map": has_codebase_map,
        }
        if repo_structure:
            user_payload["repo_structure"] = repo_structure
        user = json.dumps(user_payload, ensure_ascii=False)

        big_model = (getattr(defaults, "openai_big_model", None) or "").strip()
        model_override = big_model or None

        try:
            out = await openai_client.chat_completion(
                config,
                system,
                user,
                response_format={"type": "json_object"},
                model=model_override,
            )
            parsed = loads_safe(out, strict_first=False)
        except Exception:
            logger.exception("analyst_intent_plugin LLM classification failed")
            return {"success": False, "error": "analyst_intent_plugin classification failed"}

        if not isinstance(parsed, dict):
            return {"success": False, "error": "analyst_intent_plugin response is not a JSON object"}

        analysis_profile = _normalize_analysis_profile(parsed.get("analysis_profile"), fallback="")
        document_kind = _normalize_document_kind(parsed.get("document_kind"), fallback="")
        detail_level = _normalize_detail_level(parsed.get("detail_level"), fallback="")
        summary = str(parsed.get("summary") or "").strip()
        template_hint = _normalize_template_id(parsed.get("template_hint"), fallback="")
        clarification_questions = _normalize_list(parsed.get("clarification_questions"), limit=3)

        removed_fields = sorted(field for field in REMOVED_INTENT_FIELDS if field in parsed)
        if removed_fields:
            return {
                "success": False,
                "error": "analyst_intent_plugin invalid output: removed fields " + ", ".join(removed_fields),
            }

        invalid_contract_fields: List[str] = []
        if not analysis_profile:
            invalid_contract_fields.append("analysis_profile")
        if not document_kind:
            invalid_contract_fields.append("document_kind")
        if not detail_level:
            invalid_contract_fields.append("detail_level")
        if not summary:
            invalid_contract_fields.append("summary")

        if invalid_contract_fields:
            return {
                "success": False,
                "error": (
                    "analyst_intent_plugin invalid output: missing/invalid "
                    + ", ".join(sorted(set(invalid_contract_fields)))
                ),
            }

        result: Dict[str, Any] = {
            "analysis_profile": analysis_profile,
            "detail_level": detail_level,
            "document_kind": document_kind,
            "summary": summary,
        }
        if clarification_questions:
            result["clarification_questions"] = clarification_questions
        if template_hint:
            result["template_hint"] = template_hint
        return {"success": True, "output": json.dumps(result, ensure_ascii=False)}
