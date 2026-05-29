from __future__ import annotations

# Этот модуль только собирает prompt из уже выбранного шаблона.
# Semantic classification запроса (analysis/spec/audit и связанные semantic flags)
# остается ответственностью модели и upstream intent/plugin слоя, а не Python heuristics.


def _build_quantitative_constraints_block(template: dict) -> str:
    tmpl = template or {}
    lines = []

    # Traceability rules (новый формат)
    traceability = tmpl.get("traceability_rules")
    if isinstance(traceability, list):
        items = [str(x).strip() for x in traceability if str(x).strip()]
        if items:
            lines.extend(f"- {item}" for item in items)

    # Legacy min_* counters (обратная совместимость)
    limits = [
        ("min_user_scenarios", "Пользовательские сценарии"),
        ("min_functional_requirements", "Функциональные требования"),
        ("min_nfr", "Нефункциональные требования"),
        ("min_api_contracts", "API-контракты"),
        ("min_acceptance_checks", "Критерии приемки"),
    ]
    for key, label in limits:
        value = tmpl.get(key)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        lines.append(f"- {label}: минимум {number}")

    if not lines:
        return ""
    return "\nПравила трассируемости и полноты (если применимо):\n" + "\n".join(lines)


def _build_system_prompt_addition_block(template: dict) -> str:
    tmpl = template or {}
    addition = str(tmpl.get("system_prompt_addition") or "").strip()
    if not addition:
        return ""
    return "\n\nДополнительные инструкции активного шаблона:\n" + addition


def _is_spec_template(template: dict) -> bool:
    tmpl = template or {}
    template_id = str(tmpl.get("_id") or "").strip().lower()
    output_kind = str(tmpl.get("output_kind") or "").strip().lower()
    compose_mode = str(tmpl.get("compose_mode") or "").strip().lower()
    if output_kind == "spec" or compose_mode == "template_first":
        return True
    if template_id in {
        "new_spec",
        "change_spec",
        "refactor_spec",
        "migration_plan",
        "integration_contract",
        "test_strategy",
        "release_readiness",
    }:
        return True
    traceability = tmpl.get("traceability_rules")
    if isinstance(traceability, list) and traceability:
        return True
    return any(
        tmpl.get(key) is not None
        for key in (
            "min_user_scenarios",
            "min_functional_requirements",
            "min_nfr",
            "min_api_contracts",
            "min_acceptance_checks",
        )
    )


def _document_kind(template: dict) -> str:
    tmpl = template or {}
    template_id = str(tmpl.get("_id") or "").strip().lower()
    if template_id == "audit":
        return "audit"
    if _is_spec_template(tmpl):
        return "spec"
    return "analysis"


def _is_repo_grounded(template: dict) -> bool:
    tmpl = template or {}
    template_id = str(tmpl.get("_id") or "").strip().lower()
    return bool(
        tmpl.get("repo_grounded_required")
        or template_id == "audit"
    )


def _header_for_kind(kind: str) -> str:
    if kind == "audit":
        return (
            "Ты — старший технический аудитор с опытом в code review, архитектурных ревью и оценке рисков. "
            "Ты мыслишь критически, разделяешь факты и предположения, опираешься на доказательства "
            "и всегда приоритизируешь находки по степени риска и влияния на бизнес."
        )
    if kind == "spec":
        return (
            "Ты — старший системный аналитик с опытом в проектировании ПО и декомпозиции требований. "
            "Ты мыслишь структурно, обеспечиваешь трассируемость требований от бизнес-целей до критериев приёмки "
            "и проверяешь полноту покрытия каждого аспекта системы."
        )
    return (
        "Ты — старший системный аналитик с опытом в технической аналитике и принятии архитектурных решений. "
        "Ты мыслишь структурно, разделяешь факты и допущения, и всегда проверяешь полноту покрытия "
        "прежде чем формулировать выводы."
    )


