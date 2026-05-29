from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, List

from .ask_user_schema import is_non_semantic_ask_answer
from .ask_user_generation import ASK_USER_CLARIFICATION_SYSTEM, build_validated_ask_payload
from .contracts import PlanStep
from .heuristics import (
    ask_step_needs_rebuild,
    ask_step_validation_issues,
    needs_clarification,
    normalize_ask_step,
)
from .json_normalizer import loads_safe
from .openai_client import chat_completion
from config import AppConfig

_log = logging.getLogger(__name__)
_EXTERNAL_REFERENCE_URL_RE = re.compile(r"https?://[^\s<>()\"']+")


_PLANNER_SYSTEM = """Ты — оркестратор. Построй план шагов для выполнения задачи пользователя.
Верни строго JSON со структурой:
{
  "steps": [
    {
      "id": "step1",
      "title": "...",
      "instruction": "...",
      "step_type": "task",
      "parallel_group": null,
      "depends_on": [],
      "parallelizable": false,
      "parallelizable_reason": null,
      "ask_question": null,
      "ask_options": null
    }
  ]
}
Правила:
- Не общайся с пользователем напрямую.
- Не добавляй шаги вида "сформировать итоговый ответ пользователю"/"написать финальный ответ": финальный user-facing
  ответ формирует сам оркестратор отдельным шагом после выполнения плана.
- Если executor_profile=analyst или в контексте есть analyst_intent_flags, весь план должен оставаться analysis-only:
  разрешены только анализ, аудит, сравнение, сбор evidence, подготовка ТЗ/отчета и финальная сверка.
  Не планируй реализацию, редактирование файлов, применение патчей, миграции или любые change-execution шаги.
- Для repo-grounded analyst/use_cli/synthesis шагов разрешены только подтвержденные факты из файлов,
  конфигов, логов и наблюдаемого поведения. Если evidence нет, формулируй "не подтверждено" или
  "требует отдельной проверки". Не планируй шаги, которые подталкивают исполнителя к гипотезам
  или достраиванию картины по аналогии.
- Если нужно уточнение, добавь шаг с step_type="ask_user" и заполни ask_question + ask_options (минимум 2 варианта).
- План должен доводить работу до состояния, в котором оркестратор сможет завершить run без пустой заглушки.
- Если обязательные входы задачи не закрыты, либо запроси их через ask_user,
  либо спланируй сбор недостающего evidence до готовности финализации.
- Шаги должны быть исполнимы исполнителем с инструментами.
- НЕ указывай, какие инструменты использовать исполнителю. Он сам выбирает.
  Исключение: step_type может быть "ask_user" или "use_cli" (это служебные типы шагов).
  Если step_type="use_cli", то instruction ДОЛЖЕН быть готовым текстом задания для CLI (task_text).
- Параллельность потенциально опасна (гонки по файлам/ресурсам). По умолчанию параллельность выключена.
- Если хочешь запустить шаги параллельно, ОБЯЗАТЕЛЬНО:
  - явно выставь parallelizable=true для каждого шага, который можно исполнять параллельно,
  - объясни почему это безопасно в parallelizable_reason,
  - при необходимости задай parallel_group (одинаковый gid для шагов, которые можно запустить вместе).
- Если есть зависимости, укажи depends_on как список id шагов, которые должны завершиться ДО этого шага.
- Если параллельность не нужна, оставляй parallel_group=null и parallelizable=false.

Политика use_cli по режимам:
- В контексте может быть строка executor_profile=....
- В контексте может быть блок analyst_intent_flags (JSON) с полями requires_repo_audit / requires_final_repo_review.
- Если analyst_intent_flags.needs_clarification=true и пользователь ещё не ответил строкой "Ответ пользователя: ...",
  план должен содержать step_type="ask_user".
- Если в analyst_intent_flags уже переданы clarification_question + clarification_options, переиспользуй их, а не придумывай новый вопрос.
- Если analyst_intent_flags.required_inputs переданы, уточняй один самый важный обязательный вход из этого списка,
  а не общий meta-вопрос.
- Если в user_message уже есть строки "Ответ пользователя: ...", считай, что уточнение уже дано, и используй ответ
  в плане вместо повторного ask_user, если нет действительно нового блокирующего пробела.
- Если analyst_intent_flags.clarification_topic задан, используй его как secondary hint, а не как единственный источник вопроса.
- Если requires_codebase_grounding=true и при этом не требуются отдельные repo_audit/final_review шаги,
  план должен содержать step_type="use_cli" для базового repo-grounded анализа с id="use_cli_repo_grounding".
- Если requires_repo_audit=true, план должен содержать step_type="use_cli" для начального аудита репозитория с id="use_cli_repo_audit".
- Если requires_final_repo_review=true, план должен содержать step_type="use_cli"
  для финального second-opinion review с id="use_cli_repo_final_review".
- В analyst-контексте любой step_type="ask_user" считается blocking:
  такой шаг должен идти раньше любых task/use_cli шагов, а остальные шаги должны зависеть от ответа на него.
- Эти шаги должны быть repo-grounded: instruction должен явно ссылаться на код/репозиторий в project_root/workdir из контекста.
- В analyst-контексте step_type="use_cli" используй только как управляемый read-only под-процесс для анализа/аудита/сверки.
  Формулируй instruction так, чтобы CLI анализировал и собирал evidence, а не внедрял изменения.
- Если executor_profile НЕ равен analyst и analyst_intent_flags отсутствует,
  избегай step_type="use_cli". Добавляй его только когда без полноценного
  CLI-workflow нельзя.

Дополнительное правило стабильности:
- В контексте может присутствовать блок prior_steps (JSON-массив объектов с полями id/title/step_type/status).
  Если ты планируешь шаг, который по смыслу совпадает с одним из prior_steps (тот же step_type и почти тот же title),
  ОБЯЗАТЕЛЬНО используй тот же id, чтобы оркестратор не переисполнял уже выполненное.
"""

