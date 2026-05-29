from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .rules_store import Rule, load_rules

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    rule_kind: str
    file: str
    line: int
    snippet: str
    severity: str = "warning"


@dataclass
class GateResult:
    findings: list[Finding] = field(default_factory=list)
    rules_evaluated: int = 0
    files_scanned: int = 0
    skipped_rules: list[str] = field(default_factory=list)


class LintGateService:
    """Run active regex-detectors over a set of files.

    Mirrors the AnalystGateService interface idea: stateless, one project root,
    no direct session/bot dependencies. Non-regex detector_types are accumulated
    in skipped_rules — they will be supported by future runners (ast/shell).
    """

    def __init__(self, workdir: str, *, project_root: Path) -> None:
        self._workdir = workdir
        self._project_root = project_root

    def run_on_files(self, paths: Iterable[Path]) -> GateResult:
        active = [r for r in load_rules(self._workdir) if r.state == "active"]
        result = GateResult()
        regex_rules: list[tuple[Rule, re.Pattern[str]]] = []
        for rule in active:
            if rule.detector_type != "regex":
                result.skipped_rules.append(rule.id)
                continue
            pattern = rule.detector_payload.pattern
            if not pattern:
                result.skipped_rules.append(rule.id)
                continue
            try:
                regex_rules.append((rule, re.compile(pattern, re.MULTILINE)))
            except re.error as exc:
                logger.warning("lint_evolution.gate: bad regex in %s: %s", rule.id, exc)
                result.skipped_rules.append(rule.id)

        result.rules_evaluated = len(regex_rules)
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            result.files_scanned += 1
            rel = self._relpath(path)
            for rule, regex in regex_rules:
                for match in regex.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    snippet = match.group(0).splitlines()[0][:200]
                    result.findings.append(
                        Finding(
                            rule_id=rule.id,
                            rule_kind=rule.rule_kind,
                            file=rel,
                            line=line,
                            snippet=snippet,
                        )
                    )
        return result

    def _relpath(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._project_root.resolve()))
        except ValueError:
            return str(path)
