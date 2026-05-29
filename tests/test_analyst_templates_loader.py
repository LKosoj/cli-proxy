import logging
import os
import time

import pytest

from modes.analyst.template_service import default_templates_path, get_analyst_templates_cached, load_analyst_templates


def _write_yaml(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def test_get_analyst_templates_cached_reloads_on_mtime_change(tmp_path):
    p = tmp_path / "analyst_config.yaml"

    _write_yaml(
        str(p),
        """\
templates:
  default:
    name: "T1"
    description: "D1"
    required_sections: ["A"]
    qa_prompt: "Q1"
""",
    )

    t1 = get_analyst_templates_cached(str(p))
    assert t1["default"]["name"] == "T1"
    assert t1["default"]["required_sections"] == ["A"]

    _write_yaml(
        str(p),
        """\
templates:
  default:
    name: "T2"
    description: "D2"
    required_sections: ["B", "C"]
    qa_prompt: "Q2"
""",
    )

    # Force mtime to move forward even on coarse filesystems.
    now = time.time()
    os.utime(str(p), (now + 2, now + 2))

    t2 = get_analyst_templates_cached(str(p))
    assert t2["default"]["name"] == "T2"
    assert t2["default"]["required_sections"] == ["B", "C"]


def test_get_analyst_templates_cached_preserves_supported_optional_fields(tmp_path):
    p = tmp_path / "analyst_config.yaml"

    _write_yaml(
        str(p),
        """\
templates:
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
    compose_mode: "template_first"
    output_kind: "spec"
    artifact_preferred: true
    min_user_scenarios: 3
    min_functional_requirements: "12"
    min_nfr: 5
    min_api_contracts: 2
    min_acceptance_checks: 9
    target_size_hint: "large"
    repo_grounded_required: true
    repo_audit_required: true
    final_repo_review_required: false
""",
    )

    templates = get_analyst_templates_cached(str(p))
    template = templates["change_spec"]

    assert template["compose_mode"] == "template_first"
    assert template["output_kind"] == "spec"
    assert template["artifact_preferred"] is True
    assert template["min_user_scenarios"] == 3
    assert template["min_functional_requirements"] == 12
    assert template["min_nfr"] == 5
    assert template["min_api_contracts"] == 2
    assert template["min_acceptance_checks"] == 9
    assert template["target_size_hint"] == "large"
    assert template["repo_grounded_required"] is True
    assert template["repo_audit_required"] is True
    assert template["final_repo_review_required"] is False
    assert template["protected_spec_shell"]["title"] == "Техническое задание"
    assert template["protected_spec_shell"]["source_task_section"] == "Исходная задача"
    assert template["protected_spec_shell"]["core_sections"] == ["A"]
    assert template["protected_spec_shell"]["open_questions_section"] == "Открытые вопросы и валидационные шаги"
    assert template["protected_spec_shell"]["external_references_section"] == "Внешние референсы и примеры реализации"
    assert template["protected_spec_shell"]["external_references_conditional"] is True


def test_load_analyst_templates_adds_protected_shell_only_for_spec_templates(tmp_path):
    p = tmp_path / "analyst_config.yaml"

    _write_yaml(
        str(p),
        """\
templates:
  default:
    name: "Analysis"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  change_spec:
    name: "Spec"
    description: "D"
    required_sections: ["Контекст", "Изменения"]
    qa_prompt: "Q"
    output_kind: "spec"
    protected_spec_shell:
      title: "Техническое задание"
      source_task_section: "Исходная задача"
      open_questions_section: "Открытые вопросы и валидационные шаги"
      external_references_section: "Внешние референсы и примеры реализации"
      external_references_conditional: true
""",
    )

    templates = load_analyst_templates(str(p))

    assert "protected_spec_shell" not in templates["default"]
    assert templates["change_spec"]["protected_spec_shell"] == {
        "title": "Техническое задание",
        "source_task_section": "Исходная задача",
        "core_sections": ["Контекст", "Изменения"],
        "open_questions_section": "Открытые вопросы и валидационные шаги",
        "external_references_section": "Внешние референсы и примеры реализации",
        "external_references_conditional": True,
    }


def test_load_analyst_templates_logs_diagnostic_for_invalid_custom_entry(tmp_path, caplog):
    p = tmp_path / "analyst_config.yaml"

    _write_yaml(
        str(p),
        """\
templates:
  default:
    name: "Analysis"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  broken_spec:
    name: "Broken"
    description: "D"
    required_sections: ["A"]
""",
    )

    with caplog.at_level(logging.WARNING, logger="modes.analyst.template_service"):
        templates = load_analyst_templates(str(p))

    assert "default" in templates
    assert "broken_spec" not in templates
    assert "[analyst-template-loader] invalid template skipped template_id=broken_spec" in caplog.text
    assert "missing_or_invalid_fields=qa_prompt" in caplog.text


def test_load_analyst_templates_fail_fast_for_invalid_mandatory_bundled_template(tmp_path, monkeypatch, caplog):
    p = tmp_path / "analyst_config.yaml"

    _write_yaml(
        str(p),
        """\
templates:
  default:
    name: "Default"
    description: "D"
    required_sections: ["A"]
  new_spec:
    name: "New"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  change_spec:
    name: "Change"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  refactor_spec:
    name: "Refactor"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  integration_change_spec:
    name: "Integration"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  narrow_backend_change_spec:
    name: "Backend"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
  audit:
    name: "Audit"
    description: "D"
    required_sections: ["A"]
    qa_prompt: "Q"
""",
    )
    monkeypatch.setattr("modes.analyst.template_service.default_templates_path", lambda: str(p))

    with caplog.at_level(logging.WARNING, logger="modes.analyst.template_service"):
        with pytest.raises(
            RuntimeError,
            match="Mandatory analyst templates invalid or missing: default:missing_or_invalid_fields=qa_prompt",
        ):
            load_analyst_templates(str(p))

    assert "[analyst-template-loader] invalid template skipped template_id=default" in caplog.text
    assert "[analyst-template-loader] mandatory templates invalid_or_missing" in caplog.text


def test_default_analyst_config_spec_templates_parse_with_template_first_metadata():
    templates = load_analyst_templates(default_templates_path())

    new_spec = templates["new_spec"]
    change_spec = templates["change_spec"]
    refactor_spec = templates["refactor_spec"]

    assert new_spec["compose_mode"] == "template_first"
    assert new_spec["output_kind"] == "spec"
    assert new_spec["artifact_preferred"] is True
    assert new_spec["target_size_hint"] == "large"
    assert isinstance(new_spec["traceability_rules"], list)
    assert len(new_spec["traceability_rules"]) >= 3
    assert isinstance(new_spec["required_inputs"], list)
    assert len(new_spec["required_inputs"]) >= 2

    assert change_spec["compose_mode"] == "template_first"
    assert change_spec["output_kind"] == "spec"
    assert change_spec["artifact_preferred"] is False
    assert change_spec["target_size_hint"] == "medium"
    assert isinstance(change_spec["traceability_rules"], list)
    assert len(change_spec["traceability_rules"]) >= 2
    assert change_spec["repo_grounded_required"] is True
    assert change_spec["repo_audit_required"] is True
    assert change_spec["final_repo_review_required"] is True
    assert "Подтвержденные факты и источники" in change_spec["required_sections"]
    assert change_spec["protected_spec_shell"]["title"] == "Техническое задание"
    assert change_spec["protected_spec_shell"]["core_sections"] == change_spec["required_sections"]

    assert refactor_spec["compose_mode"] == "template_first"
    assert refactor_spec["output_kind"] == "spec"
    assert refactor_spec["artifact_preferred"] is True
    assert refactor_spec["target_size_hint"] == "large"
    assert isinstance(refactor_spec["traceability_rules"], list)
    assert len(refactor_spec["traceability_rules"]) >= 2
    assert refactor_spec["repo_grounded_required"] is True
    assert refactor_spec["repo_audit_required"] is True
    assert refactor_spec["final_repo_review_required"] is True
    assert "Изменения persistence/state/config" in refactor_spec["required_sections"]
    assert "Подтвержденные факты и источники" in refactor_spec["required_sections"]
    assert "Startup/restore/reconcile логика" in refactor_spec["required_sections"]
    assert "low-middle разработчик" in refactor_spec["qa_prompt"]
    assert refactor_spec["protected_spec_shell"]["source_task_section"] == "Исходная задача"
    assert refactor_spec["protected_spec_shell"]["open_questions_section"] == "Открытые вопросы и валидационные шаги"

    assert (
        templates["integration_change_spec"]["protected_spec_shell"]["core_sections"]
        == templates["integration_change_spec"]["required_sections"]
    )
    assert (
        templates["narrow_backend_change_spec"]["protected_spec_shell"]["external_references_section"]
        == "Внешние референсы и примеры реализации"
    )
    assert "protected_spec_shell" not in templates["default"]
