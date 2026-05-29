from __future__ import annotations

from pathlib import Path

import yaml

from agent.manager_prompts import (
    DECISION_SYSTEM,
    REVIEW_INSTRUCTION_TEMPLATE,
    DEV_INSTRUCTION_TEMPLATE,
    DEV_REWORK_INSTRUCTION_TEMPLATE,
    FINAL_SPEC_AUDIT_RETRY_TASK,
    FINAL_SPEC_AUDIT_TASK,
    PLAN_VALIDATION_SYSTEM,
    REVIEW_NORMALIZE_SYSTEM,
)


ROOT = Path(__file__).resolve().parents[1]


def test_review_prompt_requires_duplicate_functionality_check() -> None:
    assert "Обязательно проверь дублирование функционала в доработках" in REVIEW_INSTRUCTION_TEMPLATE
    assert "При обнаружении — `approved=false`" in REVIEW_INSTRUCTION_TEMPLATE
    assert "укажи, где найдено" in REVIEW_INSTRUCTION_TEMPLATE
    assert "дублирование (файлы/функции) и что нужно консолидировать." in REVIEW_INSTRUCTION_TEMPLATE


def test_review_prompt_requires_not_done_only_assessment() -> None:
    assert "Явно обработай `checklist_table` из отчёта разработчика" in REVIEW_INSTRUCTION_TEMPLATE
    assert "учитывай ТОЛЬКО строки со статусом `not_done`" in REVIEW_INSTRUCTION_TEMPLATE
    assert "строки со статусом `done` игнорируй" in REVIEW_INSTRUCTION_TEMPLATE
    assert "not_done_assessment" in REVIEW_INSTRUCTION_TEMPLATE
    assert '"verdict": "justified или not_justified"' in REVIEW_INSTRUCTION_TEMPLATE


def test_plan_validation_prompt_requires_checklist_consistency() -> None:
    assert "Проверка `checklist_table`" in PLAN_VALIDATION_SYSTEM
    assert "ТОЛЬКО строки со статусом `not_done`" in PLAN_VALIDATION_SYSTEM
    assert "Если причина обоснованная — это допустимо" in PLAN_VALIDATION_SYSTEM
    assert "Строки со статусом `done`" in PLAN_VALIDATION_SYSTEM


def test_plan_validation_prompt_requires_granular_checks() -> None:
    assert "Проверка на недодекомпозицию по смыслу" in PLAN_VALIDATION_SYSTEM
    assert "project_analysis.remaining_work" in PLAN_VALIDATION_SYSTEM
    assert "project_analysis.requirements" in PLAN_VALIDATION_SYSTEM
    assert "верни issue о недодекомпозиции" in PLAN_VALIDATION_SYSTEM
    assert "Проверка атомарности задач по смыслу" in PLAN_VALIDATION_SYSTEM
    assert "одна задача = один проверяемый результат" in PLAN_VALIDATION_SYSTEM
    assert "верни issue о неатомарности" in PLAN_VALIDATION_SYSTEM


def test_plan_validation_prompt_avoids_repeating_structural_checks() -> None:
    assert "базовые structural checks уже выполнены кодом manager" in PLAN_VALIDATION_SYSTEM
    assert "НЕ перепроверяй как основной критерий" in PLAN_VALIDATION_SYSTEM
    assert "количество задач vs min/max" in PLAN_VALIDATION_SYSTEM
    assert "формальную трассируемость `covers_requirements`" in PLAN_VALIDATION_SYSTEM
    assert "формальный лимит атомарности по количеству `covers_requirements`" in PLAN_VALIDATION_SYSTEM
    assert "формально корректный JSON-план" in PLAN_VALIDATION_SYSTEM


def test_decision_system_rejects_on_duplicate_functionality() -> None:
    assert "Если ревьюер нашёл дублирование функционала в доработках — rejected." in DECISION_SYSTEM


