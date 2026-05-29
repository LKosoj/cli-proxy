"""Tests for SSH tool plugins (SSHExecTool, SSHLongTool, SSHCancelTool)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.plugins.ssh_exec import SSHCancelTool, SSHExecTool, SSHLongTool
from app.services.ssh_service import SSHExecResult


def _mock_ssh_service():
    svc = MagicMock()
    svc.exec = AsyncMock(return_value=SSHExecResult(stdout="ok\n", stderr="", exit_code=0))
    svc.stream = AsyncMock(return_value=SSHExecResult(stdout="line1\nline2\n", stderr="", exit_code=0))
    svc.cancel = AsyncMock(return_value=True)
    return svc


def _ctx(cwd="/tmp/proj", ssh_enabled=True, chat_id=1):
    session = SimpleNamespace()
    from session import ModeState
    session.modes = ModeState(ssh_remote_enabled=ssh_enabled)
    return {"cwd": cwd, "session": session, "chat_id": chat_id}


# ---------------------------------------------------------------------------
# SSHExecTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ssh_exec_returns_output():
    tool = SSHExecTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute({"host": "prod", "command": "ls"}, _ctx())
    assert result["success"] is True
    assert "ok" in result["output"]


@pytest.mark.asyncio
async def test_ssh_exec_blocked_when_disabled():
    tool = SSHExecTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute(
        {"host": "prod", "command": "ls"},
        _ctx(ssh_enabled=False),
    )
    assert result["success"] is False
    assert "disabled" in result["error"].lower()


@pytest.mark.asyncio
async def test_ssh_exec_missing_args():
    tool = SSHExecTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute({"host": "", "command": ""}, _ctx())
    assert result["success"] is False
    assert "required" in result["error"].lower()


@pytest.mark.asyncio
async def test_ssh_exec_no_service():
    tool = SSHExecTool()
    tool.initialize(services={})
    result = await tool.execute({"host": "prod", "command": "ls"}, _ctx())
    assert result["success"] is False
    assert "not available" in result["error"].lower()


@pytest.mark.asyncio
async def test_ssh_exec_permission_error():
    svc = _mock_ssh_service()
    svc.exec = AsyncMock(side_effect=PermissionError("Chat 999 not allowed"))
    tool = SSHExecTool()
    tool.initialize(services={"ssh": svc})
    result = await tool.execute({"host": "prod", "command": "ls"}, _ctx())
    assert result["success"] is False
    assert "not allowed" in result["error"]


def test_ssh_exec_spec():
    tool = SSHExecTool()
    spec = tool.get_spec()
    assert spec.name == "ssh_exec"
    assert "host" in spec.parameters["properties"]
    assert "command" in spec.parameters["properties"]


# ---------------------------------------------------------------------------
# SSHLongTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ssh_long_returns_output():
    tool = SSHLongTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute({"host": "prod", "command": "tail -f log"}, _ctx())
    assert result["success"] is True
    assert "line1" in result["output"]


@pytest.mark.asyncio
async def test_ssh_long_blocked_when_disabled():
    tool = SSHLongTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute(
        {"host": "prod", "command": "deploy.sh"},
        _ctx(ssh_enabled=False),
    )
    assert result["success"] is False
    assert "disabled" in result["error"].lower()


def test_ssh_long_spec():
    tool = SSHLongTool()
    spec = tool.get_spec()
    assert spec.name == "ssh_long"
    assert "host" in spec.parameters["properties"]
    assert "max_duration_sec" in spec.parameters["properties"]


# ---------------------------------------------------------------------------
# SSHCancelTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ssh_cancel_success():
    tool = SSHCancelTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute({"host": "prod"}, _ctx())
    assert result["success"] is True
    assert "cancelled" in result["output"].lower()


@pytest.mark.asyncio
async def test_ssh_cancel_no_active():
    svc = _mock_ssh_service()
    svc.cancel = AsyncMock(return_value=False)
    tool = SSHCancelTool()
    tool.initialize(services={"ssh": svc})
    result = await tool.execute({"host": "prod"}, _ctx())
    assert result["success"] is False
    assert "no active" in result["error"].lower()


@pytest.mark.asyncio
async def test_ssh_cancel_blocked_when_disabled():
    tool = SSHCancelTool()
    tool.initialize(services={"ssh": _mock_ssh_service()})
    result = await tool.execute(
        {"host": "prod"},
        _ctx(ssh_enabled=False),
    )
    assert result["success"] is False
    assert "disabled" in result["error"].lower()


def test_ssh_cancel_spec():
    tool = SSHCancelTool()
    spec = tool.get_spec()
    assert spec.name == "ssh_cancel"
    assert "host" in spec.parameters["properties"]
