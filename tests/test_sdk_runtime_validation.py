from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

from modes.sdk.runtime.validation import (
    LanguageStack,
    ToolchainCommand,
    ValidationAdapter,
    ValidationStatus,
    detect_stacks,
)


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_detect_stacks_identifies_all_required_languages(tmp_path: Path) -> None:
    _touch(tmp_path / "pyproject.toml", "[project]\nname='x'")
    _touch(tmp_path / "frontend" / "package.json", "{}")
    _touch(tmp_path / "frontend" / "tsconfig.json", "{}")
    _touch(tmp_path / "backend" / "go.mod", "module demo")
    _touch(tmp_path / "native" / "Cargo.toml", "[package]\nname='x'")
    _touch(tmp_path / "cpp" / "CMakeLists.txt", "cmake_minimum_required(VERSION 3.16)")

    stacks = detect_stacks(str(tmp_path))

    assert set(stacks) == {
        LanguageStack.PYTHON,
        LanguageStack.JAVASCRIPT,
        LanguageStack.TYPESCRIPT,
        LanguageStack.GO,
        LanguageStack.RUST,
        LanguageStack.CPP,
    }


@pytest.mark.asyncio
async def test_validation_adapter_run_returns_consistent_report() -> None:
    class DummyAdapter(ValidationAdapter):
        stack = LanguageStack.PYTHON

        def build_toolchain(self, _workdir: str) -> List[ToolchainCommand]:
            return [
                ToolchainCommand(tool="flake8", command=["flake8"], optional=False),
                ToolchainCommand(tool="pytest", command=["pytest", "-q"], optional=True),
            ]

    async def _runner(command: List[str], _workdir: str) -> Dict[str, object]:
        if command[0] == "flake8":
            return {"exit_code": 0, "output": "ok"}
        return {"exit_code": 1, "output": "tests failed"}

    report = await DummyAdapter().run("/tmp/project", _runner)

    assert report.stack is LanguageStack.PYTHON
    assert report.status is ValidationStatus.OK
    assert len(report.steps) == 2
    assert report.steps[0].tool == "flake8"
    assert report.steps[1].tool == "pytest"
    assert report.steps[1].status is ValidationStatus.FAILED
    assert report.issues and report.issues[0].severity == "warning"
    serialized = report.to_dict()
    assert serialized["stack"] == "python"
    assert serialized["steps"][1]["status"] == "failed"
