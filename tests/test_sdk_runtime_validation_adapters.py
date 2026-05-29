from __future__ import annotations

from typing import Dict, List

import pytest

from modes.sdk.runtime.validation import ValidationStatus
from modes.sdk.runtime.validation.adapters import (
    GoValidationAdapter,
    PythonValidationAdapter,
    TypeScriptValidationAdapter,
)


def test_python_adapter_builds_expected_toolchain() -> None:
    commands = PythonValidationAdapter().build_toolchain("/tmp")
    assert [c.tool for c in commands] == ["flake8", "pytest"]


def test_typescript_adapter_builds_expected_toolchain() -> None:
    commands = TypeScriptValidationAdapter().build_toolchain("/tmp")
    assert [c.tool for c in commands] == ["eslint", "jest"]


def test_go_adapter_builds_expected_toolchain() -> None:
    commands = GoValidationAdapter().build_toolchain("/tmp")
    assert len(commands) == 1
    assert commands[0].tool == "go_test"
    assert commands[0].command == ["go", "test", "./..."]


@pytest.mark.asyncio
async def test_missing_toolchain_is_reported_as_not_run(monkeypatch) -> None:
    monkeypatch.setattr("modes.sdk.runtime.validation.base.shutil.which", lambda _name: None)

    async def _runner(_command: List[str], _workdir: str) -> Dict[str, object]:
        raise AssertionError("runner should not be called when toolchain is missing")

    report = await PythonValidationAdapter().run("/tmp/project", _runner)

    assert report.status is ValidationStatus.FAILED
    assert report.steps[0].status is ValidationStatus.NOT_RUN
    assert report.steps[1].status is ValidationStatus.NOT_RUN
    assert report.steps[0].output.startswith("toolchain not found:")
    assert report.issues[0].code.endswith("_not_run")
