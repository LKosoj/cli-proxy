from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from desktop.services.application_facade import ApplicationFacade
from modes.admin.chat_memory import ChatMemory, ChatPendingStore
from modes.admin.chat_service import AdminChatService


@dataclass
class _FakeLocalResult:
    action_id: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class _FakeLocalTransport:
    async def run(self, spec: Any) -> _FakeLocalResult:
        return _FakeLocalResult(
            action_id=spec.action_id,
            returncode=0,
            stdout="ok",
            stderr="",
            timed_out=False,
            duration_ms=1,
        )


class _FakeSshTransport:
    async def run(self, spec: Any) -> Any:
        return SimpleNamespace(
            action_id=spec.action_id,
            host=spec.host,
            user=spec.user or "",
            port=spec.port,
            returncode=0,
            stdout="up",
            stderr="",
            timed_out=False,
            duration_ms=1,
        )


def _write_admin_config(workdir: Path, admin_cfg: Dict[str, Any]) -> None:
    cli_proxy = workdir / ".cli-proxy"
    cli_proxy.mkdir(exist_ok=True)
    admin_dir = cli_proxy / ".admin"
    admin_dir.mkdir(exist_ok=True)
    (admin_dir / "config.yaml").write_text(
        yaml.safe_dump({"admin": admin_cfg}),
        encoding="utf-8",
    )


def _make_facade(
    tmp_path: Path,
    *,
    llm_responses: List[str] = None,
) -> SimpleNamespace:
    responses = list(llm_responses or [])
    llm_calls: List[Dict[str, str]] = []

    def factory(_bot_app: Any):
        async def provider(system: str, user: str) -> str:
            llm_calls.append({"system": system, "user": user})
            if not responses:
                return ""
            return responses.pop(0)

        return provider

    chat_service = AdminChatService(
        local_transport=_FakeLocalTransport(),
        ssh_transport=_FakeSshTransport(),
        llm_provider_factory=factory,
    )
    session = SimpleNamespace(id="sid", workdir=str(tmp_path), cli_mode="claude")
    session_service = SimpleNamespace(get_session_by_uid=lambda _uid: session)
    plugin = SimpleNamespace(_chat_service=chat_service)
    mode_registry_service = SimpleNamespace(get=lambda mode_id: plugin if mode_id == "admin" else None)
    facade = SimpleNamespace(
        session_service=session_service,
        mode_registry_service=mode_registry_service,
        logger=logging.getLogger("test_desktop_admin_chat_facade"),
        _ensure_modes_ready=lambda: None,
        _desktop_bot_app=lambda: SimpleNamespace(config=None),
    )
    facade._resolve_admin_chat_service = MethodType(
        ApplicationFacade._resolve_admin_chat_service, facade
    )
    facade._require_session_workdir = MethodType(
        ApplicationFacade._require_session_workdir, facade
    )
    facade._llm_calls = llm_calls
    facade._chat_service = chat_service
    return facade


_ADMIN_CFG = {
    "actions": {
        "local": {
            "check_disk": {
                "argv": ["df", "-h"],
                "timeout_sec": 10,
                "risk_level": "low",
                "read_only": True,
            },
        },
    },
}


def test_admin_config_yaml_round_trip_preserves_desktop_shape(tmp_path) -> None:
    facade = _make_facade(tmp_path)

    loaded = ApplicationFacade.get_admin_config_yaml(facade, "sess")
    assert loaded is not None
    assert set(loaded) == {"config_path", "yaml"}
    parsed = yaml.safe_load(loaded["yaml"])
    parsed["admin"]["monitor"]["interval_sec"] = 41

    saved = ApplicationFacade.save_admin_config_yaml(
        facade,
        "sess",
        yaml_text=yaml.safe_dump(parsed, sort_keys=False),
    )
    reloaded = ApplicationFacade.get_admin_config_yaml(facade, "sess")

    assert saved == {"ok": True}
    assert reloaded is not None
    assert yaml.safe_load(reloaded["yaml"])["admin"]["monitor"]["interval_sec"] == 41


def test_save_admin_config_yaml_preserves_desktop_error_shape(tmp_path) -> None:
    facade = _make_facade(tmp_path)

    not_mapping = ApplicationFacade.save_admin_config_yaml(facade, "sess", yaml_text="[]")
    malformed = ApplicationFacade.save_admin_config_yaml(facade, "sess", yaml_text="admin: [")

    assert not_mapping == {"ok": False, "error": "config_must_be_mapping"}
    assert malformed["ok"] is False
    assert str(malformed["error"]).startswith("invalid_yaml:")


