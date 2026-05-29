from __future__ import annotations

from .cli_contracts import CLIResponseFormat, wrap_prompt_for_response_format


def build_repo_final_review_instruction(*, base_instruction: str, draft_path: str, repo_root: str) -> str:
    return (
        f"{str(base_instruction or '').strip()}\n\n"
        "Runtime подготовил актуальный черновик ТЗ для финальной сверки.\n"
        f"Файл черновика ТЗ:\n{draft_path}\n\n"
        f"Корень репозитория:\n{repo_root}\n\n"
        "Обязательно:\n"
        "- прочитай черновик ТЗ целиком;\n"
        "- тщательно сверь ТЗ с реальными файлами проекта, конфигами, тестами и документацией;\n"
        "- проверь, что в ТЗ нет неподтвержденных сущностей, API, config keys, fallback layers или compatibility wrappers;\n"
        "- проверь, что в ТЗ перечислены только реально подтвержденные затронутые зоны "
        "(модули, конфиги, docs, tests и другие артефакты, если они подтверждены кодом);\n"
        "- если во входном запросе или исследовании был внешний референс, проверь, что он сохранён "
        "отдельным implementation-guidance слоем с source, extracted pattern, local mapping и статусом адаптации;\n"
        "- если доказательств для интеграции, поверхности или capability нет, требуй формулировку "
        '"не подтверждено" или "требует отдельной проверки", а не гипотезу;\n'
        "- если найдешь расхождения или пробелы, перечисли конкретные корректировки, которые нужно внести в ТЗ;\n"
        "- если критичных расхождений нет, явно зафиксируй это.\n\n"
        "Верни результат строго по runtime structured-output контракту для final repo review.\n"
        "Заполни поля verdict, mismatches, unverified_claims, corrections, claims, evidence и open_gaps.\n"
        "Не возвращай markdown-блоки VERDICT/MISMATCHES, code fences или пояснения вне структуры."
    )


def build_gap_closure_prompt(
    *,
    repo_root: str,
    draft_path: str,
    fact_pack_path: str,
    claim_ledger_path: str,
    open_gaps_path: str,
    artifacts_index_path: str,
    task_contract_path: str = "",
    obligation_matrix_path: str = "",
    retry_context: str = "",
) -> str:
    base = (
        "Выполни финальную шлифовку repo-grounded документа по артефактам, а не по памяти контекста.\n\n"
        f"Корень репозитория:\n{repo_root}\n\n"
        "Артефакты runtime:\n"
        f"- черновик документа: {draft_path}\n"
        f"- fact pack: {fact_pack_path}\n"
        f"- claim ledger: {claim_ledger_path}\n"
        f"- open gaps: {open_gaps_path}\n"
        f"- artifacts index: {artifacts_index_path}\n\n"
        "Задача:\n"
        "- прочитай все перечисленные файлы;\n"
        "- работай в режиме preservation-first patch/merge поверх текущего черновика, а не rewrite-from-scratch;\n"
        "- используй task contract и obligation matrix как источник обязательств текущей задачи;\n"
        "- закрой только те гэпы, которые можно закрыть на основе claim ledger, fact pack и артефактов;\n"
        '- если подтверждения нет, оставь формулировку "не подтверждено" или "требует отдельной проверки";\n'
        "- не придумывай новые сущности, API, маршруты, config keys, fallback layers или миграции;\n"
        "- не удаляй title, `Исходная задача`, core shell sections и `Открытые вопросы и валидационные шаги`;\n"
        "- потеря protected spec shell считается preservation regression и должна быть исправлена, а не принята молча;\n"
        '- если обязательные входы задачи остаются незакрытыми, не закрывай их разделом "Допущения и незакрытые входы";\n'
        "  удерживай их как открытые blocking obligations и явно фиксируй в `Открытые вопросы и валидационные шаги`;\n"
        "- если repo evidence не хватает для полного контракта, но реализацию можно начинать только после проверки,\n"
        "  переведи это в явный `manual validation gate`: какой artifact/fixture/команда нужны,\n"
        "  какой файл/компонент зависит от этой проверки и какой acceptance signal закроет гэп;\n"
        "- если поверхность не подтверждена repo evidence и не входит в исходный scope пользователя,\n"
        "  явно пометь её `out of scope` / `вне scope этой задачи` и не удерживай как blocking implementation obligation;\n"
        '- не используй формулировки уровня "реализационная деталь", "не является source of truth",\n'
        '  "решим в реализации" вместо конкретного ownership/seam/verification;\n'
        "- если во входном черновике есть внешний референс, сохрани его отдельным implementation-guidance слоем,\n"
        "  а не удаляй только потому, что это внешний источник;\n"
        "- для каждого внешнего референса удерживай source, extracted pattern, local mapping и статус адаптации;\n"
        "- сохрани структуру документа и сделай его более точным, конкретным и repo-grounded;\n"
        "- верни исправленный документ и machine-readable список закрытых/незакрытых obligations.\n"
    )
    if task_contract_path:
        base += f"\n- task contract: {task_contract_path}\n"
    if obligation_matrix_path:
        base += f"- obligation matrix: {obligation_matrix_path}\n"
    if retry_context:
        base += (
            "\nПовторная попытка после непрохождения предыдущего шага. "
            "Исправь именно эти проблемы и верни полный structured bundle:\n"
            f"{str(retry_context or '').strip()}\n"
        )
    return wrap_prompt_for_response_format(base, CLIResponseFormat.SPEC_FIX_BUNDLE_JSON)