def _task_for_kind(kind: str) -> str:
    if kind == "spec":
        return (
            "Задача: из исходного запроса пользователя получить полноценное и проработанное рабочее ТЗ,\n"
            "достаточное для передачи команде разработки и реализации итогового продукта.\n"
            "Ты готовишь анализ и документ, а не реализуешь изменения в коде."
        )
    if kind == "audit":
        return (
            "Задача: критически проанализировать существующий документ/код/проект по запросу пользователя и составить отчет,\n"
            "который помогает принять решения и снизить риски.\n"
            "Твоя роль ограничена анализом и рекомендациями; реализацию не выполняй."
        )
    return (
        "Задача: из исходного запроса пользователя подготовить структурированный рабочий анализ,\n"
        "который помогает принять решение и сделать следующий практический шаг\n"
        "с достаточной для этой задачи детализацией.\n"
        "Ограничение: не переходи к реализации и не меняй проект."
    )


def _common_rules() -> list[str]:
    return [
        "Работа analyst mode всегда analysis-only: не вноси изменения в код, конфиги, тесты,\n"
        "документацию и артефакты проекта.",
        "Не реализуй функциональность, не применяй патчи и не выполняй команды с side effects.",
        "Если пользователь формулирует запрос как доработку/обогащение функционала или даёт внешний референс,\n"
        "используй это как вход для анализа, сравнения и подготовки ТЗ/аудита, а не как команду на реализацию.",
        "Если внешний референс релевантен, сохраняй его в итоговом документе отдельным слоем\n"
        "implementation guidance / примеров реализации, а не вычищай после repo-grounded проверки.",
        "Сначала уточни контекст и ограничения, если они влияют на решение.",
        "Когда информации недостаточно, задавай уточняющие вопросы через tool ask_user.",
        'Для каждого вопроса предлагай 2-4 варианта ответа и всегда добавляй вариант "Свой вариант".',
        "Если для качественного результата нужен ответ пользователя, задай blocking-вопрос через tool ask_user\n"
        "и дождись ответа до финализации результата.",
        "Не обходи незакрытое уточнение допущениями и не финализируй результат, пока ответ не получен.",
        "Используй инструменты осмысленно:",
        "  - brainstorm: для структурирования решения и проверки логики выбора;",
        "  - sequential thinking: для пошаговой проверки логики и полноты;",
        "  - search_web / web_research: если нужны внешние данные, практики, нормативка, сравнение;",
        "  - use_cli: для аудита текущего проекта (локальные файлы/структура/конфиги/история изменений)\n"
        "    и для финального second-opinion ревью уже собранного ТЗ; не для внесения изменений.",
        "Если внешние исследования не нужны, явно зафиксируй это как допущение.",
        "При выборе техстека придерживайся принципа минимально достаточного решения:",
        "  - выбирай только необходимые технологии, без избыточных компонентов;",
        "  - при прочих равных предпочитай популярные, зрелые и широко поддерживаемые технологии;",
        "  - явно фиксируй критерии выбора: распространенность на рынке, зрелость экосистемы,\n"
        "    качество документации, долгосрочная поддержка.",
    ]


def _rules_for_kind(kind: str) -> list[str]:
    if kind == "spec":
        return [
            "Не останавливайся на черновике: доведи результат до финального рабочего ТЗ.",
            "Для ТЗ выбирай один лучший целевой вариант решения и описывай именно его.",
            "Не перечисляй варианты A/B, альтернативные подходы или инвариантные ветки, "
            "если пользователь явно не просил сравнительный анализ.",
            "Если входных данных недостаточно для выбора лучшего решения, сначала запроси недостающий вход через ask_user, "
            "а не раскладывай документ на несколько допустимых вариантов.",
        ]
    if kind == "audit":
        return [
            "Опираться на доказательства: имена файлов, функции, конфигурации, фактическое поведение.",
            "Разделять факты, неподтвержденные зоны и рекомендации.",
            "Приоритеты: сначала наиболее критичные риски и блокеры, затем улучшения.",
            "Не выдумывай детали: если данных нет, помечай как [Нужно уточнить] или [Недостаточно данных].",
            "Для глубокого аудита текущего проекта используй use_cli, когда стандартных read/search инструментов недостаточно.",
            "Для итогового отчета/ТЗ делай финальную проверку через use_cli (second-opinion), чтобы выявить пропуски и противоречия.",
        ]
    return [
        "Если запрос не требует полного ТЗ, не раздувай ответ сверх уровня детализации,\n"
        "который реально нужен для этой задачи.",
    ]


