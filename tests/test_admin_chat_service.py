from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from modes.admin.chat_memory import ChatMemory, ChatPendingStore
from modes.admin.chat_service import AdminChatService


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
        "ssh": {
            "restart_nginx": {
                "argv": ["sudo", "systemctl", "restart", "nginx"],
                "host": "1.2.3.4",
                "user": "deploy",
                "key_path": "keys/id_rsa",
                "port": 22,
                "timeout_sec": 30,
                "risk_level": "high",
            },
        },
    },
}


@dataclass
class _FakeLocalResult:
    action_id: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


@dataclass
class _FakeSshResult:
    action_id: str
    host: str
    user: str
    port: int
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class _FakeLocalTransport:
    def __init__(self, result: _FakeLocalResult) -> None:
        self._result = result
        self.calls: List[Any] = []

    async def run(self, spec: Any) -> _FakeLocalResult:
        self.calls.append(spec)
        return _FakeLocalResult(
            action_id=spec.action_id,
            returncode=self._result.returncode,
            stdout=self._result.stdout,
            stderr=self._result.stderr,
            timed_out=self._result.timed_out,
            duration_ms=self._result.duration_ms,
        )


class _FakeSshTransport:
    def __init__(self, result: _FakeSshResult) -> None:
        self._result = result
        self.calls: List[Any] = []

    async def run(self, spec: Any) -> _FakeSshResult:
        self.calls.append(spec)
        return _FakeSshResult(
            action_id=spec.action_id,
            host=spec.host,
            user=spec.user or "",
            port=spec.port,
            returncode=self._result.returncode,
            stdout=self._result.stdout,
            stderr=self._result.stderr,
            timed_out=self._result.timed_out,
            duration_ms=self._result.duration_ms,
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


def _write_ssh_yaml(workdir: Path, hosts: Dict[str, Any]) -> None:
    cli_proxy = workdir / ".cli-proxy"
    cli_proxy.mkdir(exist_ok=True)
    (cli_proxy / "ssh.yaml").write_text(
        yaml.safe_dump({"hosts": hosts}),
        encoding="utf-8",
    )


def _make_service(
    *,
    local_result: _FakeLocalResult = None,
    ssh_result: _FakeSshResult = None,
    llm_responses: List[str] = None,
) -> tuple[AdminChatService, List[Dict[str, str]]]:
    calls: List[Dict[str, str]] = []
    responses = list(llm_responses or [])

    def factory(_bot_app: Any):
        async def provider(system: str, user: str) -> str:
            calls.append({"system": system, "user": user})
            if not responses:
                return ""
            return responses.pop(0)

        return provider

    local = _FakeLocalTransport(
        local_result or _FakeLocalResult(
            action_id="", returncode=0, stdout="ok", stderr="",
            timed_out=False, duration_ms=12,
        )
    )
    ssh = _FakeSshTransport(
        ssh_result or _FakeSshResult(
            action_id="", host="1.2.3.4", user="deploy", port=22,
            returncode=0, stdout="up", stderr="",
            timed_out=False, duration_ms=34,
        )
    )
    svc = AdminChatService(
        local_transport=local,
        ssh_transport=ssh,
        llm_provider_factory=factory,
    )
    return svc, calls


def _make_session(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(id="sid", workdir=str(tmp_path), cli_mode="claude")


# ---------- sync read/write ----------


def test_list_messages_empty(tmp_path):
    svc, _ = _make_service()
    assert svc.list_messages(str(tmp_path)) == []


def test_list_messages_returns_appended_entries(tmp_path):
    mem = ChatMemory(str(tmp_path))
    mem.append(role="user", text="hello")
    mem.append(role="assistant", text="hi")
    svc, _ = _make_service()
    rows = svc.list_messages(str(tmp_path))
    assert [r["role"] for r in rows] == ["user", "assistant"]


def test_list_pending_sorted(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-a", {"approval_id": "chat-a"})
    store.save("chat-b", {"approval_id": "chat-b"})
    svc, _ = _make_service()
    items = svc.list_pending(str(tmp_path))
    ids = [r["approval_id"] for r in items]
    assert set(ids) == {"chat-a", "chat-b"}


def test_memory_md_read_and_save_roundtrip(tmp_path):
    svc, _ = _make_service()
    assert svc.get_memory_md(str(tmp_path)) == ""
    svc.save_memory_md(str(tmp_path), text="rule1\nrule2")
    assert svc.get_memory_md(str(tmp_path)) == "rule1\nrule2"


def test_counters_track_messages_and_pending(tmp_path):
    mem = ChatMemory(str(tmp_path))
    mem.append(role="user", text="a")
    ChatPendingStore(str(tmp_path)).save("chat-1", {"approval_id": "chat-1"})
    svc, _ = _make_service()
    counters = svc.counters(str(tmp_path))
    assert counters["messages_count"] == 1
    assert counters["pending_count"] == 1
    assert counters["last_message_ts"]


def test_reject_pending_removes_file_and_logs_memory(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-xx", {"approval_id": "chat-xx", "intent": {"type": "propose_action"}})
    svc, _ = _make_service()
    result = svc.reject_pending(str(tmp_path), approval_id="chat-xx")
    assert result["ok"] is True
    assert store.get("chat-xx") is None
    messages = ChatMemory(str(tmp_path)).load_messages()
    assert any("rejected" in m.text for m in messages)


def test_reject_pending_missing(tmp_path):
    svc, _ = _make_service()
    result = svc.reject_pending(str(tmp_path), approval_id="chat-ghost")
    assert result["ok"] is False
    assert result["error"] == "approval_not_found"


def test_reject_pending_invalid_id(tmp_path):
    svc, _ = _make_service()
    assert svc.reject_pending(str(tmp_path), approval_id="")["ok"] is False


# ---------- async send ----------


@pytest.mark.asyncio
async def test_send_answer(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    svc, calls = _make_service(
        llm_responses=[json.dumps({"type": "answer", "text": "hello"})]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="привет")
    assert result["ok"] is True
    assert result["reply_text"] == "hello"
    assert result["intent"]["type"] == "answer"
    assert result["pending_action_id"] is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_send_propose_action_persists_pending(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    _write_ssh_yaml(tmp_path, {"prod1": {"host": "1.2.3.4", "user": "deploy", "auth": "key"}})
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_action",
                "action_id": "restart_nginx",
                "target": "prod1",
                "rationale": "x",
                "text": "предлагаю",
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="restart")
    approval_id = result["pending_action_id"]
    assert approval_id and approval_id.startswith("chat-")
    items = svc.list_pending(str(tmp_path))
    assert any(item["approval_id"] == approval_id for item in items)


@pytest.mark.asyncio
async def test_send_empty_text_rejected(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="   ")
    assert result["ok"] is False
    assert result["error"] == "empty_text"


# ---------- async execute_pending ----------


@pytest.mark.asyncio
async def test_execute_pending_propose_action_local(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    approval_id = "chat-local-1"
    store.save(approval_id, {
        "approval_id": approval_id,
        "intent": {
            "type": "propose_action",
            "action_id": "check_disk",
            "target": "local",
            "text": "x",
        },
    })
    svc, _ = _make_service(
        local_result=_FakeLocalResult(
            action_id="check_disk", returncode=0, stdout="Filesystem ...",
            stderr="", timed_out=False, duration_ms=5,
        ),
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id=approval_id)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["target_kind"] == "local"
    assert result["action_id"] == "check_disk"
    # pending record removed
    assert store.get(approval_id) is None


@pytest.mark.asyncio
async def test_execute_pending_propose_action_ssh(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    approval_id = "chat-ssh-1"
    store.save(approval_id, {
        "approval_id": approval_id,
        "intent": {
            "type": "propose_action",
            "action_id": "restart_nginx",
            "target": "ssh",
            "text": "x",
        },
    })
    svc, _ = _make_service(
        ssh_result=_FakeSshResult(
            action_id="restart_nginx", host="1.2.3.4", user="deploy", port=22,
            returncode=0, stdout="ok", stderr="", timed_out=False, duration_ms=50,
        ),
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id=approval_id)
    assert result["ok"] is True
    assert result["target_kind"] == "ssh"
    assert result["host"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_execute_pending_propose_new_action_local(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    approval_id = "chat-adhoc-1"
    store.save(approval_id, {
        "approval_id": approval_id,
        "intent": {
            "type": "propose_new_action",
            "target": "local",
            "argv": ["echo", "hi"],
            "timeout_sec": 5,
            "risk_level": "low",
        },
    })
    svc, _ = _make_service(
        local_result=_FakeLocalResult(
            action_id="", returncode=0, stdout="hi\n",
            stderr="", timed_out=False, duration_ms=2,
        ),
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id=approval_id)
    assert result["ok"] is True
    assert result["stdout"].strip() == "hi"
    # adhoc execution logs to chat memory
    messages = ChatMemory(str(tmp_path)).load_messages()
    assert any(m.role == "exec" for m in messages)


@pytest.mark.asyncio
async def test_execute_pending_propose_new_action_ssh(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    _write_ssh_yaml(
        tmp_path,
        {"prod": {"host": "9.9.9.9", "user": "root", "port": 2222}},
    )
    store = ChatPendingStore(str(tmp_path))
    approval_id = "chat-adhoc-ssh"
    store.save(approval_id, {
        "approval_id": approval_id,
        "intent": {
            "type": "propose_new_action",
            "target": "prod",
            "argv": ["uptime"],
            "timeout_sec": 5,
            "risk_level": "low",
        },
    })
    svc, _ = _make_service(
        ssh_result=_FakeSshResult(
            action_id="", host="9.9.9.9", user="root", port=2222,
            returncode=0, stdout="up 1 day", stderr="",
            timed_out=False, duration_ms=3,
        ),
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id=approval_id)
    assert result["ok"] is True
    assert result["host"] == "9.9.9.9"


@pytest.mark.asyncio
async def test_execute_pending_not_found(tmp_path):
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-ghost")
    assert result["ok"] is False
    assert result["error"] == "approval_not_found"


@pytest.mark.asyncio
async def test_execute_pending_corrupt_intent(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-bad", {"approval_id": "chat-bad"})
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-bad")
    assert result["ok"] is False
    assert result["error"] == "pending_payload_corrupt"


@pytest.mark.asyncio
async def test_execute_pending_propose_plan_empty_steps(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan", {
        "approval_id": "chat-plan",
        "intent": {"type": "propose_plan", "steps": []},
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan")
    assert result["ok"] is False
    assert result["error"] == "plan_steps_missing"


@pytest.mark.asyncio
async def test_execute_pending_ssh_alias_unknown(tmp_path):
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-adhoc-unknown", {
        "approval_id": "chat-adhoc-unknown",
        "intent": {
            "type": "propose_new_action",
            "target": "nowhere",
            "argv": ["uptime"],
            "timeout_sec": 5,
            "risk_level": "low",
        },
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-adhoc-unknown")
    assert result["ok"] is False
    assert result["error"].startswith("ssh_alias_unknown")


# ---------- propose_plan execution ----------


@pytest.mark.asyncio
async def test_execute_pending_plan_all_steps_local(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-ok", {
        "approval_id": "chat-plan-ok",
        "intent": {
            "type": "propose_plan",
            "text": "check disks twice",
            "steps": [
                {"target": "local", "action_id": "check_disk"},
                {"target": "local", "argv": ["echo", "done"], "timeout_sec": 5},
            ],
            "stop_on_error": True,
        },
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-ok")
    assert result["ok"] is True
    assert result["target_kind"] == "plan"
    assert result["total_steps"] == 2
    assert result["completed_steps"] == 2
    assert result["stopped_early"] is False
    assert [s["step_index"] for s in result["steps"]] == [1, 2]
    assert all(s["ok"] for s in result["steps"])
    assert result["steps"][0]["action_id"] == "check_disk"
    assert result["steps"][1]["argv"] == ["echo", "done"]
    assert result.get("runbook_saved") is False


@pytest.mark.asyncio
async def test_execute_pending_plan_stops_on_error(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-fail", {
        "approval_id": "chat-plan-fail",
        "intent": {
            "type": "propose_plan",
            "steps": [
                {"target": "local", "action_id": "check_disk"},
                {"target": "local", "argv": ["echo", "never"]},
            ],
            "stop_on_error": True,
        },
    })
    svc, _ = _make_service(
        local_result=_FakeLocalResult(
            action_id="", returncode=1, stdout="", stderr="boom",
            timed_out=False, duration_ms=5,
        )
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-fail")
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["total_steps"] == 2
    assert result["completed_steps"] == 1
    assert result["steps"][0]["exit_code"] == 1


@pytest.mark.asyncio
async def test_execute_pending_plan_continues_when_stop_on_error_false(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-continue", {
        "approval_id": "chat-plan-continue",
        "intent": {
            "type": "propose_plan",
            "steps": [
                {"target": "local", "action_id": "check_disk"},
                {"target": "local", "argv": ["echo", "next"]},
            ],
            "stop_on_error": False,
        },
    })
    svc, _ = _make_service(
        local_result=_FakeLocalResult(
            action_id="", returncode=2, stdout="", stderr="err",
            timed_out=False, duration_ms=5,
        )
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-continue")
    assert result["ok"] is False
    assert result["stopped_early"] is False
    assert result["completed_steps"] == 2


@pytest.mark.asyncio
async def test_execute_pending_plan_ssh_step_uses_config(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    _write_ssh_yaml(tmp_path, {"prod": {"host": "10.0.0.1", "user": "ops", "port": 2222}})
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-ssh", {
        "approval_id": "chat-plan-ssh",
        "intent": {
            "type": "propose_plan",
            "steps": [
                {"target": "prod", "argv": ["uptime"], "timeout_sec": 10},
            ],
        },
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-ssh")
    assert result["ok"] is True
    step = result["steps"][0]
    assert step["target_kind"] == "ssh"
    assert step["host"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_execute_pending_plan_rejects_unknown_ssh_alias(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-bad-host", {
        "approval_id": "chat-plan-bad-host",
        "intent": {
            "type": "propose_plan",
            "steps": [
                {"target": "ghost", "argv": ["uptime"]},
            ],
        },
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-bad-host")
    assert result["ok"] is False
    assert result["stopped_early"] is True
    assert result["steps"][0]["error"].startswith("ssh_alias_unknown")


@pytest.mark.asyncio
async def test_execute_pending_plan_saves_runbook_on_success(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-rb", {
        "approval_id": "chat-plan-rb",
        "intent": {
            "type": "propose_plan",
            "text": "rolling restart",
            "steps": [
                {"target": "local", "action_id": "check_disk"},
            ],
            "suggest_save_as_runbook": True,
            "suggested_runbook_id": "rolling-restart",
        },
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-rb")
    assert result["ok"] is True
    assert result["runbook_saved"] is True
    runbook_path = Path(result["runbook_path"])
    assert runbook_path.exists()
    text = runbook_path.read_text(encoding="utf-8")
    assert "id: rolling-restart" in text
    assert "rolling restart" in text


@pytest.mark.asyncio
async def test_execute_pending_plan_skips_runbook_on_failure(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-fail-rb", {
        "approval_id": "chat-plan-fail-rb",
        "intent": {
            "type": "propose_plan",
            "steps": [{"target": "local", "action_id": "check_disk"}],
            "suggest_save_as_runbook": True,
            "suggested_runbook_id": "failed-plan",
            "stop_on_error": True,
        },
    })
    svc, _ = _make_service(
        local_result=_FakeLocalResult(
            action_id="", returncode=1, stdout="", stderr="",
            timed_out=False, duration_ms=3,
        )
    )
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-fail-rb")
    assert result["ok"] is False
    assert result.get("runbook_saved") is False


@pytest.mark.asyncio
async def test_execute_pending_plan_sanitizes_runbook_id(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)
    store = ChatPendingStore(str(tmp_path))
    store.save("chat-plan-bad-id", {
        "approval_id": "chat-plan-bad-id",
        "intent": {
            "type": "propose_plan",
            "steps": [{"target": "local", "action_id": "check_disk"}],
            "suggest_save_as_runbook": True,
            "suggested_runbook_id": "../../etc/passwd",
        },
    })
    svc, _ = _make_service()
    session = _make_session(tmp_path)
    result = await svc.execute_pending(session=session, approval_id="chat-plan-bad-id")
    assert result["ok"] is True
    assert result["runbook_saved"] is True
    # no parent traversal in path, safe chars only
    saved_path = Path(result["runbook_path"])
    assert saved_path.name == "etcpasswd.md"
    assert ".cli-proxy/.admin/runbooks" in str(saved_path)


# ---------- PR-4: autopilot ----------


def _admin_cfg_with_autopilot(**autonomy) -> Dict[str, Any]:
    cfg = {
        **_ADMIN_CFG,
        "autonomy": {"enabled": True, **autonomy},
    }
    return cfg


@pytest.mark.asyncio
async def test_send_autopilot_disabled_falls_back_to_pending(tmp_path):
    _write_admin_config(tmp_path, _ADMIN_CFG)  # no autonomy block → disabled
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_action",
                "action_id": "check_disk",
                "target": "local",
                "text": "x",
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="check")
    assert result["auto_exec"] is False
    # reason == "autopilot disabled" → autopilot_blocked возвращается как None (чтобы UI не показывал плашку для кейса «autopilot выключен»)
    assert result["autopilot_blocked"] is None
    assert result["pending_action_id"]
    assert result["exec_result"] is None


@pytest.mark.asyncio
async def test_send_autopilot_action_allowlisted_executes_without_pending(tmp_path):
    _write_admin_config(tmp_path, _admin_cfg_with_autopilot(auto_exec_actions=["check_disk"]))
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_action",
                "action_id": "check_disk",
                "target": "local",
                "text": "go",
            })
        ],
        local_result=_FakeLocalResult(
            action_id="check_disk", returncode=0, stdout="Filesystem",
            stderr="", timed_out=False, duration_ms=7,
        ),
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="go")
    assert result["auto_exec"] is True
    assert result["pending_action_id"] is None
    assert result["exec_result"]["ok"] is True
    assert result["exec_result"]["action_id"] == "check_disk"
    assert svc.list_pending(str(tmp_path)) == []
    # autopilot executed event in chat memory
    messages = ChatMemory(str(tmp_path)).load_messages()
    assert any(m.intent_type == "intent_autopilot_executed" for m in messages)


@pytest.mark.asyncio
async def test_send_autopilot_action_not_in_allowlist_blocked(tmp_path):
    _write_admin_config(tmp_path, _admin_cfg_with_autopilot(auto_exec_actions=["other_action"]))
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_action",
                "action_id": "check_disk",
                "target": "local",
                "text": "go",
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="go")
    assert result["auto_exec"] is False
    assert result["autopilot_blocked"] and "check_disk" in result["autopilot_blocked"]
    approval_id = result["pending_action_id"]
    assert approval_id
    pending_items = svc.list_pending(str(tmp_path))
    record = next((r for r in pending_items if r["approval_id"] == approval_id), None)
    assert record is not None
    assert record.get("autopilot_blocked") and "check_disk" in record["autopilot_blocked"]
    messages = ChatMemory(str(tmp_path)).load_messages()
    assert any(m.intent_type == "intent_autopilot_blocked" for m in messages)


@pytest.mark.asyncio
async def test_send_autopilot_adhoc_argv_allowlisted(tmp_path):
    _write_admin_config(tmp_path, _admin_cfg_with_autopilot(auto_exec_adhoc_commands=["ls"]))
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_new_action",
                "argv": ["ls", "-la"],
                "target": "local",
                "rationale": "list",
                "risk_level": "low",
                "text": "go",
            })
        ],
        local_result=_FakeLocalResult(
            action_id="", returncode=0, stdout="total",
            stderr="", timed_out=False, duration_ms=3,
        ),
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="go")
    assert result["auto_exec"] is True
    assert result["pending_action_id"] is None
    assert result["exec_result"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_send_autopilot_adhoc_argv_blocked(tmp_path):
    _write_admin_config(tmp_path, _admin_cfg_with_autopilot(auto_exec_adhoc_commands=["ls"]))
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_new_action",
                "argv": ["cat", "/etc/hosts"],
                "target": "local",
                "rationale": "peek",
                "risk_level": "low",
                "text": "go",
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="go")
    assert result["auto_exec"] is False
    assert result["autopilot_blocked"] and "cat" in result["autopilot_blocked"]


@pytest.mark.asyncio
async def test_send_autopilot_plan_all_steps_pass(tmp_path):
    _write_admin_config(
        tmp_path,
        _admin_cfg_with_autopilot(
            auto_exec_actions=["check_disk"],
            auto_exec_adhoc_commands=["ls"],
        ),
    )
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_plan",
                "text": "plan",
                "steps": [
                    {"target": "local", "action_id": "check_disk"},
                    {"target": "local", "argv": ["ls", "-la"]},
                ],
                "stop_on_error": False,  # autopilot должен форсировать True
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="go")
    assert result["auto_exec"] is True
    assert result["pending_action_id"] is None
    assert result["exec_result"]["target_kind"] == "plan"
    assert result["exec_result"]["completed_steps"] == 2
    assert result["exec_result"]["ok"] is True


@pytest.mark.asyncio
async def test_send_autopilot_plan_blocked_when_any_step_not_allowlisted(tmp_path):
    _write_admin_config(
        tmp_path,
        _admin_cfg_with_autopilot(auto_exec_actions=["check_disk"]),
    )
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_plan",
                "text": "plan",
                "steps": [
                    {"target": "local", "action_id": "check_disk"},
                    {"target": "local", "argv": ["rm", "-rf", "/tmp/x"]},
                ],
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="go")
    assert result["auto_exec"] is False
    assert result["autopilot_blocked"] and "step 2" in result["autopilot_blocked"]
    assert result["pending_action_id"]
    # Ни один шаг не был выполнен — autopilot blocked целиком
    messages = ChatMemory(str(tmp_path)).load_messages()
    assert all(m.intent_type != "chat_plan_step" for m in messages)


@pytest.mark.asyncio
async def test_send_autopilot_per_server_override(tmp_path):
    """per-server block разрешает action только на web-01; для local должно падать в pending."""
    admin_cfg = {
        **_ADMIN_CFG,
        "autonomy": {
            "enabled": True,
            "auto_exec_actions": [],  # глобально пусто
            "per_server": {
                "web-01": {"auto_exec_actions": ["check_disk"]},
            },
        },
    }
    _write_admin_config(tmp_path, admin_cfg)
    svc, _ = _make_service(
        llm_responses=[
            json.dumps({
                "type": "propose_action",
                "action_id": "check_disk",
                "target": "local",
                "text": "x",
            })
        ]
    )
    session = _make_session(tmp_path)
    result = await svc.send(session=session, bot_app=SimpleNamespace(config=None), text="x")
    assert result["auto_exec"] is False
    assert result["autopilot_blocked"] and "check_disk" in result["autopilot_blocked"]
