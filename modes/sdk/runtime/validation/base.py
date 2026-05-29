from __future__ import annotations

import abc
import dataclasses
import shutil
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional


class LanguageStack(str, Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"
    CPP = "cpp"


class ValidationStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    ERROR = "error"
    NOT_RUN = "not_run"


@dataclasses.dataclass
class ValidationIssue:
    code: str
    message: str
    path: Optional[str] = None
    severity: str = "error"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": str(self.code or ""),
            "message": str(self.message or ""),
            "path": str(self.path or "") if self.path else None,
            "severity": str(self.severity or "error"),
        }


@dataclasses.dataclass
class ValidationStepResult:
    tool: str
    command: List[str]
    exit_code: int
    output: str
    status: ValidationStatus
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": str(self.tool or ""),
            "command": [str(x) for x in (self.command or [])],
            "exit_code": int(self.exit_code),
            "output": str(self.output or ""),
            "status": self.status.value,
            "duration_ms": int(self.duration_ms),
        }


@dataclasses.dataclass
class ValidationReport:
    stack: LanguageStack
    status: ValidationStatus
    steps: List[ValidationStepResult] = dataclasses.field(default_factory=list)
    issues: List[ValidationIssue] = dataclasses.field(default_factory=list)
    started_at: float = dataclasses.field(default_factory=time.time)
    finished_at: float = dataclasses.field(default_factory=time.time)
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stack": self.stack.value,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "issues": [i.to_dict() for i in self.issues],
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "metadata": dict(self.metadata or {}),
        }


@dataclasses.dataclass(frozen=True)
class ToolchainCommand:
    tool: str
    command: List[str]
    optional: bool = False


ToolRunner = Callable[[List[str], str], Awaitable[Dict[str, Any]]]


class ValidationAdapter(abc.ABC):
    stack: LanguageStack

    @abc.abstractmethod
    def build_toolchain(self, workdir: str) -> List[ToolchainCommand]:
        """Return ordered toolchain commands for the provided workdir."""

    async def run(self, workdir: str, runner: ToolRunner) -> ValidationReport:
        started = time.time()
        steps: List[ValidationStepResult] = []
        issues: List[ValidationIssue] = []
        overall_status = ValidationStatus.OK

        for spec in self.build_toolchain(workdir):
            step_started = time.time()
            command = list(spec.command or [])
            tool_bin = str(command[0] if command else "").strip()
            if not tool_bin:
                steps.append(
                    ValidationStepResult(
                        tool=spec.tool,
                        command=command,
                        exit_code=-1,
                        output="toolchain command is empty",
                        status=ValidationStatus.NOT_RUN,
                        duration_ms=int((time.time() - step_started) * 1000),
                    )
                )
                if not spec.optional:
                    overall_status = ValidationStatus.FAILED
                issues.append(
                    ValidationIssue(
                        code=f"{spec.tool}_not_run",
                        message="toolchain command is empty",
                        severity="warning" if spec.optional else "error",
                    )
                )
                continue

            if "/" not in tool_bin and "\\" not in tool_bin and shutil.which(tool_bin) is None:
                steps.append(
                    ValidationStepResult(
                        tool=spec.tool,
                        command=command,
                        exit_code=-1,
                        output=f"toolchain not found: {tool_bin}",
                        status=ValidationStatus.NOT_RUN,
                        duration_ms=int((time.time() - step_started) * 1000),
                    )
                )
                if not spec.optional:
                    overall_status = ValidationStatus.FAILED
                issues.append(
                    ValidationIssue(
                        code=f"{spec.tool}_not_run",
                        message=f"toolchain not found: {tool_bin}",
                        severity="warning" if spec.optional else "error",
                    )
                )
                continue

            raw = await runner(command, workdir)
            exit_code = int(raw.get("exit_code", 1))
            output = str(raw.get("output") or raw.get("error") or "")
            step_status = ValidationStatus.OK if exit_code == 0 else ValidationStatus.FAILED
            steps.append(
                ValidationStepResult(
                    tool=spec.tool,
                    command=list(spec.command),
                    exit_code=exit_code,
                    output=output,
                    status=step_status,
                    duration_ms=int((time.time() - step_started) * 1000),
                )
            )
            if exit_code != 0:
                if not spec.optional:
                    overall_status = ValidationStatus.FAILED
                issues.append(
                    ValidationIssue(
                        code=f"{spec.tool}_failed",
                        message=output or f"{spec.tool} failed",
                        severity="warning" if spec.optional else "error",
                    )
                )

        return ValidationReport(
            stack=self.stack,
            status=overall_status,
            steps=steps,
            issues=issues,
            started_at=started,
            finished_at=time.time(),
        )