_ASK_CLARIFICATION_SYSTEM = ASK_USER_CLARIFICATION_SYSTEM


async def plan_steps(config: AppConfig, user_message: str, context: str) -> List[PlanStep]:
    _log.info("planner: start, user_message=%r context_len=%d", user_message[:200], len(context))
    deterministic_steps = _build_deterministic_analyst_plan(user_message, context)
    if deterministic_steps:
        _log.info(
            "planner: using deterministic analyst plan, %d step(s): %s",
            len(deterministic_steps),
            ", ".join(f"{s.id}({s.step_type}:{s.title})" for s in deterministic_steps),
        )
        return deterministic_steps
    raw = await chat_completion(
        config,
        _PLANNER_SYSTEM,
        f"Контекст:\n{context}\n\nЗапрос пользователя:\n{user_message}",
        response_format={"type": "json_object"},
    )
    analyst_flags = _extract_analyst_intent_flags(context)
    if not raw:
        _log.warning("planner: LLM returned empty response, using fallback single step")
        steps = _fallback_steps(user_message, context)
        _ensure_flagged_use_cli_steps(context, steps)
        _ensure_external_reference_research_step(user_message, context, steps)
        _enforce_blocking_clarification_gate(context, steps)
        _ensure_unique_step_ids(steps)
        return steps
    _log.info("planner: LLM response received, %d chars", len(raw))
    try:
        payload = loads_safe(raw, strict_first=False)
        steps_raw = payload.get("steps", [])
    except Exception as exc:
        _log.warning("planner: JSON parse error: %s, raw=%r", exc, raw[:300])
        steps = _fallback_steps(user_message, context)
        _ensure_flagged_use_cli_steps(context, steps)
        _ensure_external_reference_research_step(user_message, context, steps)
        _enforce_blocking_clarification_gate(context, steps)
        _ensure_unique_step_ids(steps)
        return steps
    steps: List[PlanStep] = []
    if not isinstance(steps_raw, list):
        steps_raw = []
    for idx, item in enumerate(steps_raw, start=1):
        if not isinstance(item, dict):
            continue
        step_id = item.get("id") or f"step{idx}"
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list):
            depends_on = []
        step = PlanStep(
            id=step_id,
            title=item.get("title") or f"Шаг {idx}",
            instruction=item.get("instruction") or user_message,
            step_type=item.get("step_type") or "task",
            parallel_group=item.get("parallel_group"),
            depends_on=[str(x) for x in depends_on if x],
            parallelizable=bool(item.get("parallelizable") or False),
            parallelizable_reason=item.get("parallelizable_reason"),
            ask_question=item.get("ask_question"),
            ask_options=item.get("ask_options"),
        )
        if step.step_type == "ask_user":
            normalize_ask_step(step)
        steps.append(step)
    if not steps:
        _log.warning("planner: no valid steps parsed, using fallback single step")
        steps = _fallback_steps(user_message, context)
    if not any(s.step_type == "ask_user" for s in steps) and needs_clarification(user_message, config, context):
        _log.info("planner: adding clarification step (ask_user) via LLM")
        ask_step = await _build_clarification_step(config, user_message, context)
        normalize_ask_step(ask_step)
        steps.insert(0, ask_step)
    clarification_answers = _extract_clarification_answers(user_message, context)
    if (
        analyst_flags.get("needs_clarification")
        and not clarification_answers
        and not any(s.step_type == "ask_user" for s in steps)
    ):
        _log.info("planner: adding clarification step (ask_user) from analyst_intent_flags")
        ask_step = await _build_clarification_step(config, user_message, context)
        normalize_ask_step(ask_step)
        steps.insert(0, ask_step)
    steps = await _repair_ask_steps(config, user_message, context, steps)
    if analyst_flags and any(s.step_type == "ask_user" for s in steps):
        _log.info(
            "planner: analyst ask_user is blocking and must be answered before the run can complete"
        )
    _ensure_flagged_use_cli_steps(context, steps)
    _ensure_external_reference_research_step(user_message, context, steps)
    _enforce_blocking_clarification_gate(context, steps)
    _ensure_unique_step_ids(steps)
    _log.info("planner: finished, %d step(s): %s", len(steps),
              ", ".join(f"{s.id}({s.step_type}:{s.title})" for s in steps))
    return steps


