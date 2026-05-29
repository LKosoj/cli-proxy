from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_MODE_PATH = REPO_ROOT / "modes" / "admin" / "mode.py"


def _load_tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _find_class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"Class {name} not found")


def _class_has_mode_id_literal(cls: ast.ClassDef, expected: str) -> bool:
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "mode_id":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value == expected:
                        return True
    return False


def test_admin_mode_inherits_base_mode() -> None:
    tree = _load_tree(ADMIN_MODE_PATH)
    cls = _find_class(tree, "AdminMode")
    base_names = {
        base.id
        for base in cls.bases
        if isinstance(base, ast.Name)
    } | {
        base.attr
        for base in cls.bases
        if isinstance(base, ast.Attribute)
    }
    assert "BaseMode" in base_names
    assert _class_has_mode_id_literal(cls, "admin")
