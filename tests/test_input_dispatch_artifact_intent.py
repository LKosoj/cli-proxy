"""Integration test: InputDispatchService routes artifact intent before CLI fallback."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.input_dispatch_service import InputDispatchService


def _make_bot_app(*, artifact_intent_service=None, cli_called=None, sent_docs=None):
    """Build a minimal bot_app SimpleNamespace for InputDispatchService tests."""

    class _InputTransport:
        async def send_text(self, _ctx, *, text: str, dest, fallback_chat_id, md2: bool = True):
            _ = text, dest, fallback_chat_id, md2
            return SimpleNamespace(message_id=1)

        async def send_document(self, _ctx, **kwargs):
            if sent_docs is not None:
                sent_docs.append(kwargs)
            return True

    async def _handle_cli_input(*_a, **_kw):
        if cli_called is not None:
            cli_called.append(True)

    class _StubRouter:
        def __init__(self):
            self.send_message = None
            self.dialogs = None
            self.mode_registry = None

        async def route_mode_or_cli(self, **kwargs):
            cli_fallback = kwargs.get("cli_fallback")
            if cli_fallback:
                await cli_fallback(
                    kwargs["session"], kwargs["text"], kwargs["chat_id"], kwargs["context"]
                )

    bot_app = SimpleNamespace(
        artifact_intent_service=artifact_intent_service,
        config=SimpleNamespace(defaults=SimpleNamespace(openai_big_model="test-model")),
        orchestrator_chat_completion=object(),
        mode_input_router=_StubRouter(),
        mode_dialogs=None,
        mode_registry_service=SimpleNamespace(),
        pending_input_ui=_InputTransport(),
        _handle_cli_input=_handle_cli_input,
        ui_state=SimpleNamespace(pending={}),
        metrics=SimpleNamespace(inc=lambda _: None),
        build_telegram_reply_dest=lambda _s, _c, **_kw: {"kind": "telegram", "chat_id": _c},
    )
    return bot_app


@pytest.mark.asyncio
async def test_artifact_intent_intercepts_cli(tmp_path: Path) -> None:
    """When artifact intent is detected, CLI fallback must NOT run."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    test_file = docs_dir / "hello.txt"
    test_file.write_text("world")

    from app.services.artifact_intent_service import ArtifactIntent, ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            return ArtifactIntent(file_pattern="docs/hello.txt", confidence=0.95)

    cli_called: list = []
    sent_docs: list = []

    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
        sent_docs=sent_docs,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root=str(tmp_path),
        conversation_scope=None,
    )

    await service.handle_user_input(session, "пришли docs/hello.txt", chat_id=1, context=object())

    assert len(cli_called) == 0, "CLI fallback should not run when artifact intent handled"
    assert len(sent_docs) == 1, "Document should be sent exactly once"
    assert sent_docs[0].get("filename") == "hello.txt"


@pytest.mark.asyncio
async def test_artifact_intent_uses_workdir_when_project_root_missing(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    test_file = docs_dir / "hello.txt"
    test_file.write_text("world")

    from app.services.artifact_intent_service import ArtifactIntent, ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            return ArtifactIntent(file_pattern="docs/hello.txt", confidence=0.95)

    cli_called: list = []
    sent_docs: list = []

    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
        sent_docs=sent_docs,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root=None,
        workdir=str(tmp_path),
        conversation_scope=None,
    )

    await service.handle_user_input(session, "пришли docs/hello.txt", chat_id=1, context=object())

    assert len(cli_called) == 0, "CLI fallback should not run when artifact intent handled"
    assert len(sent_docs) == 1, "Document should be sent exactly once"
    assert sent_docs[0].get("filename") == "hello.txt"