def test_get_admin_chat_messages_empty(tmp_path) -> None:
    facade = _make_facade(tmp_path)
    result = ApplicationFacade.get_admin_chat_messages(facade, "sess")
    assert result == {"ok": True, "messages": []}


def test_get_admin_chat_messages_returns_entries(tmp_path) -> None:
    ChatMemory(str(tmp_path)).append(role="user", text="hi")
    facade = _make_facade(tmp_path)
    result = ApplicationFacade.get_admin_chat_messages(facade, "sess")
    assert result["ok"] is True
    assert len(result["messages"]) == 1


def test_get_admin_chat_pending_empty(tmp_path) -> None:
    facade = _make_facade(tmp_path)
    result = ApplicationFacade.get_admin_chat_pending(facade, "sess")
    assert result == {"ok": True, "items": []}


def test_get_admin_chat_pending_returns_items(tmp_path) -> None:
    ChatPendingStore(str(tmp_path)).save(
        "chat-x",
        {"approval_id": "chat-x", "intent": {"type": "propose_action"}},
    )
    facade = _make_facade(tmp_path)
    result = ApplicationFacade.get_admin_chat_pending(facade, "sess")
    assert result["ok"] is True
    assert [i["approval_id"] for i in result["items"]] == ["chat-x"]


def test_memory_md_roundtrip(tmp_path) -> None:
    facade = _make_facade(tmp_path)
    read1 = ApplicationFacade.get_admin_chat_memory_md(facade, "sess")
    assert read1 == {"ok": True, "text": ""}

    saved = ApplicationFacade.save_admin_chat_memory_md(facade, "sess", text="nginx only at night")
    assert saved == {"ok": True}
    read2 = ApplicationFacade.get_admin_chat_memory_md(facade, "sess")
    assert "nginx only at night" in read2["text"]


def test_reject_removes_pending_and_logs(tmp_path) -> None:
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-r", {"approval_id": "chat-r", "intent": {"type": "propose_action"}})
    facade = _make_facade(tmp_path)
    result = ApplicationFacade.reject_admin_chat_pending(facade, "sess", approval_id="chat-r")
    assert result["ok"] is True
    assert store.get("chat-r") is None


def test_reject_missing_returns_error(tmp_path) -> None:
    facade = _make_facade(tmp_path)
    result = ApplicationFacade.reject_admin_chat_pending(facade, "sess", approval_id="chat-ghost")
    assert result["ok"] is False
    assert result["error"] == "approval_not_found"


def test_resolve_service_unavailable_returns_error(tmp_path) -> None:
    # No plugin in registry → service is None
    facade = _make_facade(tmp_path)
    facade.mode_registry_service = SimpleNamespace(get=lambda _m: None)
    result = ApplicationFacade.get_admin_chat_messages(facade, "sess")
    assert result["ok"] is False
    assert result["error"] == "chat_service_unavailable"


def test_session_not_found_returns_error(tmp_path) -> None:
    facade = _make_facade(tmp_path)
    facade.session_service = SimpleNamespace(get_session_by_uid=lambda _uid: None)
    result = ApplicationFacade.get_admin_chat_messages(facade, "sess")
    assert result["ok"] is False
    assert result["error"] == "session_workdir_empty"


@pytest.mark.asyncio
async def test_post_admin_chat_message(tmp_path) -> None:
    _write_admin_config(tmp_path, _ADMIN_CFG)
    facade = _make_facade(
        tmp_path,
        llm_responses=[json.dumps({"type": "answer", "text": "hi"})],
    )
    result = await ApplicationFacade.post_admin_chat_message(
        facade, "sess", text="hello",
    )
    assert result["ok"] is True
    assert result["reply_text"] == "hi"


@pytest.mark.asyncio
async def test_approve_admin_chat_pending_local(tmp_path) -> None:
    _write_admin_config(tmp_path, _ADMIN_CFG)
    ChatPendingStore(str(tmp_path)).save(
        "chat-local",
        {
            "approval_id": "chat-local",
            "intent": {
                "type": "propose_action",
                "action_id": "check_disk",
                "target": "local",
                "text": "x",
            },
        },
    )
    facade = _make_facade(tmp_path)
    result = await ApplicationFacade.approve_admin_chat_pending(
        facade, "sess", approval_id="chat-local",
    )
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["target_kind"] == "local"


@pytest.mark.asyncio
async def test_approve_admin_chat_pending_missing(tmp_path) -> None:
    facade = _make_facade(tmp_path)
    result = await ApplicationFacade.approve_admin_chat_pending(
        facade, "sess", approval_id="chat-ghost",
    )
    assert result["ok"] is False
    assert result["error"] == "approval_not_found"