def _build_deterministic_analyst_plan(user_message: str, context: str) -> List[PlanStep]:
    flags = _extract_analyst_intent_flags(context)
    executor_profile = _extract_context_value(context, "executor_profile").strip().lower()
    if executor_profile != "analyst" and not flags:
        return []
    if flags.get("needs_clarification") and not _extract_clarification_answers(user_message, context):
        return []

    document_kind = str(flags.get("document_kind") or "").strip().lower()
    template_id = str(flags.get("template_id") or "").strip().lower()
    external_references = _extract_external_reference_urls(user_message, context)
    requires_repo_flow = bool(
        flags.get("requires_codebase_grounding")
        or flags.get("requires_repo_audit")
        or flags.get("requires_final_repo_review")
        or external_references
    )
    if document_kind != "spec" or not requires_repo_flow:
        return []
    if template_id not in {"change_spec", "integration_change_spec", "narrow_backend_change_spec"}:
        return []

    steps: List[PlanStep] = [
        PlanStep(
            id="synthesize_final_tz",
            title="Синтез финального ТЗ",
            instruction=(
                "На базе уже собранного repo-grounded evidence по репозиторию и отдельного design-input анализа "
                "собери финальное ТЗ для реализации задачи пользователя (исходный запрос и уточнения приложены ниже).\n\n"
                "Структурные требования к ТЗ:\n"
                "- отделить подтвержденные факты репозитория от implementation proposal;\n"
                "- зафиксировать current state, gap analysis и целевое поведение;\n"
                "- перечислить реально затронутые модули, контракты, форматы данных и точки интеграции;\n"
                "- раскрыть влияние на смежные существующие реализации/сценарии, чтобы регрессии были явно учтены;\n"
                "- включить требования к тестам, обратной совместимости, рискам и критериям приёмки;\n"
                "- сделать документ пригодным для передачи low-middle разработчику в работу.\n\n"
                "ОБЯЗАТЕЛЬНО ВСТРОИТЬ В ТЕЛО ТЗ (не обтекаемо пересказывать, а цитировать):\n"
                "- Полные сигнатуры ключевых существующих классов/структур/датаклассов, от которых зависит "
                "новый код, цитатой из репозитория с путём и номером строки (например, `path/to/file.py:42`). "
                "Достаточно 3-8 строк кода на каждый такой якорь.\n"
                "- Для каждой новой функции/класса/метода — точная сигнатура (имя, параметры с типами, тип "
                "возвращаемого значения) и 2-4 строки псевдокода о внутренней логике. Нельзя описывать только прозой.\n"
                "- Для каждого нового результата (dataclass / TypedDict / Pydantic) — готовая декларация полей с типами.\n"
                "- Если задача затрагивает структурированные данные (JSONL/JSON/SQL/YAML/proto/etc.), привести "
                "минимум один реальный пример каждой ключевой формы — из fixture репозитория, из design-input "
                "analysis, либо явно помеченный как hypothesis, подлежащий validation gate.\n"
                "- Для точек расширения (registry, dispatcher, branch по target) — цитата существующего кода плюс "
                "конкретный diff-namerение (что именно добавить/заменить), а не формулировка «добавить Codex в registry».\n"
                "- Для каждого acceptance criterion из контракта — явно указать targeted test файл/функцию либо "
                "новую, которую нужно создать.\n"
                "- Разделы «requires-validation» должны содержать конкретный artifact (фикстура, smoke-команда, "
                "flag) и acceptance signal, а не только слова «нужна проверка».\n"
                "- Для writer-компонентов (reader/writer сессий, materialization) ВСЕГДА описывать "
                "atomic write pattern: запись в temp file + atomic rename. Это обязательно и для production-кода, "
                "и для тестов. Тесты должны писать во injectable base_dir (например, через параметр или fixture), "
                "НИКОГДА не в реальный ~/.codex или реальный HOME пользователя. Явно указать это в секции "
                "«Implementation handoff» каждого writer-компонента.\n"
                "- Имена разделов ТЗ должны быть УНИКАЛЬНЫМИ. Если в ТЗ уже есть раздел «Открытые вопросы» — "
                "не создавать второй с тем же или синонимичным названием. Проверить весь документ перед "
                "финализацией и смержить дубликаты."
            ),
            step_type="use_cli",
            parallel_group=None,
            depends_on=[],
            parallelizable=False,
            parallelizable_reason=None,
            ask_question=None,
            ask_options=None,
        ),
        PlanStep(
            id="validate_tz_completeness",
            title="Валидация полноты и трассируемости ТЗ",
            instruction=(
                "Проверь финальное ТЗ на полноту, repo-grounded трассируемость и пригодность к передаче в разработку.\n\n"
                "Стандартные проверки:\n"
                "- все ключевые требования пользователя покрыты явно;\n"
                "- неподтвержденные вещи помечены как hypothesis / requires-validation, а не как факты;\n"
                "- затронутые модули, формат данных, тесты, риски и acceptance criteria не пропущены.\n\n"
                "Обязательно проверить наличие embedded evidence и пометить `Critical` если отсутствует:\n"
                "- цитаты существующих dataclass'ов/классов/сигнатур с путями и номерами строк (не просто ссылка «см. файл X»);\n"
                "- точные сигнатуры новых функций/классов (имя, параметры с типами, возврат) — не только прозой;\n"
                "- декларации полей для новых dataclass/TypedDict результатов;\n"
                "- для каждого acceptance criterion — конкретный test target (файл/функция);\n"
                "- если задача касается структурированных данных — хотя бы один реальный или помеченный hypothesis пример каждой формы.\n\n"
                "Требования к requires-validation секциям — это ОТДЕЛЬНЫЕ ожидаемые открытые вопросы, а НЕ дефекты:\n"
                "- Если для requires-validation нет конкретного artifact/command/signal — это acceptable, "
                "просто помечай section как requiring-validation.\n"
                "- НЕ помечай requires-validation как Critical и НЕ включай их в verdict «не готово».\n"
                "- «готово к реализации» означает: все embedded evidence присутствует, ключевые контракты зафиксированы, "
                "а requires-validation sections есть как ожидаемые вопросы для developer-led validation.\n"
                "- «не готово» = отсутствует embedded evidence из раздела выше, или ключевой контракт не определён вообще.\n\n"
                "В итоге: перечисли точечные корректировки. Если документ пригоден к передаче — прямо скажи «Готово к реализации». "
                "Не помечай requires-validation sections как Critical."
            ),
            step_type="use_cli",
            parallel_group=None,
            depends_on=["synthesize_final_tz"],
            parallelizable=False,
            parallelizable_reason=None,
            ask_question=None,
            ask_options=None,
        ),
    ]
    _ensure_flagged_use_cli_steps(context, steps)
    _ensure_external_reference_research_step(user_message, context, steps)
    _wire_deterministic_analyst_spec_dependencies(steps)
    _enforce_blocking_clarification_gate(context, steps)
    _ensure_unique_step_ids(steps)
    return steps


