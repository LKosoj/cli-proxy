from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    REPO_ROOT / "agent" / "manager.py",
    REPO_ROOT / "modes" / "manager" / "mode.py",
)


def _collect_status_assignments(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    lines.append(int(getattr(node, "lineno", 0) or 0))
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Attribute) and target.attr == "status":
                lines.append(int(getattr(node, "lineno", 0) or 0))
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Attribute) and target.attr == "status":
                lines.append(int(getattr(node, "lineno", 0) or 0))
    return lines


def test_manager_status_mutations_are_encapsulated_via_domain_methods() -> None:
    for path in TARGETS:
        lines = _collect_status_assignments(path)
        assert not lines, f"Direct status assignment found in {path}: {lines}"
    agent_source = (REPO_ROOT / "agent" / "manager.py").read_text(encoding="utf-8")
    agent_core_source = (REPO_ROOT / "agent" / "manager_core.py").read_text(encoding="utf-8")
    mode_source = (REPO_ROOT / "modes" / "manager" / "mode.py").read_text(encoding="utf-8")
    assert (".set_status(" in agent_source) or (".set_status(" in agent_core_source)
    assert ("._set_status(" in mode_source) or (".set_status(" in mode_source)
