from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROMPTS_PATH = ROOT / "modes" / "analyst" / "prompts.yaml"


def _load_prompts() -> tuple[str, dict]:
    text = PROMPTS_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return text, dict((data or {}).get("prompts") or {})


def test_analyst_prompts_yaml_removes_primary_source_wording() -> None:
    text, prompts = _load_prompts()
    assert "первичный источник контекста" not in text
    assert "первичный источник контекста" not in str(prompts.get("codebase_intro") or "")


def test_analyst_prompts_yaml_contains_repo_review_blocks_without_default_surface_boilerplate() -> None:
    _, prompts = _load_prompts()

    repo_consistency = str(prompts.get("repo_consistency_review_rules") or "")
    affected_surfaces = str(prompts.get("affected_surfaces_checklist") or "")

    assert "архитектурных или cross-surface изменениях" in repo_consistency
    assert "сверяй вывод между кодом, конфигами, документами и тестами" in repo_consistency

    normalized = " ".join(affected_surfaces.split())
    assert "фиксированный список" in normalized
    assert "по умолчанию" in normalized
    assert "- telegram" not in affected_surfaces.lower()
    assert "- desktop" not in affected_surfaces.lower()
    assert "- miniapp" not in affected_surfaces.lower()
