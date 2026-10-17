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
    # tmp_path лежит внутри /tmp, поэтому выходим до корня ФС, мимо разрешённого /tmp.
    traversal = "../" * len(tmp_path.parts) + "etc/passwd"
    result = svc.resolve(ArtifactIntent(traversal, 0.9), str(tmp_path))
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


def test_resolve_absolute_pattern_existing_file(tmp_path: Path) -> None:
    (tmp_path / "app.apk").write_bytes(b"x")
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent(str(tmp_path / "app.apk"), 1.0), str(tmp_path))
    assert result.error is None
    assert result.resolved_path == str(tmp_path / "app.apk")


def test_resolve_absolute_pattern_missing_file_reports_not_found(tmp_path: Path) -> None:
    # Регрессия: Path.glob с абсолютным паттерном бросал NotImplementedError.
    svc = ArtifactIntentService()
    missing = tmp_path / "build" / "outputs" / "app-debug.apk"
    result = svc.resolve(ArtifactIntent(str(missing), 1.0), str(tmp_path))
    assert result.resolved_path == ""
    assert result.error is not None
    assert "не найден" in result.error.lower()


def test_resolve_absolute_pattern_outside_root(tmp_path: Path) -> None:
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent("/etc/hostname", 1.0), str(tmp_path))
    assert result.resolved_path == ""
    assert result.error is not None
    assert "пределы проекта" in result.error


@pytest.mark.parametrize("pattern_factory", [lambda root: str(root), lambda root: str(root) + "/", lambda _root: "."])
def test_resolve_project_root_itself_reports_not_found(tmp_path: Path, pattern_factory) -> None:
    svc = ArtifactIntentService()
    result = svc.resolve(ArtifactIntent(pattern_factory(tmp_path), 1.0), str(tmp_path))
    assert result.resolved_path == ""
    assert result.error is not None
    assert "не найден" in result.error.lower()


@pytest.fixture
def allowed_tmp(tmp_path: Path, monkeypatch) -> Path:
    from app.services import artifact_intent_service as module

    allowed = tmp_path / "tmp"
    allowed.mkdir()
    monkeypatch.setattr(module, "_EXTRA_ALLOWED_ROOT", allowed)
    return allowed


def test_resolve_absolute_path_in_tmp_is_allowed(tmp_path: Path, allowed_tmp: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (allowed_tmp / "report.html").write_text("ok")
    result = ArtifactIntentService().resolve(ArtifactIntent(str(allowed_tmp / "report.html"), 1.0), str(project))
    assert result.error is None
    assert result.resolved_path == str(allowed_tmp / "report.html")


def test_resolve_tmp_glob_and_blocked_names(tmp_path: Path, allowed_tmp: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (allowed_tmp / "out.log").write_text("log")
    (allowed_tmp / ".env").write_text("SECRET=1")
    svc = ArtifactIntentService()
    glob_result = svc.resolve(ArtifactIntent(str(allowed_tmp / "*.log"), 1.0), str(project))
    assert glob_result.resolved_path == str(allowed_tmp / "out.log")
    blocked = svc.resolve(ArtifactIntent(str(allowed_tmp / ".env"), 1.0), str(project))
    assert blocked.error is not None
    assert "секрет" in blocked.error.lower()


def test_resolve_tmp_symlink_outside_is_rejected(tmp_path: Path, allowed_tmp: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (allowed_tmp / "link.txt").symlink_to(outside)
    result = ArtifactIntentService().resolve(ArtifactIntent(str(allowed_tmp / "link.txt"), 1.0), str(project))
    assert result.resolved_path == ""
    assert "пределы проекта" in (result.error or "")


@pytest.mark.parametrize("in_tmp", [True, False], ids=["tmp", "project"])
def test_resolve_glob_symlink_outside_is_rejected(tmp_path: Path, allowed_tmp: Path, in_tmp: bool) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    base = allowed_tmp if in_tmp else project
    (base / "link.txt").symlink_to(outside)
    result = ArtifactIntentService().resolve(ArtifactIntent(str(base / "lin*.txt"), 1.0), str(project))
    assert result.resolved_path == ""
    assert "пределы проекта" in (result.error or "")


def test_resolve_glob_symlink_to_secret_is_blocked(tmp_path: Path, allowed_tmp: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (allowed_tmp / "id_ed25519").write_text("key")
    (allowed_tmp / "key.txt").symlink_to(allowed_tmp / "id_ed25519")
    result = ArtifactIntentService().resolve(ArtifactIntent(str(allowed_tmp / "key.tx?"), 1.0), str(project))
    assert result.resolved_path == ""
    assert "секрет" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# classify() with decision model
# ---------------------------------------------------------------------------


def _decision_config() -> SimpleNamespace:
    return SimpleNamespace(defaults=SimpleNamespace(openai_decision_model="llmgateway/decision"))


@pytest.mark.asyncio
async def test_classify_decision_no_skips_llm(monkeypatch) -> None:
    from app.services import artifact_intent_service as module

    captured = {}

    async def fake_decisions(_config, *, model, state, questions):
        captured.update(model=model, state=state, questions=questions)
        return {"file_request": {"type": "noul", "noul": 0.03}}

    async def llm_fn(*_args, **_kw):
        raise AssertionError("chat LLM must not be called after decision «no»")

    monkeypatch.setattr(module, "decisions", fake_decisions)
    intent = await ArtifactIntentService().classify(
        "посмотри файл app/main.py",
        app_config=_decision_config(),
        llm_fn=llm_fn,
    )

    assert intent is None
    assert captured["model"] == "llmgateway/decision"
    assert captured["state"] == "посмотри файл app/main.py"
    assert captured["questions"]["file_request"]["type"] == "noul"


@pytest.mark.asyncio
async def test_classify_decision_yes_uses_llm_for_path(monkeypatch) -> None:
    from app.services import artifact_intent_service as module

    async def fake_decisions(*_args, **_kw):
        return {"file_request": {"type": "noul", "noul": 0.94}}

    async def llm_fn(*_args, **_kw):
        return '{"is_artifact_request": true, "file_pattern": "src/config.yaml", "confidence": 0.9}'

    monkeypatch.setattr(module, "decisions", fake_decisions)
    intent = await ArtifactIntentService().classify(
        "скинь мне src/config.yaml",
        app_config=_decision_config(),
        llm_fn=llm_fn,
    )

    assert intent == ArtifactIntent(file_pattern="src/config.yaml", confidence=0.9)


@pytest.mark.asyncio
async def test_classify_decision_failure_falls_back_to_llm(monkeypatch) -> None:
    from app.services import artifact_intent_service as module

    async def fake_decisions(*_args, **_kw):
        raise RuntimeError("gateway down")

    async def llm_fn(*_args, **_kw):
        return '{"is_artifact_request": true, "file_pattern": "a/b.txt", "confidence": 0.9}'

    monkeypatch.setattr(module, "decisions", fake_decisions)
    intent = await ArtifactIntentService().classify(
        "пришли a/b.txt",
        app_config=_decision_config(),
        llm_fn=llm_fn,
    )

    assert intent is not None
    assert intent.file_pattern == "a/b.txt"