def _fallback_steps(user_message: str, context: str) -> List[PlanStep]:
    flags = _extract_analyst_intent_flags(context)
    executor_profile = _extract_context_value(context, "executor_profile").strip().lower()
    if not flags and executor_profile != "analyst":
        return [PlanStep(id="step1", title="Выполнить задачу", instruction=user_message)]

    document_kind = str(flags.get("document_kind") or "").strip().lower()
    requires_repo_grounding = bool(
        flags.get("requires_codebase_grounding")
        or flags.get("requires_repo_audit")
        or flags.get("requires_final_repo_review")
    )
    focus_paths = [str(item).strip() for item in (flags.get("focus_paths") or []) if str(item).strip()]

    if document_kind == "audit":
        title = "Подготовить repo-grounded аудит"
        instruction = (
            "Собери подтвержденные наблюдения и риски по запросу пользователя. "
            "Опирайся только на доступный контекст, реальные файлы, конфиги, тесты, логи и внешние референсы, "
            "которые явно присутствуют во входных данных."
        )
    elif document_kind == "analysis":
        title = "Подготовить repo-grounded анализ"
        instruction = (
            "Собери подтвержденные факты, ограничения и выводы по запросу пользователя. "
            "Не достраивай картину по аналогии: если evidence нет, явно помечай пробелы."
        )
    else:
        title = "Подготовить repo-grounded ТЗ"
        instruction = (
            "Собери подтвержденные требования, текущую реализацию, затронутые поверхности и ограничения. "
            "На этой базе подготовь детализированное ТЗ, пригодное для передачи в разработку."
        )

    if requires_repo_grounding:
        instruction += " Используй repo-grounded evidence из текущего проекта как основной источник истины."
    if focus_paths:
        instruction += "\n\nПриоритетные пути анализа:\n" + "\n".join(f"- {path}" for path in focus_paths)

    return [PlanStep(id="step1", title=title, instruction=instruction)]


