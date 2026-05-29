import pytest
import yaml

from app.services.project_prompts_service import (
    InvalidProjectPromptsError,
    ensure_project_prompts,
    load_mode_prompts,
)


def test_ensure_project_prompts_creates_required_files_in_empty_workdir(tmp_path):
    workdir = str(tmp_path)

    loaded = ensure_project_prompts(workdir)

    for mode_id in ("manager", "webmaster"):
        prompt_dir = tmp_path / ".cli-proxy" / f".{mode_id}" / "prompt"
        prompts_path = prompt_dir / "prompts.yaml"
        learning_path = prompt_dir / "learning.yaml"
        assert prompts_path.exists()
        assert learning_path.exists()

        prompts = load_mode_prompts(workdir, mode_id)
        assert isinstance(prompts, dict)
        assert isinstance(prompts.get("prompts"), dict)
        assert loaded.get(mode_id) == prompts

    manager_prompts = load_mode_prompts(workdir, "manager")["prompts"]
    assert "resume_question_template" in manager_prompts
    assert "decompose_instruction" in manager_prompts
    assert "decision_system" in manager_prompts
    assert "work_type_classifier_system" in manager_prompts
    assert "Обязательные поля ответа: work_type, confidence, reason." in manager_prompts["work_type_classifier_system"]
    assert "reason: короткая строка с объяснением выбора work_type." in manager_prompts["work_type_classifier_system"]
    manager_prompts_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"
    manager_file_payload = yaml.safe_load(manager_prompts_path.read_text(encoding="utf-8")) or {}
    manager_file_prompts = manager_file_payload.get("prompts") if isinstance(manager_file_payload, dict) else {}
    if not isinstance(manager_file_prompts, dict):
        manager_file_prompts = {}
    assert "decompose_instruction" in manager_file_prompts
    assert "resume_question_template" not in manager_file_prompts
    assert "decompose_instruction: |" in manager_prompts_path.read_text(encoding="utf-8")

    webmaster_prompts = load_mode_prompts(workdir, "webmaster")["prompts"]
    assert "system_base" in webmaster_prompts
    assert "validation_task" in webmaster_prompts


def test_ensure_project_prompts_raises_on_invalid_prompts_yaml(tmp_path):
    workdir = str(tmp_path)
    ensure_project_prompts(workdir)

    bad_prompts = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"
    bad_prompts.write_text("prompts: [broken", encoding="utf-8")

    with pytest.raises(InvalidProjectPromptsError):
        ensure_project_prompts(workdir)


def test_ensure_project_prompts_rejects_system_keys_inside_manager_project_yaml(tmp_path):
    workdir = str(tmp_path)
    ensure_project_prompts(workdir)

    prompts_path = tmp_path / ".cli-proxy" / ".manager" / "prompt" / "prompts.yaml"
    payload = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
    prompts = payload.get("prompts") if isinstance(payload, dict) else {}
    if not isinstance(prompts, dict):
        prompts = {}
    prompts["resume_question_template"] = "SHOULD_NOT_BE_HERE"
    prompts_path.write_text(
        yaml.safe_dump({"prompts": prompts}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(InvalidProjectPromptsError):
        ensure_project_prompts(workdir)


def test_ensure_project_prompts_allows_webmaster_without_intent_analysis(tmp_path):
    workdir = str(tmp_path)
    ensure_project_prompts(workdir)

    prompts_path = tmp_path / ".cli-proxy" / ".webmaster" / "prompt" / "prompts.yaml"
    payload = yaml.safe_load(prompts_path.read_text(encoding="utf-8")) or {}
    prompts = payload.get("prompts") if isinstance(payload, dict) else {}
    if not isinstance(prompts, dict):
        prompts = {}
    prompts.pop("intent_analysis", None)
    prompts_path.write_text(
        yaml.safe_dump({"prompts": prompts}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    loaded = ensure_project_prompts(workdir)
    webmaster_prompts = loaded["webmaster"]["prompts"]
    assert "system_base" in webmaster_prompts
    assert "validation_task" in webmaster_prompts
    assert "intent_analysis" not in webmaster_prompts