def build_followup_repo_final_review_prompt(
    *,
    repo_root: str,
    draft_path: str,
    draft_sha1: str = "",
    fact_pack_path: str,
    claim_ledger_path: str,
    artifacts_index_path: str,
    task_contract_path: str = "",
    obligation_matrix_path: str = "",
    retry_context: str = "",
) -> str:
    base = (
        "Выполни повторную финальную obligation-driven repo-grounded сверку уже исправленного документа.\n\n"
        f"Корень репозитория:\n{repo_root}\n\n"
        "Артефакты runtime:\n"
        f"- исправленный черновик документа: {draft_path}\n"
        f"- expected draft sha1: {str(draft_sha1 or '').strip() or '[missing]'}\n"
        f"- fact pack: {fact_pack_path}\n"
        f"- claim ledger: {claim_ledger_path}\n"
        f"- artifacts index: {artifacts_index_path}\n\n"
        "Задача:\n"
        "- валидируй именно этот persisted draft artifact по указанным path+sha1;\n"
        "- прочитай исправленный черновик и заново проверь closure blocking obligations;\n"
        "- проверь, что rework шёл как patch/merge поверх исходного draft, а не как rewrite-from-scratch;\n"
        "- если draft artifact устарел, изменился между сохранением и review или не соответствует указанному sha1,\n"
        "  открой blocking obligation и degraded mode вместо молчаливого PASS;\n"
        "- перечисли незакрытые blocking obligations, false closures и обязательные корректировки;\n"
        "- если после правок все blocking obligations закрыты, явно зафиксируй это;\n"
        '- если подтверждения нет, оставь формулировку "не подтверждено" или "требует отдельной проверки";\n'
        "- если документ честно перевел неподтвержденный контракт в `manual validation gate`\n"
        "  с конкретным artifact/fixture/командой, зависимым файлом/компонентом и acceptance signal,\n"
        "  не считай это blocking implementation gap само по себе;\n"
        "- если поверхность не подтверждена repo evidence и явно помечена `out of scope` / `вне scope этой задачи`,\n"
        "  не удерживай ее как blocking obligation, пока документ не делает claims о поддержке этой поверхности;\n"
        '- формулировки уровня "реализационная деталь", "не является source of truth", "решим в реализации"\n'
        "  без конкретного ownership/seam/verification считай required correction;\n"
        "- потеря title, `Исходная задача`, core shell sections или `Открытые вопросы и валидационные шаги`\n"
        "  считается preservation regression и не должна проходить молча;\n"
        '- если required input остался незакрытым, проверь, что его не закрыли ложным разделом "Допущения и незакрытые входы";\n'
        "  он должен оставаться blocking obligation и быть явно отражён в `Открытые вопросы и валидационные шаги`;\n"
        "- если в документе есть внешний референс, проверь, что он сохранён отдельным implementation-guidance слоем,\n"
        "  а не смешан с repo facts и не потерян после rework;\n"
        "- не придумывай новые сущности, API, маршруты, config keys, fallback layers или миграции.\n"
    )
    if task_contract_path:
        base += f"\n- task contract: {task_contract_path}\n"
    if obligation_matrix_path:
        base += f"- obligation matrix: {obligation_matrix_path}\n"
    if retry_context:
        base += (
            "\nПовторная попытка после непрохождения предыдущего verifier-pass. "
            "Исправь structured output по следующим причинам:\n"
            f"{str(retry_context or '').strip()}\n"
        )
    return wrap_prompt_for_response_format(base, CLIResponseFormat.OBLIGATION_REVIEW_BUNDLE_JSON)