async def _build_clarification_step(config: AppConfig, user_message: str, context: str) -> PlanStep:
    analyst_flags = _extract_analyst_intent_flags(context)
    clarification_topic = str(analyst_flags.get("clarification_topic") or "").strip()
    clarification_question = str(analyst_flags.get("clarification_question") or "").strip()
    clarification_options = _normalize_string_list(analyst_flags.get("clarification_options"), limit=4)
    required_inputs = _normalize_string_list(analyst_flags.get("required_inputs"), limit=8)
    template_id = str(analyst_flags.get("template_id") or "").strip()
    clarification_answers = _extract_clarification_answers(user_message, context)

    prompt_parts = [
        f"Запрос пользователя:\n{user_message}",
        f"Шаблон analyst:\n{template_id or '(не определён)'}",
        f"Приоритетная тема уточнения:\n{clarification_topic or '(не задана)'}",
        "Обязательные входные данные:",
    ]
    if required_inputs:
        prompt_parts.extend(f"- {item}" for item in required_inputs)
    else:
        prompt_parts.append("- (не заданы)")
    prompt_parts.extend(
        [
            "",
            f"Уже полученные ответы пользователя:\n{json.dumps(clarification_answers, ensure_ascii=False)}",
            f"Seed question:\n{clarification_question or '(не задан)'}",
            f"Seed options:\n{json.dumps(clarification_options, ensure_ascii=False)}",
        ]
    )
    user_prompt = "\n".join(prompt_parts)

    ask_question, ask_options = await build_validated_ask_payload(
        config,
        user_prompt=user_prompt,
        system_prompt=_ASK_CLARIFICATION_SYSTEM,
        chat_completion_fn=chat_completion,
        log=_log,
        log_prefix="planner",
    )

    return PlanStep(
        id="ask_user_1",
        title="Уточнение запроса",
        instruction="Запросить уточнение у пользователя",
        step_type="ask_user",
        ask_question=ask_question,
        ask_options=ask_options[:4],
    )


async def _repair_ask_steps(
    config: AppConfig,
    user_message: str,
    context: str,
    steps: List[PlanStep],
) -> List[PlanStep]:
    ask_positions = [idx for idx, step in enumerate(steps) if step.step_type == "ask_user"]
    if not ask_positions:
        return steps

    ask_steps = [steps[idx] for idx in ask_positions]
    needs_rebuild = len(ask_steps) > 1 or any(ask_step_needs_rebuild(step) for step in ask_steps)
    if not needs_rebuild:
        for step in ask_steps:
            normalize_ask_step(step)
        return steps

    repair_reason, repair_issues = _ask_repair_details(ask_steps)
    _log.warning(
        "planner: rebuilding ask_user block (count=%d, reason=%s, issues=%s)",
        len(ask_steps), repair_reason, repair_issues,
    )
    rebuilt = await _build_clarification_step(config, user_message, context)
    first_ask = ask_steps[0]
    rebuilt.id = str(first_ask.id or rebuilt.id)
    rebuilt.title = str(first_ask.title or rebuilt.title)
    normalize_ask_step(rebuilt)
    replaced_ids = {str(step.id or "").strip() for step in ask_steps if str(step.id or "").strip()}

    repaired: List[PlanStep] = []
    inserted = False
    for idx, step in enumerate(steps):
        if idx in ask_positions:
            if not inserted:
                repaired.append(rebuilt)
                inserted = True
            continue
        if getattr(step, "depends_on", None):
            normalized_depends_on: List[str] = []
            for dep in step.depends_on:
                dep_id = str(dep or "").strip()
                if not dep_id:
                    continue
                normalized_depends_on.append(rebuilt.id if dep_id in replaced_ids else dep_id)
            step.depends_on = list(dict.fromkeys(normalized_depends_on))
        repaired.append(step)
    return repaired


def _ask_repair_details(ask_steps: List[PlanStep]) -> tuple[str, str]:
    issues = sorted({
        issue
        for step in ask_steps
        for issue in ask_step_validation_issues(step)
    })
    reasons: List[str] = []
    if len(ask_steps) > 1:
        reasons.append("multiple_ask_user_steps")
    if issues:
        reasons.append("invalid_ask_user_payload")
    return "+".join(reasons) or "ask_step_rebuild_requested", ",".join(issues) or "-"


def _ensure_unique_step_ids(steps: List[PlanStep]) -> None:
    seen: set[str] = set()
    for idx, step in enumerate(steps, start=1):
        base_id = step.id or f"step{idx}"
        candidate = base_id
        if candidate in seen:
            candidate = f"{base_id}_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        while candidate in seen:
            candidate = f"{base_id}_{uuid.uuid4().hex[:6]}"
        step.id = candidate
        seen.add(candidate)


def _extract_context_value(context: str, key: str) -> str:
    if not context or not key:
        return ""
    prefix = f"{key}="
    for line in str(context).splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _extract_context_json_payload(context: str, key: str) -> Any:
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
    except Exception as exc:
        _log.warning("planner: failed to parse context JSON block %s: %s", key, exc)
        return None


