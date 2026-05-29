from __future__ import annotations

from typing import List

from ..base import LanguageStack, ToolchainCommand, ValidationAdapter


class TypeScriptValidationAdapter(ValidationAdapter):
    stack = LanguageStack.TYPESCRIPT

    def build_toolchain(self, _workdir: str) -> List[ToolchainCommand]:
        return [
            ToolchainCommand(tool="eslint", command=["npx", "eslint", "."], optional=False),
            ToolchainCommand(tool="jest", command=["npx", "jest", "--runInBand"], optional=False),
        ]
