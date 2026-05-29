import asyncio

from agent.analyst_prompts import build_analyst_prompt
from modes.analyst.mode import AnalystMode
from modes.analyst.template_service import default_templates_path, load_analyst_templates
from modes.registry import ModeRegistry
from sessions.session_management import SessionManagement


def _compact_whitespace(value: str) -> str:
    return " ".join(str(value).split())


def test_build_analyst_prompt_uses_template_required_sections():
    template = {
        "required_sections": ["S1", "S2"],
        "system_prompt_addition": "ADDITIONAL RULE",
    }
    out = build_analyst_prompt("goal", template)
    assert "- S1" in out
    assert "- S2" in out
    assert "Правила трассируемости и полноты" not in out
    assert "структурированный рабочий анализ" in out
    assert "с достаточной для этой задачи детализацией" in out
    assert "Если анализ repo-grounded, source of truth = реальные файлы проекта" not in out
    assert "рабочее ТЗ" not in out
    assert "Дополнительные инструкции активного шаблона:" in out
    assert "ADDITIONAL RULE" in out
    assert "без лишней детализации" not in out
    # Old hardcoded section should not leak in.
    assert "Цели и измеримые критерии успеха" not in out


def test_build_analyst_prompt_adds_repo_grounded_rules_only_for_repo_grounded_analysis_template():
    template = {
        "_id": "project_analysis",
        "required_sections": ["S1"],
        "repo_grounded_required": True,
    }
    out = build_analyst_prompt("goal", template)
    assert "Если анализ repo-grounded, source of truth = реальные файлы проекта" in out
    assert "Если данных нет, фиксируй это как [Не подтверждено] или [Требует отдельной проверки]." in out


def test_build_analyst_prompt_includes_quantitative_limits_and_system_prompt_addition_when_present():
    template = {
        "required_sections": ["S1"],
        "min_user_scenarios": 3,
        "min_functional_requirements": 12,
        "min_nfr": 5,
        "min_api_contracts": "2",
        "min_acceptance_checks": 9,
        "system_prompt_addition": "SPEC EXTRA",
    }
    out = build_analyst_prompt("goal", template)
    assert "Правила трассируемости и полноты (если применимо):" in out
    assert "- Пользовательские сценарии: минимум 3" in out
    assert "- Функциональные требования: минимум 12" in out
    assert "- Нефункциональные требования: минимум 5" in out
    assert "- API-контракты: минимум 2" in out
    assert "- Критерии приемки: минимум 9" in out
    assert "рабочее ТЗ" in out
    assert "Дополнительные инструкции активного шаблона:" in out
    assert "SPEC EXTRA" in out


def test_build_analyst_prompt_separates_clarification_answers_from_original_query():
    template = {
        "_id": "change_spec",
        "required_sections": ["S1"],
    }
    out = build_analyst_prompt(
        "Сделай ТЗ на доработку проекта",
        template,
        clarification_answers=["Только web", "Мобильные пользователи не важны"],
    )

    assert "Исходный запрос пользователя:" in out
    assert "Сделай ТЗ на доработку проекта" in out
    assert "Уже полученные уточнения пользователя:" in out
    assert "- Только web" in out
    assert "- Мобильные пользователи не важны" in out


def test_build_analyst_prompt_uses_real_refactor_spec_large_repo_grounded_contract():
    templates = load_analyst_templates(default_templates_path())
    template = dict(templates["refactor_spec"], _id="refactor_spec")

    out = build_analyst_prompt("goal", template)
    normalized = _compact_whitespace(out)

    assert template["compose_mode"] == "template_first"
    assert template["output_kind"] == "spec"
    assert template["artifact_preferred"] is True
    assert template["target_size_hint"] == "large"
    assert template["repo_grounded_required"] is True
    assert template["repo_audit_required"] is True
    assert template["final_repo_review_required"] is True
    assert "рабочее ТЗ" in out
    assert "Правила трассируемости и полноты (если применимо):" in out
    assert "поведенческой эквивалентности" in out
    assert "Дополнительные инструкции активного шаблона:" in out
    assert "Ты готовишь рабочее ТЗ на рефакторинг существующего проекта." in out
    assert "source of truth = реальные файлы проекта" in normalized
    assert "отдельно фиксируй только подтвержденные затронутые поверхности и артефакты" in normalized
    assert "telegram, desktop, miniapp" not in normalized
    assert "если предлагается новый config key, fallback layer" in normalized


