from __future__ import annotations

import asyncio

import pytest

from agent.plugins.run_command import RunCommandTool
from agent.tooling import helpers


@pytest.fixture(autouse=True)
def _reset_pending_commands(monkeypatch):
    helpers._PENDING_COMMANDS.clear()
    helpers._PENDING_COMMAND_WAITERS.clear()
    helpers._PENDING_COMMAND_DECISIONS.clear()
    monkeypatch.setattr(helpers, "_APPROVAL_CALLBACK", None, raising=False)


@pytest.mark.asyncio
async def test_run_command_waits_for_approval_then_executes(monkeypatch):
    tool = RunCommandTool()
    tool.initialize(config=None, services={})

    monkeypatch.setattr(helpers, "check_command", lambda *_args, **_kwargs: (True, False, "Dangerous"))

    async def _fake_exec(command: str, cwd: str):
        return {"success": True, "output": f"executed {command} @ {cwd}"}

    monkeypatch.setattr(helpers, "execute_shell_command", _fake_exec)

    issued = {}

    def _approval_callback(chat_id: int, cmd_id: str, cmd: str, reason: str) -> None:
        issued["chat_id"] = int(chat_id)
        issued["cmd_id"] = str(cmd_id)
        issued["cmd"] = str(cmd)
        issued["reason"] = str(reason)

        async def _approve_later() -> None:
            await asyncio.sleep(0.01)
            helpers.approve_pending_command(cmd_id)

        asyncio.get_running_loop().create_task(_approve_later())

    monkeypatch.setattr(helpers, "_APPROVAL_CALLBACK", _approval_callback, raising=False)

    result = await tool.execute(
        {"command": "rm -rf ./tmp"},
        {"cwd": "/tmp", "session_id": "s1", "chat_id": 101, "chat_type": "private"},
    )

    assert result["success"] is True
    assert "executed rm -rf ./tmp @ /tmp" == result.get("output")
    assert issued["chat_id"] == 101
    assert issued["cmd"] == "rm -rf ./tmp"
    assert helpers.pop_pending_command(issued["cmd_id"]) is None


@pytest.mark.asyncio
async def test_run_command_waits_for_deny_and_returns_error(monkeypatch):
    tool = RunCommandTool()
    tool.initialize(config=None, services={})

    monkeypatch.setattr(helpers, "check_command", lambda *_args, **_kwargs: (True, False, "Dangerous"))

    def _approval_callback(_chat_id: int, cmd_id: str, _cmd: str, _reason: str) -> None:
        async def _deny_later() -> None:
            await asyncio.sleep(0.01)
            helpers.deny_pending_command(cmd_id)

        asyncio.get_running_loop().create_task(_deny_later())

    monkeypatch.setattr(helpers, "_APPROVAL_CALLBACK", _approval_callback, raising=False)

    result = await tool.execute(
        {"command": "sudo rm -rf ./tmp"},
        {"cwd": "/tmp", "session_id": "s1", "chat_id": 101, "chat_type": "private"},
    )

    assert result["success"] is False
    assert result["approval_required"] is True
    assert "denied" in str(result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_run_command_requires_approval_channel(monkeypatch):
    tool = RunCommandTool()
    tool.initialize(config=None, services={})

    monkeypatch.setattr(helpers, "check_command", lambda *_args, **_kwargs: (True, False, "Dangerous"))

    result = await tool.execute(
        {"command": "rm -rf ./tmp"},
        {"cwd": "/tmp", "session_id": "s1", "chat_id": 101, "chat_type": "private"},
    )

    assert result["success"] is False
    assert result["approval_required"] is True
    assert "approval channel is unavailable" in str(result.get("error") or "").lower()
    assert helpers._PENDING_COMMANDS == {}