def _extract_analyst_intent_flags(context: str) -> dict[str, Any]:
    raw = _extract_context_json_payload(context, "analyst_intent_flags")
    if not isinstance(raw, dict):
        return {}
    needs_clarification = bool(raw.get("needs_clarification"))
    return {
        "needs_clarification": needs_clarification,
        "clarification_is_blocking": bool(needs_clarification or raw.get("clarification_is_blocking")),
        "clarification_topic": str(raw.get("clarification_topic") or "").strip(),
        "clarification_question": str(raw.get("clarification_question") or "").strip(),
        "clarification_options": _normalize_string_list(raw.get("clarification_options"), limit=4),
        "template_id": str(raw.get("template_id") or "").strip(),
        "required_inputs": _normalize_string_list(raw.get("required_inputs"), limit=8),
        "document_kind": str(raw.get("document_kind") or "").strip().lower(),
        "requires_codebase_grounding": bool(raw.get("requires_codebase_grounding")),
        "requires_repo_audit": bool(raw.get("requires_repo_audit")),
        "requires_final_repo_review": bool(raw.get("requires_final_repo_review")),
        "focus_paths": _normalize_string_list(raw.get("focus_paths"), limit=5),
    }


def _normalize_string_list(value: Any, *, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        out.append(text)
    return out[:limit]


def _extract_clarification_answers(user_message: str, context: str) -> List[str]:
    answers: List[str] = []
    for line in str(user_message or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("Ответ пользователя:"):
            continue
        answer = stripped.split(":", 1)[1].strip()
        if answer and not is_non_semantic_ask_answer(answer) and answer not in answers:
            answers.append(answer)
    raw = _extract_context_json_payload(context, "clarification_answers")
    if isinstance(raw, list):
        for item in raw:
            text = str(item or "").strip()
            if text and not is_non_semantic_ask_answer(text) and text not in answers:
                answers.append(text)
    return answers


def _extract_prior_step_ids(context: str) -> set[str]:
    raw = _extract_context_json_payload(context, "prior_steps")
    if not isinstance(raw, list):
        return set()
    step_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or "").strip()
        if step_id:
            step_ids.add(step_id)
    return step_ids


def _extract_valid_prior_repo_use_cli_step_ids(context: str) -> set[str]:
    raw = _extract_context_json_payload(context, "prior_steps")
    if not isinstance(raw, list):
        return set()
    valid_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or "").strip()
        step_type = str(item.get("step_type") or "").strip()
        step_status = str(item.get("status") or "").strip().lower()
        if (
            step_id in {"use_cli_repo_grounding", "use_cli_repo_audit", "use_cli_repo_final_review"}
            and step_type == "use_cli"
            and step_status == "ok"
        ):
            valid_ids.add(step_id)
    return valid_ids


def _extract_external_reference_urls(user_message: str, context: str) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()
    texts: List[str] = [str(user_message or "")]
    texts.extend(_extract_clarification_answers(user_message, context))
    for text in texts:
        for match in _EXTERNAL_REFERENCE_URL_RE.findall(str(text or "")):
            url = str(match or "").rstrip(".,);]").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def _extract_valid_prior_external_reference_step_ids(context: str) -> set[str]:
    raw = _extract_context_json_payload(context, "prior_steps")
    if not isinstance(raw, list):
        return set()
    valid_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or "").strip()
        if not step_id:
            continue
        step_status = str(item.get("status") or "").strip().lower()
        title = str(item.get("title") or "").strip().lower()
        instruction = str(item.get("instruction") or "").strip().lower()
        if step_status != "ok":
            continue
        if step_id == "analyze_external_reference":
            valid_ids.add(step_id)
            continue
        if (
            "внеш" in f"{title}\n{instruction}"
            and ("референ" in f"{title}\n{instruction}" or "reference" in f"{title}\n{instruction}")
        ):
            valid_ids.add(step_id)
    return valid_ids


def _insert_after_leading_ask_user(steps: List[PlanStep], step: PlanStep) -> None:
    insert_at = 0
    while insert_at < len(steps) and steps[insert_at].step_type == "ask_user":
        insert_at += 1
    steps.insert(insert_at, step)


def _is_valid_repo_use_cli_step(step: PlanStep, *, step_id: str, root: str) -> bool:
    if str(getattr(step, "id", "") or "").strip() != step_id:
        return False
    if str(getattr(step, "step_type", "") or "").strip() != "use_cli":
        return False
    instruction = str(getattr(step, "instruction", "") or "")
    return bool(root) and root in instruction


def _step_covers_external_reference(step: PlanStep, reference_urls: List[str]) -> bool:
    title = str(getattr(step, "title", "") or "").strip()
    instruction = str(getattr(step, "instruction", "") or "").strip()
    haystack = f"{title}\n{instruction}".lower()
    if any(str(url).strip().lower() in haystack for url in reference_urls if str(url).strip()):
        return True
    return "внеш" in haystack and ("референ" in haystack or "reference" in haystack)


def _insert_after_repo_grounding_prefix(steps: List[PlanStep], step: PlanStep) -> None:
    insert_at = 0
    reserved_prefix_ids = {"use_cli_repo_grounding", "use_cli_repo_audit"}
    while insert_at < len(steps):
        current = steps[insert_at]
        current_id = str(getattr(current, "id", "") or "").strip()
        if current.step_type == "ask_user" or current_id in reserved_prefix_ids:
            insert_at += 1
            continue
        break
    steps.insert(insert_at, step)