def test_spec_prompt_contains_repo_grounding_rules_and_blocking_clarification_contract():
    template = {"_id": "change_spec", "required_sections": ["S1"], "repo_grounded_required": True}
    out = build_analyst_prompt("goal", template)
    normalized = _compact_whitespace(out)
    assert "Ты готовишь анализ и документ, а не реализуешь изменения в коде." in out
    assert "Работа analyst mode всегда analysis-only" in out
    assert "используй это как вход для анализа, сравнения и подготовки ТЗ/аудита" in normalized
    assert "задай blocking-вопрос через tool ask_user" in normalized
    assert "Не обходи незакрытое уточнение допущениями" in normalized
    assert "не финализируй результат, пока ответ не получен" in normalized
    assert "Если ask_user недоступен или рантайм явно ограничил новые non-blocking уточнения" not in normalized
    assert "продолжай с явными допущениями" not in normalized
    assert "Source of truth для repo-grounded/spec работы = реальные файлы проекта" in normalized
    assert "Codebase Map используй только как навигационный индекс" in normalized
    assert (
        "Запрещено придумывать новые config keys, fallback layers, compatibility wrappers, "
        "сущности, API и контракты"
    ) in normalized
    assert (
        "Если данных не хватает, фиксируй это как [Не подтверждено] или [Требует отдельной проверки]; "
        "не заполняй пробелы гипотезами."
    ) in normalized
    assert "рабочее ТЗ для low-middle разработчика без устных пояснений" in normalized
    assert (
        'делай обязательный раздел "Implementation handoff по компонентам и файлам": '
        "для каждой затронутой единицы укажи компонент/файл -> что меняется -> как проверить -> "
        "какие тесты/команды запускать"
    ) in normalized
    assert 'Не оставляй в handoff и плане реализации placeholders уровня TODO, TBD, "дописать позже"' in normalized
    assert "Для каждого Must-FR/UC/API/NFR добавляй не только формулировку требования" in normalized
    assert "Для ТЗ выбирай один лучший целевой вариант решения и описывай именно его." in normalized
    assert "Не перечисляй варианты A/B, альтернативные подходы или инвариантные ветки" in normalized
    assert "При наличии нескольких технически возможных путей выбери один лучший и опиши только его как целевой." in normalized
    assert "Не превращай ТЗ в каталог вариантов или сравнительную таблицу решений" in normalized


def test_full_detail_spec_prompt_forbids_alternative_solution_catalog() -> None:
    template = {"_id": "change_spec", "required_sections": ["S1"], "repo_grounded_required": True}
    out = build_analyst_prompt("goal", template, detail_level="full")
    normalized = _compact_whitespace(out)

    assert "не перечисляй альтернативные решения" in normalized
    assert "Фиксируй один выбранный вариант" in normalized
    assert "примеры, альтернативы и edge cases" not in normalized


def test_repo_grounded_spec_templates_require_implementation_handoff_section() -> None:
    templates = load_analyst_templates(default_templates_path())
    expected = {
        "change_spec",
        "ui_change_spec",
        "bugfix_spec",
        "integration_change_spec",
        "narrow_backend_change_spec",
        "refactor_spec",
    }

    for template_id in expected:
        template = templates[template_id]
        assert "Implementation handoff по компонентам и файлам" in template["required_sections"]
        assert "Подтвержденные факты и источники" in template["required_sections"]