@pytest.mark.asyncio
async def test_artifact_intent_sends_via_input_transport_adapter(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    test_file = docs_dir / "hello.txt"
    test_file.write_text("world")

    from app.services.artifact_intent_service import ArtifactIntent, ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            return ArtifactIntent(file_pattern="docs/hello.txt", confidence=0.95)

    cli_called: list = []
    sent_docs: list = []

    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
        sent_docs=sent_docs,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root=str(tmp_path),
        conversation_scope=None,
    )

    await service.handle_user_input(session, "пришли docs/hello.txt", chat_id=1, context=object())

    assert len(cli_called) == 0, "CLI fallback should not run when artifact intent handled"
    assert len(sent_docs) == 1, "Document should be sent exactly once via input transport adapter"
    assert sent_docs[0].get("filename") == "hello.txt"


@pytest.mark.asyncio
async def test_artifact_intent_skipped_when_mode_active(tmp_path: Path) -> None:
    """When a mode is active, artifact intent should NOT be checked."""
    from app.services.artifact_intent_service import ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            raise AssertionError("classify should not be called when mode is active")

    cli_called: list = []
    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode="analyst",
        advanced_orchestrator_enabled=False,
        project_root=str(tmp_path),
        conversation_scope=None,
    )

    await service.handle_user_input(session, "пришли docs/hello.txt", chat_id=1, context=object())
    assert len(cli_called) == 1, "CLI fallback should run when mode is active"


@pytest.mark.asyncio
async def test_artifact_intent_skipped_without_path_separators(tmp_path: Path) -> None:
    """Without slash characters, artifact intent classification should not run."""
    from app.services.artifact_intent_service import ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            raise AssertionError("classify should not be called without path separators")

    cli_called: list = []
    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root=str(tmp_path),
        conversation_scope=None,
    )

    await service.handle_user_input(session, "пришли hello.txt", chat_id=1, context=object())
    assert len(cli_called) == 1, "CLI fallback should run when request is not an explicit path"


@pytest.mark.asyncio
async def test_artifact_intent_skipped_for_long_text_with_path(tmp_path: Path) -> None:
    """Long logs can contain paths and file verbs, but should not invoke artifact classification."""
    from app.services.artifact_intent_service import ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            raise AssertionError("classify should not be called for long text")

    cli_called: list = []
    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root=str(tmp_path),
        conversation_scope=None,
    )
    long_text = "пришли docs/hello.txt\n" + ("INFO app/services/input_dispatch_service.py: log line\n" * 8)
    assert len(long_text) > 250

    await service.handle_user_input(session, long_text, chat_id=1, context=object())

    assert len(cli_called) == 1, "CLI fallback should run when artifact intent is skipped for long text"


@pytest.mark.asyncio
async def test_artifact_intent_logs_skip_without_path_separators(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from app.services.artifact_intent_service import ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            raise AssertionError("classify should not be called without path separators")

    cli_called: list = []
    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root=str(tmp_path),
        conversation_scope=None,
    )

    with caplog.at_level(logging.INFO, logger="app.services.input_dispatch_service"):
        await service.handle_user_input(session, "пришли hello.txt", chat_id=1, context=object())

    assert len(cli_called) == 1
    assert "skip_no_path_separators" in caplog.text


@pytest.mark.asyncio
async def test_artifact_intent_none_falls_through_to_cli(caplog: pytest.LogCaptureFixture) -> None:
    """When classify returns None, request proceeds to CLI fallback."""
    from app.services.artifact_intent_service import ArtifactIntentService

    class _MockIntentService(ArtifactIntentService):
        async def classify(self, text, **kw):
            return None

    cli_called: list = []
    bot_app = _make_bot_app(
        artifact_intent_service=_MockIntentService(),
        cli_called=cli_called,
    )
    service = InputDispatchService(bot_app)

    session = SimpleNamespace(
        id="s1",
        active_mode=None,
        advanced_orchestrator_enabled=False,
        project_root="/tmp",
        conversation_scope=None,
    )

    with caplog.at_level(logging.INFO, logger="app.services.input_dispatch_service"):
        await service.handle_user_input(session, "пришли docs/missing.txt", chat_id=1, context=object())

    assert len(cli_called) == 1, "CLI fallback should run when no artifact intent"
    assert "classify_none" in caplog.text