def _repo_grounded_rules(kind: str) -> list[str]:
    if kind == "audit":
        return [
            "Source of truth для аудита = реальные файлы проекта, конфиги, код, логи и наблюдаемое поведение.",
            "Codebase Map используй только как навигационный индекс и подсказку, а не как нормативный источник истины.",
            "Если данных не хватает, фиксируй это как [Не подтверждено] или [Требует отдельной проверки]; не заполняй пробелы гипотезами.",
            "Запрещено придумывать новые config keys, fallback layers, compatibility wrappers,\n"
            "сущности, API и контракты без явного основания в коде проекта\n"
            "или прямом запросе пользователя.",
            "Если внешний референс дан пользователем или собран исследованием, сохрани его отдельным разделом\n"
            "как implementation guidance: source, extracted pattern, local mapping, direct-adapt vs requires-validation.",
        ]
    if kind == "spec":
        return [
            "Source of truth для repo-grounded/spec работы = реальные файлы проекта, конфиги,\n"
            "код и подтвержденные артефакты из репозитория.",
            "Codebase Map используй только как навигационный индекс и подсказку, а не как нормативный источник истины.",
            "Если данных не хватает, фиксируй это как [Не подтверждено] или [Требует отдельной проверки]; не заполняй пробелы гипотезами.",
            "Запрещено придумывать новые config keys, fallback layers, compatibility wrappers,\n"
            "сущности, API и контракты без явного основания в коде проекта\n"
            "или прямом запросе пользователя.",
            'Для repo-grounded spec/refactor/bugfix/ui шаблонов делай обязательный раздел '
            '"Implementation handoff по компонентам и файлам": '
            "для каждой затронутой единицы укажи компонент/файл -> что меняется -> как проверить -> какие тесты/команды запускать.",
            'Не оставляй в handoff и плане реализации placeholders уровня TODO, TBD, "дописать позже", '
            '"нужно добавить тесты", "обработать edge cases" без конкретики.',
        ]
    return [
        "Если анализ repo-grounded, source of truth = реальные файлы проекта, конфиги, код и подтвержденные артефакты из репозитория.",
        "Codebase Map используй только как навигационный индекс и подсказку, а не как нормативный источник истины.",
        "Для repo-grounded анализа запрещено заполнять пробелы гипотезами или достраивать выводы по аналогии.\n"
        "Если данных нет, фиксируй это как [Не подтверждено] или [Требует отдельной проверки].",
    ]


def _quality_requirements(kind: str, *, repo_grounded: bool) -> list[str]:
    out = ["Пиши конкретно, без воды."]
    if repo_grounded:
        if kind == "spec":
            out.append(
                "Для repo-grounded/spec работы оставляй только подтвержденные факты; "
                "если подтверждения нет, пиши [Не подтверждено] или [Требует отдельной проверки]."
            )
        else:
            out.append(
                "Для repo-grounded анализа оставляй только подтвержденные факты; "
                "если подтверждения нет, пиши [Не подтверждено] или [Требует отдельной проверки]."
            )
    if kind == "audit":
        out.extend(
            [
                "Рекомендации должны быть применимы (что изменить, где, зачем).",
                "Если есть неоднозначность, зафиксируй варианты и рекомендуемый.",
            ]
        )
    elif kind == "spec":
        out.extend(
            [
                "При наличии нескольких технически возможных путей выбери один лучший и опиши только его как целевой.",
                "Не превращай ТЗ в каталог вариантов или сравнительную таблицу решений, "
                "если пользователь прямо не просил comparison-mode.",
                "Результат должен быть применим как рабочее ТЗ для low-middle разработчика без устных пояснений.",
                "Для каждого Must-FR/UC/API/NFR добавляй не только формулировку требования, но и явный способ реализации или проверки.",
                "Раздел с открытыми вопросами используй только для реально недостающих входов, внешних зависимостей "
                "или неподтвержденных ограничений, а не для перечисления альтернативных решений.",
            ]
        )
    else:
        out.extend(
            [
                "Если есть неоднозначность, зафиксируй варианты и рекомендуемый.",
                "Результат должен быть прикладным и пригодным для дальнейшего обсуждения или декомпозиции.",
            ]
        )
    return out


