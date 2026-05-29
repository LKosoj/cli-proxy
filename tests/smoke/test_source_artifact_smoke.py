from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from utils.source_artifact import REQUIRED_ARTIFACT_MEMBERS


def _resolve_artifact_from_env() -> pathlib.Path | None:
    spec = str(os.environ.get("SMOKE_SOURCE_ARTIFACT") or "").strip()
    if not spec:
        return None
    root = pathlib.Path(__file__).resolve().parents[2]
    if any(token in spec for token in "*?["):
        matches = sorted(root.glob(spec))
        assert matches, f"artifact glob matched nothing: {spec}"
        return matches[0]
    path = (root / spec).resolve()
    assert path.exists(), f"artifact does not exist: {path}"
    return path


def test_source_artifact_smoke_builds_and_validates_pack(tmp_path) -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    artifact_path = _resolve_artifact_from_env()
    if artifact_path is None:
        artifact_path = tmp_path / "source-smoke.zip"
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "utils.source_artifact",
                "build",
                "--root",
                str(repo_root),
                "--output",
                str(artifact_path),
            ],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert built.returncode == 0, built.stderr

    validated = subprocess.run(
        [
            sys.executable,
            "-m",
            "utils.source_artifact",
            "validate",
            str(artifact_path),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert validated.returncode == 0, validated.stderr
    assert artifact_path.exists()
    report = json.loads(validated.stdout)
    members = set(str(item) for item in report.get("members") or [])
    for member in REQUIRED_ARTIFACT_MEMBERS:
        assert member in members
    assert "config.yaml" not in members
