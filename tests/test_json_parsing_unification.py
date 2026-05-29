from __future__ import annotations

import re
from pathlib import Path


_JSON_LOADS_PATTERN = re.compile(r"\bjson\.loads\(")


def test_no_direct_json_loads_outside_json_normalizer() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    allowlist = {
        repo_root / "modes" / "sdk" / "runtime" / "json_normalizer.py",
    }
    violations: list[str] = []

    for path in repo_root.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        if "tests" in path.parts:
            continue
        if path in allowlist:
            continue
        text = path.read_text(encoding="utf-8")
        if _JSON_LOADS_PATTERN.search(text):
            violations.append(str(path.relative_to(repo_root)))

    assert not violations, (
        "Direct json.loads is forbidden outside modes/sdk/runtime/json_normalizer.py. "
        f"Use loads_safe/parse_normalize_validate instead. Violations: {violations}"
    )