def _wire_deterministic_analyst_spec_dependencies(steps: List[PlanStep]) -> None:
    existing_ids = {str(getattr(step, "id", "") or "").strip() for step in steps}
    synth_deps = [
        step_id
        for step_id in ("use_cli_repo_grounding", "use_cli_repo_audit", "analyze_external_reference")
        if step_id in existing_ids
    ]
    for step in steps:
        step_id = str(getattr(step, "id", "") or "").strip()
        if step_id == "synthesize_final_tz":
            step.depends_on = list(synth_deps)
        elif step_id == "validate_tz_completeness":
            step.depends_on = ["synthesize_final_tz"]


def _ensure_flagged_use_cli_steps(context: str, steps: List[PlanStep]) -> None:
    if not steps:
        return
    flags = _extract_analyst_intent_flags(context)
    requires_codebase_grounding = bool(flags.get("requires_codebase_grounding"))
    requires_repo_audit = bool(flags.get("requires_repo_audit"))
    requires_final_repo_review = bool(flags.get("requires_final_repo_review"))
    focus_paths = flags.get("focus_paths") or []
    requires_repo_grounding = bool(
        requires_codebase_grounding and not requires_repo_audit and not requires_final_repo_review
    )
    if not requires_repo_grounding and not requires_repo_audit and not requires_final_repo_review:
        return
    project_root = _extract_context_value(context, "project_root")
    workdir = _extract_context_value(context, "workdir")
    root = project_root or workdir or "текущую рабочую директорию сессии"
    focus_hint = ""
    if focus_paths:
        focus_hint = (
            "\n\nПриоритетные зоны анализа (начни с них, не анализируй весь репозиторий):\n"
            + "\n".join(f"- {p}" for p in focus_paths)
        )
    existing_ids = {str(step.id or "").strip() for step in steps if str(step.id or "").strip()}
    prior_ids = _extract_valid_prior_repo_use_cli_step_ids(context)

    def _remove_invalid_reserved_step(step_id: str) -> None:
        for idx, step in enumerate(list(steps)):
            if str(step.id or "").strip() != step_id:
                continue
            if _is_valid_repo_use_cli_step(step, step_id=step_id, root=root):
                return
            del steps[idx]
            existing_ids.discard(step_id)
            return

    if requires_repo_grounding:
        _remove_invalid_reserved_step("use_cli_repo_grounding")
    if requires_repo_audit:
        _remove_invalid_reserved_step("use_cli_repo_audit")
    if requires_final_repo_review:
        _remove_invalid_reserved_step("use_cli_repo_final_review")

    if (
        requires_repo_grounding
        and "use_cli_repo_grounding" not in existing_ids
        and "use_cli_repo_grounding" not in prior_ids
    ):
        grounding_task_text = (
            "Сделай базовый repo-grounded анализ репозитория через CLI в директории:\n"
            f"{root}\n\n"
            "Нужно:\n"
            "- быстро подтвердить по реальным файлам структуру проекта, стек и ключевые подсистемы\n"
            "- выявить кодовые и конфигурационные зоны, которые реально относятся к запросу пользователя\n"
            "- зафиксировать краткие repo-grounded выводы без выдумывания сущностей, API или поведения\n"
            "- подготовить сжатую опору для следующих аналитических шагов"
            f"{focus_hint}"
        )
        _insert_after_leading_ask_user(
            steps,
            PlanStep(
                id="use_cli_repo_grounding",
                title="Базовый repo-grounded анализ через CLI",
                instruction=grounding_task_text,
                step_type="use_cli",
                parallel_group=None,
                depends_on=[],
                parallelizable=False,
                parallelizable_reason=None,
                ask_question=None,
                ask_options=None,
            ),
        )
        existing_ids.add("use_cli_repo_grounding")

    if requires_repo_audit and "use_cli_repo_audit" not in existing_ids and "use_cli_repo_audit" not in prior_ids:
        audit_task_text = (
            "Сделай начальный аудит репозитория через CLI в директории:\n"
            f"{root}\n\n"
            "Нужно:\n"
            "- быстро понять структуру проекта и ключевые подсистемы\n"
            "- выявить архитектурные, тестовые, конфигурационные и интеграционные риски\n"
            "- перечислить только реально подтвержденные затронутые зоны "
            "(config, docs, tests, модули, интеграции и другие артефакты, если они подтверждены)\n"
            "- подготовить краткий repo-grounded отчёт с next steps без выдумывания сущностей или API"
            f"{focus_hint}"
        )
        _insert_after_leading_ask_user(
            steps,
            PlanStep(
                id="use_cli_repo_audit",
                title="Начальный аудит репозитория через CLI",
                instruction=audit_task_text,
                step_type="use_cli",
                parallel_group=None,
                depends_on=[],
                parallelizable=False,
                parallelizable_reason=None,
                ask_question=None,
                ask_options=None,
            ),
        )
        existing_ids.add("use_cli_repo_audit")

    if (
        requires_final_repo_review
        and "use_cli_repo_final_review" not in existing_ids
        and "use_cli_repo_final_review" not in prior_ids
    ):
        # Depend only on other repo-grounded steps, not the entire plan.
        # By the time final review runs, all content steps have completed;
        # _prepare_final_repo_review_step composes the draft from available
        # results.  Depending on all steps caused cascading blocks when any
        # non-repo step failed, making recovery replans ineffective.
        _repo_step_ids = {"use_cli_repo_grounding", "use_cli_repo_audit"}
        final_review_deps = [
            str(step.id or "").strip()
            for step in steps
            if str(step.id or "").strip() in _repo_step_ids
        ]
        final_review_task_text = (
            "Сделай финальную сверку ТЗ с репозиторием через CLI в директории:\n"
            f"{root}\n\n"
            "Нужно:\n"
            "- получить от runtime актуальный черновик ТЗ и сверить его с реальным кодом\n"
            "- перепроверить ТЗ на repo-grounded согласованность с кодом, конфигами, тестами и docs\n"
            "- отдельно проверить, что в ТЗ не названы как факты интеграции, поверхности или capability, "
            "для которых в репозитории нет прямого подтверждения\n"
            "- если найдешь расхождения или пробелы, перечислить конкретные корректировки для ТЗ\n"
            "- зафиксировать оставшиеся риски, пробелы и финальные рекомендации перед завершением"
            f"{focus_hint}"
        )
        steps.append(
            PlanStep(
                id="use_cli_repo_final_review",
                title="Финальный second-opinion review репозитория через CLI",
                instruction=final_review_task_text,
                step_type="use_cli",
                parallel_group=None,
                depends_on=final_review_deps,
                parallelizable=False,
                parallelizable_reason=None,
                ask_question=None,
                ask_options=None,
            )
        )


