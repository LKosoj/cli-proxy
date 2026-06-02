"""Tests for modes/sdd/constitution.py and _with_invariant_policy injection."""
from __future__ import annotations

from pathlib import Path
import types

import pytest

from app.services.project_prompts_service import (
    ensure_project_prompts,
    load_mode_prompt_texts,
)
from agent.manager import ManagerOrchestrator
from modes.sdd.constitution import _MAX_CONSTITUTION_CHARS, load_constitution


# ---------------------------------------------------------------------------
# load_constitution unit tests
# ---------------------------------------------------------------------------


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not available on this filesystem")


def test_load_constitution_returns_content_when_file_exists(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "constitution.md").write_text(
        "# Constitution\n## Article I\nSome rule.", encoding="utf-8"
    )
    result = load_constitution(str(tmp_path))
    assert "# Constitution" in result
    assert "Some rule." in result


def test_load_constitution_returns_empty_when_file_missing(tmp_path) -> None:
    result = load_constitution(str(tmp_path))
    assert result == ""


def test_load_constitution_returns_empty_when_workdir_is_empty_string() -> None:
    result = load_constitution("")
    assert result == ""


def test_load_constitution_returns_empty_when_workdir_is_none() -> None:
    result = load_constitution(None)  # type: ignore[arg-type]
    assert result == ""


def test_load_constitution_returns_empty_on_unreadable_path(tmp_path) -> None:
    # Point to a path that exists as a directory (not a file) — unreadable as text.
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "constitution.md").mkdir()  # directory, not a file
    result = load_constitution(str(tmp_path))
    assert result == ""


def test_load_constitution_ignores_symlink_outside_workdir(tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-constitution.md"
    outside.write_text("SECRET OUTSIDE\n", encoding="utf-8")
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    _symlink_or_skip(cli_proxy_dir / "constitution.md", outside)

    result = load_constitution(str(tmp_path))

    assert result == ""


def test_load_constitution_limits_content_size(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "constitution.md").write_text(
        "x" * (_MAX_CONSTITUTION_CHARS + 100),
        encoding="utf-8",
    )

    result = load_constitution(str(tmp_path))

    assert len(result) == _MAX_CONSTITUTION_CHARS


def test_load_constitution_preserves_curly_braces_in_content(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    content = '## Article\nExample: {"key": "value"} and {placeholder}'
    (cli_proxy_dir / "constitution.md").write_text(content, encoding="utf-8")
    result = load_constitution(str(tmp_path))
    assert '{"key": "value"}' in result
    assert "{placeholder}" in result


# ---------------------------------------------------------------------------
# _with_invariant_policy tests
# ---------------------------------------------------------------------------


def _make_orchestrator() -> ManagerOrchestrator:
    """Instantiate ManagerOrchestrator without full config."""
    obj = object.__new__(ManagerOrchestrator)
    obj._config = types.SimpleNamespace(defaults=types.SimpleNamespace())
    return obj


def test_invariant_policy_injects_constitution_into_prompt(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "constitution.md").write_text(
        "# Constitution\n## Article I\nRule one.", encoding="utf-8"
    )
    ensure_project_prompts(str(tmp_path))

    orch = _make_orchestrator()
    orch._manager_prompt = (  # type: ignore[method-assign]
        lambda _wd, _key: "{constitution}"
    )

    result = orch._with_invariant_policy(str(tmp_path), "base text")
    assert "Rule one." in result
    assert "base text" in result


def test_invariant_policy_falls_back_to_base_when_constitution_missing(tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))

    orch = _make_orchestrator()
    orch._manager_prompt = (  # type: ignore[method-assign]
        lambda _wd, _key: "{constitution}"
    )

    result = orch._with_invariant_policy(str(tmp_path), "base only")
    # constitution is empty → policy becomes empty → return base
    assert result == "base only"


def test_invariant_policy_curly_braces_in_content_do_not_raise(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    tricky = '## Article\nJSON example: {"user_goal": "foo", "key": "val"}'
    (cli_proxy_dir / "constitution.md").write_text(tricky, encoding="utf-8")
    ensure_project_prompts(str(tmp_path))

    orch = _make_orchestrator()
    orch._manager_prompt = (  # type: ignore[method-assign]
        lambda _wd, _key: "{constitution}"
    )

    # Must not raise even though constitution contains {user_goal}
    result = orch._with_invariant_policy(str(tmp_path), "base")
    assert '{"user_goal": "foo"' in result


def test_invariant_policy_empty_policy_after_substitution_returns_base(tmp_path) -> None:
    # constitution.md doesn't exist → load_constitution returns ""
    # policy_template is "{constitution}" → after replace → "" → empty → return base
    ensure_project_prompts(str(tmp_path))

    orch = _make_orchestrator()
    orch._manager_prompt = (  # type: ignore[method-assign]
        lambda _wd, _key: "{constitution}"
    )

    result = orch._with_invariant_policy(str(tmp_path), "just base")
    assert result == "just base"


# ---------------------------------------------------------------------------
# Regression: РЕАЛЬНЫЙ системный шаблон invariant_policy (не застабленный)
# обязан сохранять базовые guardrail'ы Менеджера даже без constitution.md.
# ---------------------------------------------------------------------------


def test_real_template_keeps_base_rules_without_constitution(tmp_path) -> None:
    ensure_project_prompts(str(tmp_path))
    policy_template = load_mode_prompt_texts(str(tmp_path), "manager")["invariant_policy"]
    # Базовые правила на месте + плейсхолдер конституции до подстановки.
    assert "Invariant Policy" in policy_template
    assert "not_done" in policy_template
    assert "{constitution}" in policy_template


def test_with_invariant_policy_real_template_no_constitution_keeps_base_rules(tmp_path) -> None:
    # constitution.md отсутствует — но базовые правила Менеджера НЕ должны теряться.
    ensure_project_prompts(str(tmp_path))
    assert load_constitution(str(tmp_path)) == ""

    orch = _make_orchestrator()  # _manager_prompt НЕ застаблен — реальная загрузка шаблона
    result = orch._with_invariant_policy(str(tmp_path), "BASE PROMPT")

    assert "BASE PROMPT" in result
    assert "not_done" in result
    # Пустой плейсхолдер не должен протечь в итоговый prompt.
    assert "{constitution}" not in result


def test_with_invariant_policy_real_template_with_constitution_has_both(tmp_path) -> None:
    cli_proxy_dir = tmp_path / ".cli-proxy"
    cli_proxy_dir.mkdir()
    (cli_proxy_dir / "constitution.md").write_text(
        "# Constitution\n## Article I\nProject-specific rule X.", encoding="utf-8"
    )
    ensure_project_prompts(str(tmp_path))

    orch = _make_orchestrator()
    result = orch._with_invariant_policy(str(tmp_path), "BASE PROMPT")

    # И базовые правила, и конституция присутствуют.
    assert "not_done" in result
    assert "Project-specific rule X." in result
    assert "BASE PROMPT" in result