def test_real_ui_change_spec_template_is_repo_grounded_and_not_generic_surface_boilerplate():
    templates = load_analyst_templates(default_templates_path())
    template = dict(templates["ui_change_spec"], _id="ui_change_spec")

    out = build_analyst_prompt("goal", template)
    normalized = _compact_whitespace(out)

    assert template["output_kind"] == "spec"
    assert template["repo_grounded_required"] is True
    assert template["repo_audit_required"] is True
    assert template["final_repo_review_required"] is True
    assert "локальную UI/UX-доработку" in normalized
    assert "компонент, экран, форма, dropdown, header, menu, CTA, states, responsive behavior" in normalized
    assert "telegram, desktop, miniapp" not in normalized
    assert "миграции, rollout, инфраструктуру или внешние поверхности" in normalized


def test_audit_prompt_contains_repo_grounding_rules():
    template = {"_id": "audit", "required_sections": ["S1"]}
    out = build_analyst_prompt("goal", template)
    normalized = _compact_whitespace(out)
    assert "Source of truth для аудита = реальные файлы проекта" in normalized
    assert "Codebase Map используй только как навигационный индекс" in normalized
    assert (
        "Запрещено придумывать новые config keys, fallback layers, compatibility wrappers, "
        "сущности, API и контракты"
    ) in normalized
    assert (
        "Если данных не хватает, фиксируй это как [Не подтверждено] или [Требует отдельной проверки]; "
        "не заполняй пробелы гипотезами."
    ) in normalized


def test_run_analyst_uses_get_template_for_session():
    async def _run():
        class _FakeBotApp:
            def __init__(self):
                self.config = type("C", (), {"defaults": type("D", (), {"summary_max_chars": 1000})()})()

                class _Analyst:
                    async def run(self, _session, prompt, _bot_app, _context, _dest):
                        assert "- S1" in prompt
                        assert "Правила трассируемости и полноты (если применимо):" in prompt
                        assert "- Пользовательские сценарии: минимум 3" in prompt
                        assert "- Функциональные требования: минимум 7" in prompt
                        assert "Дополнительные инструкции активного шаблона:" in prompt
                        assert "SESSION TEMPLATE ADDITION" in prompt
                        return "OK"

                    def get_template_for_session(self, _session):
                        return {
                            "_id": "change_spec",
                            "required_sections": ["S1"],
                            "min_user_scenarios": 3,
                            "min_functional_requirements": 7,
                            "system_prompt_addition": "SESSION TEMPLATE ADDITION",
                        }

                analyst_runner = _Analyst()
                self.get_runtime_by_capability = (
                    lambda cap: analyst_runner if str(cap) in {"run_analyst", "template_provider"} else None
                )

                class _Mgr:
                    def _persist_sessions(self):
                        return None

                self.manager = _Mgr()
                self.mode_registry = ModeRegistry()
                mode = AnalystMode()
                mode.initialize(
                    self.config,
                    services={
                        "runtime_by_capability": (
                            lambda cap: analyst_runner if str(cap) in {"run_analyst", "template_provider"} else None
                        ),
                    },
                )
                self.mode_registry.register(mode)

            async def _send_message(self, *_a, **_k):
                return None

        bot_app = _FakeBotApp()
        sm = SessionManagement(bot_app)

        # Minimal session stub for run_analyst (no queue follow-up).
        session = type(
            "S",
            (),
            {
                "id": "s1",
                "run_lock": asyncio.Lock(),
                "send_lock": asyncio.Lock(),
                "busy": False,
                "queue": [],
                "tick_seen": 0,
                "state_summary": "",
                "state_updated_at": 0.0,
            },
        )()

        await sm.run_mode_pipeline(
            session,
            "user prompt",
            dest={"kind": "telegram", "chat_id": 1},
            context=None,
            mode_id="analyst",
        )

    asyncio.run(_run())