def _ensure_external_reference_research_step(
    user_message: str,
    context: str,
    steps: List[PlanStep],
) -> None:
    if not steps:
        return
    flags = _extract_analyst_intent_flags(context)
    executor_profile = _extract_context_value(context, "executor_profile").strip().lower()
    if not flags and executor_profile != "analyst":
        return
    reference_urls = _extract_external_reference_urls(user_message, context)
    if not reference_urls:
        return
    if "analyze_external_reference" in _extract_valid_prior_external_reference_step_ids(context):
        return
    if any(_step_covers_external_reference(step, reference_urls) for step in steps):
        return

    repo_dependencies = [
        step_id
        for step_id in ("use_cli_repo_grounding", "use_cli_repo_audit")
        if any(str(getattr(step, "id", "") or "").strip() == step_id for step in steps)
    ]
    displayed_urls = reference_urls[:5]
    urls_block = "\n".join(f"- {url}" for url in displayed_urls)
    if len(reference_urls) > len(displayed_urls):
        urls_block += f"\n- ... и ещё {len(reference_urls) - len(displayed_urls)}"
    instruction = (
        "Отдельно исследуй внешний референс из запроса пользователя.\n\n"
        f"Референсы:\n{urls_block}\n\n"
        "Нужно:\n"
        "- рассмотреть референс как внешний design-input слой, а не как source of truth проекта;\n"
        "- выделить полезные implementation patterns, структуры данных, форматы или flow;\n"
        "- связать полезные выводы с локальными файлами, модулями, контрактами или тестами текущего проекта, "
        "если такая связь подтверждается;\n"
        "- если локальная привязка не подтверждается, явно пометить вывод как requires-validation;\n"
        "- сохранить source, extracted pattern, local mapping и статус direct-adapt vs requires-validation "
        "для последующего ТЗ."
    )
    _insert_after_repo_grounding_prefix(
        steps,
        PlanStep(
            id="analyze_external_reference",
            title="Отдельный анализ внешнего референса",
            instruction=instruction,
            step_type="task",
            parallel_group=None,
            depends_on=repo_dependencies,
            parallelizable=False,
            parallelizable_reason=None,
            ask_question=None,
            ask_options=None,
        ),
    )


def _enforce_blocking_clarification_gate(context: str, steps: List[PlanStep]) -> None:
    if not steps:
        return
    flags = _extract_analyst_intent_flags(context)
    if not flags:
        return
    first_ask = next((step for step in steps if step.step_type == "ask_user"), None)
    if first_ask is None:
        return
    ask_id = str(first_ask.id or "").strip()
    if not ask_id:
        return
    first_ask.depends_on = [d for d in (first_ask.depends_on or []) if str(d or "").strip() and str(d) != ask_id]
    if steps[0] is not first_ask:
        steps[:] = [first_ask] + [step for step in steps if step is not first_ask]
    for step in steps[1:]:
        step_id = str(step.id or "").strip()
        deps = [str(d).strip() for d in (step.depends_on or []) if str(d or "").strip()]
        deps = [d for d in deps if d != step_id]
        if ask_id not in deps:
            deps.insert(0, ask_id)
        step.depends_on = deps
