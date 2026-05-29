"""Tests for ArtifactIntentService — classification and resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.artifact_intent_service import (
    ArtifactIntent,
    ArtifactIntentService,
)


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_positive() -> None:
    """LLM returns a valid artifact request."""

    async def llm_fn(_config, _system, _user, **_kw):
        return '{"is_artifact_request": true, "file_pattern": "config.yaml", "confidence": 0.95}'

    svc = ArtifactIntentService()
    intent = await svc.classify(
        "пришли мне config.yaml",
        app_config=SimpleNamespace(),
        llm_fn=llm_fn,
    )
    assert intent is not None
    assert intent.file_pattern == "config.yaml"
    assert intent.confidence == 0.95


@pytest.mark.asyncio
async def test_classify_explicit_absolute_path_uses_llm_result() -> None:
    svc = ArtifactIntentService()
    captured = {}

    async def llm_fn(_config, _system, _user, **_kw):
        captured["called"] = True
        return (
            '{"is_artifact_request": true, '
            '"file_pattern": "/srv/git_projects/cli-proxy/docs/plans/2026-03-21-opencode-serve-transport-design.md", '
            '"confidence": 0.97}'
        )

    intent = await svc.classify(
        "пришли файл /srv/git_projects/cli-proxy/docs/plans/2026-03-21-opencode-serve-transport-design.md",
        app_config=SimpleNamespace(),
        llm_fn=llm_fn,
    )

    assert intent is not None
    assert (
        intent.file_pattern
        == "/srv/git_projects/cli-proxy/docs/plans/2026-03-21-opencode-serve-transport-design.md"
    )
    assert intent.confidence == 0.97
    assert captured["called"] is True


@pytest.mark.asyncio
async def test_classify_negative() -> None:
    """LLM says it's not an artifact request."""

    async def llm_fn(_config, _system, _user, **_kw):
        return '{"is_artifact_request": false, "file_pattern": "", "confidence": 1.0}'

    svc = ArtifactIntentService()
    intent = await svc.classify(
        "посмотри содержимое config.yaml",
        app_config=SimpleNamespace(),
        llm_fn=llm_fn,
    )
    assert intent is None


@pytest.mark.asyncio
async def test_classify_low_confidence() -> None:
    """LLM returns artifact request but with low confidence — should be ignored."""

    async def llm_fn(_config, _system, _user, **_kw):
        return '{"is_artifact_request": true, "file_pattern": "file.txt", "confidence": 0.3}'

    svc = ArtifactIntentService()
    intent = await svc.classify(
        "а что там в file.txt",
        app_config=SimpleNamespace(),
        llm_fn=llm_fn,
    )
    assert intent is None


@pytest.mark.asyncio
async def test_classify_llm_error_returns_none() -> None:
    """LLM call fails — should not raise, return None."""

    async def llm_fn(_config, _system, _user, **_kw):
        raise RuntimeError("LLM unavailable")

    svc = ArtifactIntentService()
    intent = await svc.classify(
        "пришли config.yaml",
        app_config=SimpleNamespace(),
        llm_fn=llm_fn,
    )
    assert intent is None


@pytest.mark.asyncio
async def test_classify_empty_text() -> None:
    svc = ArtifactIntentService()

    async def llm_fn(*_a, **_kw):
        raise AssertionError("should not be called")

    intent = await svc.classify("", app_config=SimpleNamespace(), llm_fn=llm_fn)
    assert intent is None


@pytest.mark.asyncio
async def test_classify_passes_model_kwarg() -> None:
    """Model override is forwarded to llm_fn."""
    captured = {}

    async def llm_fn(_config, _system, _user, **kw):
        captured.update(kw)
        return '{"is_artifact_request": false, "file_pattern": "", "confidence": 1.0}'

    svc = ArtifactIntentService()
    await svc.classify(
        "text",
        app_config=SimpleNamespace(),
        llm_fn=llm_fn,
        model="gpt-4o",
    )
    assert captured.get("model") == "gpt-4o"


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_file_exists(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("hello.txt", 0.9), str(tmp_path))
    assert result.error is None
    assert result.resolved_path == str(tmp_path / "hello.txt")


def test_resolve_file_not_found(tmp_path: Path) -> None:
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("nope.txt", 0.9), str(tmp_path))
    assert result.error is not None
    assert "не найден" in result.error.lower() or "Не найден" in result.error


def test_resolve_blocked_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1")
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent(".env", 0.9), str(tmp_path))
    assert result.error is not None
    assert "секрет" in result.error.lower()


def test_resolve_path_traversal(tmp_path: Path) -> None:
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("../../etc/passwd", 0.9), str(tmp_path))
    assert result.error is not None
    assert "за пределы" in result.error.lower() or "пределы" in result.error


def test_resolve_no_project_root() -> None:
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("file.txt", 0.9), None)
    assert result.error is not None


def test_resolve_glob_single_match(tmp_path: Path) -> None:
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "main.py").write_text("print(1)")
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("src/main.py", 0.9), str(tmp_path))
    assert result.error is None
    assert result.resolved_path == str(sub / "main.py")


def test_resolve_glob_multiple_matches(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("*.txt", 0.9), str(tmp_path))
    assert result.error is not None
    assert "несколько" in result.error.lower() or "Уточните" in result.error


def test_resolve_blocked_via_glob(tmp_path: Path) -> None:
    """Even when resolved via glob, blocked files are rejected."""
    (tmp_path / "credentials.json").write_text("{}")
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("credentials.json", 0.9), str(tmp_path))
    assert result.error is not None
    assert "секрет" in result.error.lower()
