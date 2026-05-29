from __future__ import annotations

from typing import List

from ..base import LanguageStack, ToolchainCommand, ValidationAdapter


class GoValidationAdapter(ValidationAdapter):
    stack = LanguageStack.GO

    def build_toolchain(self, _workdir: str) -> List[ToolchainCommand]:
        return [
            ToolchainCommand(tool="go_test", command=["go", "test", "./..."], optional=False),
        ]