def test_dev_prompts_require_checklist_table_in_report() -> None:
    assert '"checklist_table"' in DEV_INSTRUCTION_TEMPLATE
    assert '"checklist_table"' in DEV_REWORK_INSTRUCTION_TEMPLATE
    assert "Как выполнено / доказательство" in DEV_INSTRUCTION_TEMPLATE
    assert "Как выполнено / доказательство" in DEV_REWORK_INSTRUCTION_TEMPLATE


def test_final_spec_audit_prompt_requires_fixed_json_contract() -> None:
    rendered = FINAL_SPEC_AUDIT_TASK.format(original_goal="goal")
    retry_rendered = FINAL_SPEC_AUDIT_RETRY_TASK.format(
        original_goal="goal",
        remaining_gaps="- gap",
    )
    assert '"status": "PASS" | "GAP_FIXED" | "FAIL"' in rendered
    assert '"fixes_applied"' in rendered
    assert '"remaining_gaps"' in rendered
    assert '"requirement_matrix"' in rendered
    assert "Anti-examples" in rendered
    assert 'Если линтер не запускался — верни `"lint": []`.' in rendered
    assert 'вернуть top-level объект вида `{"command": "..."}`' in rendered
    assert "верни итоговый audit JSON со" in rendered
    assert "`status=FAIL`" in rendered
    assert "Тесты зависли. Завершу их и запущу заново." in rendered
    assert "Даже если проверка снова заблокирована" in retry_rendered


def test_review_normalize_prompt_forbids_action_objects() -> None:
    assert "Верни только итоговый ReviewResult JSON" in REVIEW_NORMALIZE_SYSTEM
    assert 'Нельзя возвращать top-level payload вида `{"command": "..."}`' in REVIEW_NORMALIZE_SYSTEM
    assert '{"pattern": "...", "path": "..."}' in REVIEW_NORMALIZE_SYSTEM
    assert '{"path": "tests/test_miniapp_rc_settings_put.py", "offset": 1, "limit": 200}' in REVIEW_NORMALIZE_SYSTEM
    assert "approved" in REVIEW_NORMALIZE_SYSTEM
    assert "summary" in REVIEW_NORMALIZE_SYSTEM
    assert "comments" in REVIEW_NORMALIZE_SYSTEM


def test_manager_yaml_prompts_keep_review_and_final_audit_contract_hardening() -> None:
    prompts_yaml = (ROOT / "modes" / "manager" / "prompts.yaml").read_text(encoding="utf-8")
    system_prompts_yaml = yaml.safe_load(
        (ROOT / "modes" / "manager" / "system_prompts.yaml").read_text(encoding="utf-8")
    )
    final_spec_audit_task = str(system_prompts_yaml["prompts"]["final_spec_audit_task"]).format(
        original_goal="goal"
    )
    final_spec_audit_retry_task = str(
        system_prompts_yaml["prompts"]["final_spec_audit_retry_task"]
    ).format(
        original_goal="goal",
        remaining_gaps="- gap",
    )
    assert "Верни только итоговый ReviewResult JSON" in prompts_yaml
    assert 'Нельзя возвращать top-level payload вида `{"command": "..."}`' in prompts_yaml
    assert 'Плохо: `{"pattern": "unfinished markers", "path": "file.py"}`.' in prompts_yaml
    assert 'Плохо: `{"path": "tests/test_miniapp_rc_settings_put.py", "offset": 1, "limit": 200}`.' in prompts_yaml
    assert 'Если линтер не запускался — верни `"lint": []`.' in final_spec_audit_task
    assert 'вернуть top-level объект вида `{"command": "..."}` вместо итогового audit JSON' in final_spec_audit_task
    assert "верни итоговый audit JSON со" in final_spec_audit_task
    assert "`status=FAIL`" in final_spec_audit_task
    assert "Даже если проверка снова заблокирована" in final_spec_audit_retry_task
