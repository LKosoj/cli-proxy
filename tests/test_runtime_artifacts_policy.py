from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "docs" / "runtime-artifacts-policy.md"

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


def test_runtime_artifacts_policy_covers_disputed_tracked_files() -> None:
    policy = POLICY_PATH.read_text(encoding="utf-8")

    for path in DISPUTED_FILES:
        assert path in policy

    assert "git ls-files SESSION.json miniapp.pid full_ui.yaml" in policy
    assert "wc -c SESSION.json miniapp.pid full_ui.yaml" in policy
    assert "rg -n" in policy
    assert "git check-ignore --no-index miniapp/package.json" in policy
    assert "Task_16 Gate" in policy
    assert "tests/fixtures/..." in policy


def test_runtime_artifacts_policy_has_per_file_evidence() -> None:
    policy = POLICY_PATH.read_text(encoding="utf-8")
    normalized_policy = policy.lower()

    evidence_markers = {
        "SESSION.json": "modes/sdk/runtime/agent_core.py:508",
        "miniapp.pid": "docs/architecture-debt-remediation-tz.md:368",
        "full_ui.yaml": "docs/architecture-debt-remediation-tz.md:369",
        "miniapp/package.json": "docs/architecture-debt-remediation-tz.md:370",
        "miniapp/package-lock.json": "docs/architecture-debt-remediation-tz.md:371",
        "skills-lock.json": ".cli-proxy/.codebase_map/nodes/skills-lock-json.md:15",
    }
    for marker in evidence_markers.values():
        assert marker in policy

    assert "no `miniapp/package.json` fixture/test usage is proven" in policy
    assert "no `miniapp/package-lock.json` fixture/test usage is proven" in policy
    assert "not an intentional miniapp package workflow yet" in normalized_policy
    assert "not an intentional miniapp lockfile by itself" in normalized_policy


def test_runtime_trash_removed_from_index_and_lockfile_contract_remains() -> None:
    tracked = _git_ls_files(*DISPUTED_FILES)

    assert tracked == {"skills-lock.json"}

    policy = POLICY_PATH.read_text(encoding="utf-8")
    assert "Applied Task_16 Decision" in policy
    assert "Removed from the index as runtime-like trash" in policy
    assert "Removed from the index and working tree as non-intentional MiniApp workflow" in policy
    assert "Kept in the index as an intentional repository lockfile" in policy