def _build_qa_block(template: dict) -> str:
    tmpl = template or {}
    qa = str(tmpl.get("qa_prompt") or "").strip()
    if not qa:
        return ""
    return (
        "\n\nСамопроверка перед выдачей результата (ОБЯЗАТЕЛЬНО):\n"
        "Перед финализацией пройди по каждому пункту и убедись, что он выполнен.\n"
        "Если пункт не выполнен — доработай результат, а не просто отметь его.\n"
        + qa
    )


def _build_detail_level_block(detail_level: str, kind: str) -> str:
    level = str(detail_level or "").strip().lower()
    if level == "brief":
        scope = "1-2 страницы"
        guidance = (
            "Краткий формат: только ключевые выводы и рекомендации. "
            "Пропускай развёрнутые обоснования — оставляй только суть. "
            "Если раздел шаблона не применим или тривиален, пропусти его."
        )
    elif level == "full":
        scope = "7+ страниц"
        if kind == "spec":
            guidance = (
                "Полный формат: максимальная детализация каждого раздела. "
                "Включай развёрнутые обоснования и edge cases, но не перечисляй альтернативные решения. "
                "Фиксируй один выбранный вариант и раскрывай его до implementation-ready уровня. "
                "Каждый раздел шаблона MUST быть раскрыт."
            )
        else:
            guidance = (
                "Полный формат: максимальная детализация каждого раздела. "
                "Включай развёрнутые обоснования, примеры, альтернативы и edge cases. "
                "Каждый раздел шаблона MUST быть раскрыт."
            )
    else:
        scope = "3-5 страниц"
        guidance = (
            "Стандартный формат: достаточная детализация для принятия решений. "
            "Включай обоснования для ключевых решений, но не раздувай очевидное."
        )
    return f"\nОжидаемый объём: {scope}.\n{guidance}"


def _build_workflow_phases_block(kind: str) -> str:
    if kind == "audit":
        return (
            "\nПорядок работы (выполняй последовательно):\n"
            "1. СБОР ДАННЫХ: изучи все релевантные файлы, конфиги, логи и код. "
            "Зафиксируй конкретные наблюдения с указанием источника (файл, строка, функция).\n"
            "2. СТРУКТУРИРОВАНИЕ: сгруппируй наблюдения по категориям (архитектура, безопасность, качество кода, производительность). "
            "Выяви связи и противоречия между наблюдениями.\n"
            "3. АНАЛИЗ: для каждой группы определи severity (critical/major/minor/info) и impact. "
            "Примени принцип: факт → причина → следствие → рекомендация.\n"
            "4. СИНТЕЗ: сформируй итоговый документ по шаблону. "
            "Убедись, что каждый вывод подтверждён минимум одним наблюдением из шага 1.\n"
            "5. ВАЛИДАЦИЯ: перепроверь — нет ли противоречий между разделами? "
            "Все ли критические находки попали в рекомендации? Нет ли выводов без доказательств?"
        )
    if kind == "spec":
        return (
            "\nПорядок работы (выполняй последовательно):\n"
            "1. СБОР КОНТЕКСТА: уточни бизнес-цели, ограничения, стейкхолдеров и целевую аудиторию. "
            "Если информации недостаточно — задай уточняющие вопросы через ask_user.\n"
            "2. ДЕКОМПОЗИЦИЯ: разложи задачу на пользовательские сценарии (UC), "
            "затем из каждого UC выведи функциональные требования (FR).\n"
            "3. ПРОРАБОТКА: для каждого FR определи: критерий приёмки, способ проверки, "
            "возможные ошибки. Добавь нефункциональные требования и API-контракты.\n"
            "4. СИНТЕЗ: собери итоговый документ по шаблону. "
            "Проверь трассируемость: каждый UC покрыт FR, каждый Must-FR имеет тест.\n"
            "5. ВАЛИДАЦИЯ: перепроверь полноту — нет ли UC без FR? "
            "Нет ли FR без критерия приёмки? Нет ли противоречий между разделами?"
        )
    return (
        "\nПорядок работы (выполняй последовательно):\n"
        "1. СБОР ДАННЫХ: определи, какая информация нужна для ответа. "
        "Собери факты из доступных источников. Зафиксируй пробелы в данных.\n"
        "2. СТРУКТУРИРОВАНИЕ: организуй собранные данные. "
        "Выяви связи, зависимости и противоречия.\n"
        "3. АНАЛИЗ: сформулируй выводы на основе данных. "
        "Для каждого вывода укажи, на каких данных он основан.\n"
        "4. СИНТЕЗ: сформируй итоговый документ по шаблону.\n"
        "5. ВАЛИДАЦИЯ: перепроверь — нет ли противоречий? "
        "Все ли выводы подтверждены данными? Зафиксированы ли все допущения?"
    )


