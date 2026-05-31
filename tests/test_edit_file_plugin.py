from __future__ import annotations

import pytest

from agent.plugins.edit_file import EditFileTool


@pytest.mark.asyncio
async def test_edit_file_replaces_unique_old_text_match(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = await EditFileTool().execute(
        {"path": "sample.txt", "old_text": "beta", "new_text": "BETA"},
        {"cwd": str(tmp_path)},
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


@pytest.mark.asyncio
async def test_edit_file_rejects_missing_old_text_and_preserves_file(tmp_path):
    target = tmp_path / "sample.txt"
    original = "alpha\nbeta\ngamma\n"
    target.write_text(original, encoding="utf-8")

    result = await EditFileTool().execute(
        {"path": "sample.txt", "old_text": "delta", "new_text": "DELTA"},
        {"cwd": str(tmp_path)},
    )

    assert result["success"] is False
    assert "old_text not found" in result["error"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_edit_file_rejects_multiple_old_text_matches_and_preserves_file(tmp_path):
    target = tmp_path / "sample.txt"
    original = "alpha\nbeta\nbeta\n"
    target.write_text(original, encoding="utf-8")

    result = await EditFileTool().execute(
        {"path": "sample.txt", "old_text": "beta", "new_text": "BETA"},
        {"cwd": str(tmp_path)},
    )

    assert result["success"] is False
    assert "matched multiple locations" in result["error"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_edit_file_rejects_overlapping_old_text_matches_and_preserves_file(tmp_path):
    target = tmp_path / "sample.txt"
    original = "aaa"
    target.write_text(original, encoding="utf-8")

    result = await EditFileTool().execute(
        {"path": "sample.txt", "old_text": "aa", "new_text": "b"},
        {"cwd": str(tmp_path)},
    )

    assert result["success"] is False
    assert "matched multiple locations" in result["error"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_edit_file_rejects_empty_old_text_and_preserves_file(tmp_path):
    target = tmp_path / "sample.txt"
    original = "alpha\n"
    target.write_text(original, encoding="utf-8")

    result = await EditFileTool().execute(
        {"path": "sample.txt", "old_text": "", "new_text": "prefix"},
        {"cwd": str(tmp_path)},
    )

    assert result["success"] is False
    assert "must not be empty" in result["error"]
    assert target.read_text(encoding="utf-8") == original
