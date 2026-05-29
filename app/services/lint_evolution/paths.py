from __future__ import annotations

import hashlib
import os
from pathlib import Path

from utils.paths import cli_proxy_artifact_path

_LINT_DIR = "lint_evolution"


def lint_root(workdir: str) -> Path:
    return Path(cli_proxy_artifact_path(workdir, _LINT_DIR))


def state_path(workdir: str) -> Path:
    return lint_root(workdir) / "state.json"


def db_path(workdir: str) -> Path:
    return lint_root(workdir) / "evolution.db"


def signals_jsonl_path(workdir: str) -> Path:
    return lint_root(workdir) / "signals.jsonl"


def rules_dir(workdir: str) -> Path:
    return lint_root(workdir) / "rules"


def candidates_dir(workdir: str) -> Path:
    return lint_root(workdir) / "candidates"


def schemas_dir(workdir: str) -> Path:
    return lint_root(workdir) / "schemas"


def reports_dir(workdir: str) -> Path:
    return lint_root(workdir) / "reports"


def autopause_path(workdir: str) -> Path:
    return lint_root(workdir) / "autopause.json"


def project_id_for(workdir: str) -> str:
    """Stable per-project identifier derived from absolute workdir."""
    real = os.path.realpath(str(workdir or "")) or "default"
    return hashlib.md5(real.encode("utf-8")).hexdigest()[:12]


def ensure_directories(workdir: str) -> Path:
    root = lint_root(workdir)
    for sub in (root, rules_dir(workdir), candidates_dir(workdir), schemas_dir(workdir), reports_dir(workdir)):
        sub.mkdir(parents=True, exist_ok=True)
    return root