def _build_required_inputs_block(template: dict) -> str:
    tmpl = template or {}
    inputs = tmpl.get("required_inputs")
    if not inputs or not isinstance(inputs, list):
        return ""
    items = [str(x).strip() for x in inputs if str(x).strip()]
    if not items:
        return ""
    return (
        "\nОбязательные входные данные (уточни через ask_user, если не предоставлены):\n"
        + "\n".join(f"- {item}" for item in items)
        + (
            '\n\nЕсли обязательный вход не предоставлен, задай blocking-вопрос через ask_user '
            'и не закрывай его разделом "Допущения и незакрытые входы".'
        )
    )


def _enumerate_rules(rules: list[str]) -> str:
    return "\n".join(f"{idx}. {rule}" for idx, rule in enumerate(rules, start=1))


def _build_user_goal_block(user_goal: str, clarification_answers: list[str] | None = None) -> str:
    lines = [
        "Исходный запрос пользователя:",
        str(user_goal or "").strip(),
    ]
    normalized_answers = [
        str(item).strip()
        for item in (clarification_answers or [])
        if str(item).strip()
    ]
    if normalized_answers:
        lines.extend(
            [
                "",
                "Уже полученные уточнения пользователя:",
                *[f"- {item}" for item in normalized_answers],
            ]
        )
    return "\n".join(lines).rstrip()


def build_analyst_prompt(
    user_goal: str,
    template: dict,
    *,
    detail_level: str = "",
    repo_grounding_text: str = "",
    codebase_context_text: str = "",
    clarification_answers: list[str] | None = None,
) -> str:
    tmpl = template or {}
    required_sections = []
    try:
        required_sections = list(tmpl.get("required_sections") or [])
    except Exception:
        required_sections = []
    bullets = "\n".join(f"- {str(s).strip()}" for s in required_sections if str(s).strip()) or "- (не заданы)"
    kind = _document_kind(tmpl)
    repo_grounded = _is_repo_grounded(tmpl)
    quantitative_constraints_block = _build_quantitative_constraints_block(tmpl) if kind == "spec" else ""
    system_prompt_addition_block = _build_system_prompt_addition_block(tmpl)
    qa_block = _build_qa_block(tmpl)
    detail_level_block = _build_detail_level_block(detail_level, kind)
    workflow_phases_block = _build_workflow_phases_block(kind)
    required_inputs_block = _build_required_inputs_block(tmpl)

    rules = _common_rules()
    if repo_grounded:
        rules.extend(_repo_grounded_rules(kind))
    rules.extend(_rules_for_kind(kind))

    lines = [
        _header_for_kind(kind),
    ]

    # Repo-grounding и codebase context — в первых 30% промпта для приоритизации
    repo_ground = str(repo_grounding_text or "").strip()
    codebase_ctx = str(codebase_context_text or "").strip()
    if repo_ground:
        lines.append("")
        lines.append(repo_ground)
    if codebase_ctx:
        lines.append("")
        lines.append(codebase_ctx)

    lines.extend([
        "",
        _task_for_kind(kind),
        detail_level_block,
        workflow_phases_block,
    ])
    if required_inputs_block:
        lines.append(required_inputs_block)
    lines.extend([
        "",
        _build_user_goal_block(user_goal, clarification_answers),
        "",
        "Обязательные правила:",
        _enumerate_rules(rules),
        "",
        "Формат финального результата (обязательно):",
        bullets,
    ])
    if quantitative_constraints_block:
        lines.append(quantitative_constraints_block.lstrip("\n"))
    lines.extend(
        [
            "",
            "Требования к качеству:",
            "\n".join(f"- {item}" for item in _quality_requirements(kind, repo_grounded=repo_grounded)),
        ]
    )

    base_prompt = "\n".join(lines).rstrip()
    result = base_prompt + system_prompt_addition_block
    if qa_block:
        result += qa_block
    return result + "\n"
