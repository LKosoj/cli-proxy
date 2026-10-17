"""Ввод в простаивающую сессию с живым tmux идёт сразу в CLI, минуя оркестратор и скиллы."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import input_dispatch_service as module
from app.services.input_dispatch_models import DIRECT_TMUX_DEST_KEY
from app.services.input_dispatch_service import InputDispatchService


def _service(monkeypatch, *, backend: str = "tmux", live: bool = True, artifact_handled: bool = False):
    bot_app = SimpleNamespace(
        config=SimpleNamespace(defaults=SimpleNamespace(pending_input_confirmation_enabled=False)),
        build_telegram_reply_dest=lambda _s, chat_id, **_kw: {"kind": "telegram", "chat_id": chat_id},
    )
    service = InputDispatchService(bot_app)
    calls: dict[str, list] = {"cli": [], "artifact": [], "impl": []}

    async def route_mode_or_cli(*, session, text, chat_id, context, dest, user_id):
        calls["cli"].append({"text": text, "dest": dest})

    async def try_artifact_intent(**kwargs):
        calls["artifact"].append(kwargs["text"])
        return artifact_handled

    async def handle_user_input_impl(**kwargs):
        calls["impl"].append(kwargs["text"])

    monkeypatch.setattr(service, "_route_mode_or_cli", route_mode_or_cli)
    monkeypatch.setattr(service, "_try_artifact_intent", try_artifact_intent)
    monkeypatch.setattr(service, "_handle_user_input_impl", handle_user_input_impl)
    monkeypatch.setattr(service, "_is_session_busy", lambda *_a, **_kw: False)
    monkeypatch.setattr(module, "get_session_execution_backend", lambda _s: backend)
    monkeypatch.setattr(module, "has_live_tmux", lambda _s: live)
    return service, calls


def _session(**overrides) -> SimpleNamespace:
    values = {"id": "s1", "workdir": "/proj", "conversation_scope": None, "active_mode": None}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_live_idle_tmux_sends_directly_with_attachments(monkeypatch) -> None:
    service, calls = _service(monkeypatch)

    await service.stage_user_input(
        _session(),
        "что на картинке",
        1,
        object(),
        image_paths=["/proj/.attachments/a.png"],
    )

    assert calls["impl"] == []
    assert len(calls["cli"]) == 1
    sent = calls["cli"][0]
    assert sent["text"] == "что на картинке"
    assert sent["dest"][DIRECT_TMUX_DEST_KEY] is True
    assert sent["dest"]["image_paths"] == ["/proj/.attachments/a.png"]


@pytest.mark.asyncio
async def test_live_tmux_text_with_slash_checks_file_request_first(monkeypatch) -> None:
    service, calls = _service(monkeypatch, artifact_handled=True)

    await service.stage_user_input(_session(), "скинь /tmp/report.html", 1, object())

    assert calls["artifact"] == ["скинь /tmp/report.html"]
    assert calls["cli"] == []


@pytest.mark.asyncio
async def test_live_tmux_text_with_slash_not_file_request_goes_to_tmux(monkeypatch) -> None:
    service, calls = _service(monkeypatch, artifact_handled=False)

    await service.stage_user_input(_session(), "почини app/main.py", 1, object())

    assert calls["artifact"] == ["почини app/main.py"]
    assert [item["text"] for item in calls["cli"]] == ["почини app/main.py"]


@pytest.mark.parametrize(
    ("backend", "live", "session_overrides"),
    [
        ("tmux", False, {}),
        ("headless", True, {}),
        ("tmux", True, {"active_mode": "agent"}),
    ],
    ids=["tmux_not_running", "not_tmux_backend", "active_mode"],
)
@pytest.mark.asyncio
async def test_regular_flow_when_live_tmux_not_applicable(monkeypatch, backend, live, session_overrides) -> None:
    service, calls = _service(monkeypatch, backend=backend, live=live)

    await service.stage_user_input(_session(**session_overrides), "сделай рефакторинг", 1, object())

    assert calls["impl"] == ["сделай рефакторинг"]
    assert calls["cli"] == []


@pytest.mark.asyncio
async def test_live_tmux_dest_reaches_cli_through_real_router(monkeypatch) -> None:
    received: list[dict] = []

    class _Router:
        mode_registry = None

        async def route_mode_or_cli(self, **kwargs):
            await kwargs["cli_fallback"](kwargs["session"], kwargs["text"], kwargs["chat_id"], kwargs["context"])

    async def handle_cli_input(_session, text, _chat_id, _context, *, dest=None):
        received.append({"text": text, "dest": dest})

    bot_app = SimpleNamespace(
        config=SimpleNamespace(defaults=SimpleNamespace(pending_input_confirmation_enabled=False)),
        build_telegram_reply_dest=lambda _s, chat_id, **_kw: {"kind": "telegram", "chat_id": chat_id, "user_id": 7},
        mode_input_router=_Router(),
        _handle_cli_input=handle_cli_input,
    )
    service = InputDispatchService(bot_app)

    async def no_artifact(**_kw):
        return False

    monkeypatch.setattr(service, "_try_artifact_intent", no_artifact)
    monkeypatch.setattr(service, "_is_session_busy", lambda *_a, **_kw: False)
    monkeypatch.setattr(module, "get_session_execution_backend", lambda _s: "tmux")
    monkeypatch.setattr(module, "has_live_tmux", lambda _s: True)

    await service.stage_user_input(_session(), "", 1, object(), image_paths=["/proj/.attachments/a.png"])

    assert len(received) == 1
    assert received[0]["dest"][DIRECT_TMUX_DEST_KEY] is True
    assert received[0]["dest"]["image_paths"] == ["/proj/.attachments/a.png"]
