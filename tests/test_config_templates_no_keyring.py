from __future__ import annotations

from pathlib import Path


def test_config_templates_do_not_contain_keyring_placeholders() -> None:
    """
    После удаления keyring в проекте не должно оставаться плейсхолдеров
    вида '__keyring__:*' в конфиг-шаблонах.
    """
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "config_example.yaml",  # tracked template
        repo_root / "config.yaml",          # local template (may be gitignored)
    ]

    for path in candidates:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        assert "__keyring__" not in content
