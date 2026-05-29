from __future__ import annotations

import re
from pathlib import Path

from app.config_runtime.models import DefaultsConfigModel
from config import DefaultsConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
MIN_RESPONSE_TOKENS = 8000
PRODUCTION_GLOBS = (
    "agent/**/*.py",
    "app/**/*.py",
    "modes/**/*.py",
    "summary.py",
)
LIMIT_PATTERNS = (
    re.compile(r"\bmax_tokens\s*[=:]\s*([0-9][0-9_]*)"),
    re.compile(r"\bMAX_[A-Z0-9_]*TOKENS\s*=\s*([0-9][0-9_]*)"),
)


def test_production_llm_max_token_limits_are_at_least_8000() -> None:
    offenders = []
    for pattern in PRODUCTION_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for limit_pattern in LIMIT_PATTERNS:
                for match in limit_pattern.finditer(text):
                    value = int(match.group(1).replace("_", ""))
                    if value < MIN_RESPONSE_TOKENS:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{match.start()}={value}")

    assert offenders == []


def test_default_context_reserve_tokens_is_8000() -> None:
    assert DefaultsConfig(workdir=".").context_reserve_tokens == MIN_RESPONSE_TOKENS
    assert DefaultsConfigModel(workdir=".").context_reserve_tokens == MIN_RESPONSE_TOKENS
