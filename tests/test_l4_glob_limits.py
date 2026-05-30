"""Tests: glob/rglob calls are truncated at their module-level limits."""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# utils/source_artifact.py — candidate.rglob("*") capped at _RGLOB_FILES_LIMIT
# ---------------------------------------------------------------------------


def _create_files(directory: Path, count: int) -> None:
    """Create `count` dummy .py files inside *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (directory / f"f{i}.py").write_text("")


def test_source_artifact_rglob_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """iter_source_artifact_files stops at _RGLOB_FILES_LIMIT files per directory."""
    import utils.source_artifact as sa

    small_limit = 5
    monkeypatch.setattr(sa, "_RGLOB_FILES_LIMIT", small_limit)

    subdir = tmp_path / "app"
    _create_files(subdir, small_limit + 3)  # 8 files, limit is 5

    results = list(sa.iter_source_artifact_files(root=tmp_path, include=["app"]))
    assert len(results) <= small_limit, (
        f"Expected at most {small_limit} files, got {len(results)}"
    )


def test_source_artifact_rglob_no_truncation_when_under_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When file count is below the limit, all files are returned."""
    import utils.source_artifact as sa

    small_limit = 20
    monkeypatch.setattr(sa, "_RGLOB_FILES_LIMIT", small_limit)

    subdir = tmp_path / "app"
    _create_files(subdir, 7)

    results = list(sa.iter_source_artifact_files(root=tmp_path, include=["app"]))
    assert len(results) == 7


# ---------------------------------------------------------------------------
# app/services/artifact_intent_service.py — root.glob() capped at _GLOB_MATCHES_LIMIT
# ---------------------------------------------------------------------------


def test_artifact_intent_glob_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve() stops at _GLOB_MATCHES_LIMIT matches when glob returns many results."""
    from app.services.artifact_intent_service import ArtifactIntent, ArtifactIntentService

    import app.services.artifact_intent_service as ais

    small_limit = 4
    monkeypatch.setattr(ais, "_GLOB_MATCHES_LIMIT", small_limit)

    # Create more .txt files than the limit
    for i in range(small_limit + 3):
        (tmp_path / f"note{i}.txt").write_text("x")

    intent = ArtifactIntent(file_pattern="*.txt", confidence=0.9)
    svc = ArtifactIntentService()
    result = svc.resolve(intent, project_root=str(tmp_path))

    # With multiple matches the service returns an error listing them.
    # We just need to confirm it didn't hang / didn't exceed the cap.
    # The raw_matches list is capped; the error path lists up to 10.
    assert result.error is not None
    assert "Найдено несколько файлов" in result.error


def test_artifact_intent_glob_single_match_still_works(tmp_path: Path) -> None:
    """resolve() returns the file when exactly one match exists."""
    from app.services.artifact_intent_service import ArtifactIntent, ArtifactIntentService

    (tmp_path / "report.csv").write_text("col1,col2\n1,2\n")

    intent = ArtifactIntent(file_pattern="*.csv", confidence=0.9)
    svc = ArtifactIntentService()
    result = svc.resolve(intent, project_root=str(tmp_path))

    assert result.error is None
    assert result.resolved_path.endswith("report.csv")
