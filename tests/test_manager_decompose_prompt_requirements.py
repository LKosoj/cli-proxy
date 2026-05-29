from __future__ import annotations

from agent.manager_prompts import (
    DECOMPOSE_INSTRUCTION,
    DECOMPOSE_NORMALIZE_SYSTEM,
    PLAN_VALIDATION_SYSTEM,
    PLAN_FIX_MINIMAL_INSTRUCTION,
    PLAN_FIX_SYSTEM,
)


def test_decompose_prompt_requires_full_remaining_work_coverage() -> None:
    assert "План должен покрывать ВЕСЬ объём remaining_work без пропусков." in DECOMPOSE_INSTRUCTION
    assert "каждый пункт remaining_work покрыт минимум одной задачей" in DECOMPOSE_INSTRUCTION
    assert "нет \"потерянной\" работы вне списка задач" in DECOMPOSE_INSTRUCTION


def test_decompose_prompt_forbids_overly_broad_tasks_and_requires_verification() -> None:
    assert "НЕ делай слишком широкие задачи" in DECOMPOSE_INSTRUCTION
    assert "не более {max_requirements_per_task} требований" in DECOMPOSE_INSTRUCTION
    assert "минимум 2 acceptance_criteria" in DECOMPOSE_INSTRUCTION
    assert "функциональный" in DECOMPOSE_INSTRUCTION
    assert "способ проверки" in DECOMPOSE_INSTRUCTION
    assert "МАКСИМАЛЬНО атомарную декомпозицию" in DECOMPOSE_INSTRUCTION
    assert "Количество задач должно быть в диапазоне {min_tasks_dynamic}-{max_tasks}" in DECOMPOSE_INSTRUCTION
    assert "Запрещено укрупнять задачи ради уменьшения их количества" in DECOMPOSE_INSTRUCTION
    assert "covers_requirements" in DECOMPOSE_INSTRUCTION
    assert "requirements" in DECOMPOSE_INSTRUCTION
    assert "checklist_table" in DECOMPOSE_INSTRUCTION


def test_manager_prompts_use_dynamic_max_tasks_placeholder() -> None:
    assert "{min_tasks_dynamic}" in DECOMPOSE_INSTRUCTION
    assert "{max_tasks}" in DECOMPOSE_NORMALIZE_SYSTEM
    assert "{max_tasks}" in PLAN_FIX_SYSTEM
    assert "checklist_table" in PLAN_FIX_SYSTEM
    assert "project_analysis" in PLAN_FIX_SYSTEM


def test_decompose_normalize_prompt_is_format_safe() -> None:
    rendered = DECOMPOSE_NORMALIZE_SYSTEM.format(max_tasks=30)
    assert '"project_analysis"' in rendered
    assert "Максимум 30 задач" in rendered


def test_plan_fix_minimal_prompt_respects_rules_and_allows_split_when_permitted() -> None:
    assert "atomicity_hotspots" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "project_analysis" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.max_requirements_per_task" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.no_new_tasks_by_default=true" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.no_new_tasks_by_default=false" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "дробить слишком широкие задачи" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "глобальный повторный проход по ВСЕМ задачам" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "обязательный чеклист" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "covers_requirements" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "depends_on" in PLAN_FIX_MINIMAL_INSTRUCTION


def test_plan_fix_minimal_prompt_defines_rules_contract_fields() -> None:
    assert "rules.max_tasks" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.min_tasks" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.max_requirements_per_task" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.preserve_ids=true" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "rules.prevent_count_oscillation=true" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "не добавляй новые задачи, только исправляй существующие" in PLAN_FIX_MINIMAL_INSTRUCTION
    assert "разрешено добавлять новые задачи" in PLAN_FIX_MINIMAL_INSTRUCTION


def test_plan_fix_minimal_prompt_is_format_safe() -> None:
    rendered = PLAN_FIX_MINIMAL_INSTRUCTION.format(
        payload_json='{"issues":[],"tasks":[]}',
        max_tasks=12,
        max_requirements_per_task=2,
    )
    assert '{"issues":[],"tasks":[]}' in rendered
    assert "не более 12" in rendered
    assert "Обязательно соблюдай `rules` из payload" in rendered
    assert "не должно остаться больше `rules.max_requirements_per_task`" in rendered


def test_plan_fix_system_respects_rules_and_preserves_project_context() -> None:
    assert "atomicity_hotspots" in PLAN_FIX_SYSTEM
    assert "rules.max_tasks" in PLAN_FIX_SYSTEM
    assert "rules.min_tasks" in PLAN_FIX_SYSTEM
    assert "rules.max_requirements_per_task" in PLAN_FIX_SYSTEM
    assert "rules.no_new_tasks_by_default=true" in PLAN_FIX_SYSTEM
    assert "rules.no_new_tasks_by_default=false" in PLAN_FIX_SYSTEM
    assert "rules.prevent_count_oscillation=true" in PLAN_FIX_SYSTEM
    assert "повторно проверь ВСЕ задачи" in PLAN_FIX_SYSTEM
    assert "обязательный чеклист" in PLAN_FIX_SYSTEM
    assert "Сохраняй `project_analysis`" in PLAN_FIX_SYSTEM
    assert "Верни строго валидный JSON с полями `project_analysis`, `checklist_table`, `tasks`." in PLAN_FIX_SYSTEM


def test_plan_validation_prompt_allows_extra_tasks_if_goal_is_covered() -> None:
    assert "Дополнительные задачи допустимы" in PLAN_VALIDATION_SYSTEM
    assert "не упомянута в project_goal" in PLAN_VALIDATION_SYSTEM
    assert "S9.8/S9.9" in PLAN_VALIDATION_SYSTEM
