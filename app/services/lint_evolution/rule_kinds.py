from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

UNKNOWN = "__unknown__"

CANONICAL_RULE_KINDS: tuple[str, ...] = (
    "tests_failing",
    "syntax_error",
    "missing_implementation",
    "no_evidence_provided",
    "unused_imports",
    "circular_dependency",
    "dead_code_present",
    "test_timeout",
    "acceptance_criteria_unmet",
    "logic_error",
    UNKNOWN,
)


@dataclass(frozen=True)
class KindPattern:
    kind: str
    pattern: re.Pattern[str]


def _compile(rules: Iterable[tuple[str, str]]) -> tuple[KindPattern, ...]:
    return tuple(KindPattern(kind=k, pattern=re.compile(p, re.IGNORECASE | re.UNICODE)) for k, p in rules)


# Order matters: more specific patterns first.
PATTERNS: tuple[KindPattern, ...] = _compile(
    [
        (
            "test_timeout",
            r"(timeout|таймаут)\s+(?:при|при запуск|of)\s+тест|pytest\s+(был|was)\s+(убит|killed)",
        ),
        ("tests_failing", r"тест[ыа]?\s+(не\s+проход|пада|fail)|test\s+(failed|fails|not pass)|fail(ing|ed)\s+test"),
        ("syntax_error", r"(syntax\s*error|ошибк[ауи]?\s+синтаксис|unmatched\s+[\(\)\[\]\{\}]|invalid\s+syntax)"),
        ("unused_imports", r"(unused\s+import|неиспользуемы[еих]?\s+импорт|removed?\s+(unused\s+)?import)"),
        ("circular_dependency", r"(circular\s+(import|dependency)|циклическ\w*\s+импорт|recursive\s+import)"),
        (
            "missing_implementation",
            r"(заглушк[аи]\s+|stub|placeholder|notimplementederror|#\s*(todo|fixme)|pass\s*#\s*(todo|fixme))",
        ),
        ("no_evidence_provided", r"(git\s+status\s+пуст|changed_files\s+отсутств|нет\s+(изменений|доказательств)|empty\s+diff)"),
        ("dead_code_present", r"(устаревш\w+\s+файл|удалены?\s+(стары|устаревш)|deprecated\s+(file|files|code)|dead\s+code)"),
        (
            "acceptance_criteria_unmet",
            r"(не\s+соответствует\s+требовани|requirement\s+not\s+met|acceptance\s+criteria|критер\w+\s+приёмки)",
        ),
        ("logic_error", r"(логическ\w*\s+ошибк|incorrect\s+(logic|behaviour|behavior)|wrong\s+result|потер\w+\s+запрос)"),
    ]
)
