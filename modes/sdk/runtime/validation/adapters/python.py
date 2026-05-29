from __future__ import annotations

from typing import List

from ..base import LanguageStack, ToolchainCommand, ValidationAdapter


class PythonValidationAdapter(ValidationAdapter):
    stack = LanguageStack.PYTHON

    def build_toolchain(self, _workdir: str) -> List[ToolchainCommand]:
        return [
            ToolchainCommand(tool="flake8", command=["flake8"], optional=False),
            ToolchainCommand(tool="pytest", command=["pytest", "-q"], optional=False),
        ]
