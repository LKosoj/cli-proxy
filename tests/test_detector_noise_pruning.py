from __future__ import annotations

from pathlib import Path

import pytest

from modes.sdk.runtime.validation.detector import _iter_project_files, detect_stacks


def _touch(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Шумовые директории не обходятся
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("noise_dir", ["node_modules", ".venv", ".git"])
def test_noise_dir_not_traversed(tmp_path: Path, noise_dir: str) -> None:
    """Файлы внутри шумовых директорий не должны появляться в _iter_project_files."""
    # Файл внутри шумовой директории
    _touch(tmp_path / noise_dir / "subdir" / "package.json", "{}")
    # Файл в корне (должен быть виден)
    _touch(tmp_path / "pyproject.toml", "[project]")

    found_files = [name for name, _ in _iter_project_files(str(tmp_path))]

    assert "pyproject.toml" in found_files, "Корневой файл должен быть найден"
    assert "package.json" not in found_files, f"Файл из {noise_dir}/ не должен обходиться"


def test_noise_dir_does_not_cause_deep_stack(tmp_path: Path) -> None:
    """Глубокое дерево в node_modules не вызывает переполнения стека и не возвращает стеки."""
    # Создаём глубокое дерево без реального содержимого
    deep = tmp_path / "node_modules"
    level = deep
    for i in range(10):
        level = level / f"pkg{i}"
    _touch(level / "index.js", "// js")

    # Единственный реальный файл
    _touch(tmp_path / "requirements.txt", "pytest")

    stacks = detect_stacks(str(tmp_path))

    # Python должен быть найден
    from modes.sdk.runtime.validation.base import LanguageStack
    assert LanguageStack.PYTHON in stacks
    # JavaScript из node_modules не должен быть найден
    assert LanguageStack.JAVASCRIPT not in stacks


def test_venv_files_not_detected_as_stack(tmp_path: Path) -> None:
    """.venv/lib/python3.x/site-packages/... не влияет на детектирование стека."""
    _touch(tmp_path / ".venv" / "lib" / "python3.11" / "site-packages" / "some_pkg" / "setup.py", "")
    _touch(tmp_path / "go.mod", "module demo")

    from modes.sdk.runtime.validation.base import LanguageStack
    stacks = detect_stacks(str(tmp_path))

    assert LanguageStack.GO in stacks
    # Python не должен детектироваться только из .venv
    assert LanguageStack.PYTHON not in stacks
