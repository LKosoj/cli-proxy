from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DISPUTED_FILES = (
    "SESSION.json",
    "miniapp.pid",
    "full_ui.yaml",
    "miniapp/package.json",
    "miniapp/package-lock.json",
    "skills-lock.json",
)


def _git_ls_files(*paths: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _git_check_ignore_no_index(*paths: str) -> set[str]:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *paths],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_runtime_artifact_gitignore_safeguards_are_explicit() -> None:
    ignored = _git_check_ignore_no_index(
        "SESSION.json",
        "miniapp.pid",
        "full_ui.yaml",
        "tmp/runtime.sqlite",
        "tmp/runtime.db",
        "tmp/runtime.log",
    )

    assert ignored == {
        "SESSION.json",
        "miniapp.pid",
        "full_ui.yaml",
        "tmp/runtime.sqlite",
        "tmp/runtime.db",
        "tmp/runtime.log",
    }


def test_intentional_lockfiles_and_manifest_are_not_globally_ignored() -> None:
    ignored = _git_check_ignore_no_index(
        "miniapp/package.json",
        "miniapp/package-lock.json",
        "skills-lock.json",
    )

    assert ignored == set()


def test_runtime_trash_removed_from_index_and_lockfile_contract_remains() -> None:
    tracked = _git_ls_files(*DISPUTED_FILES)

    assert tracked == {"skills-lock.json"}
