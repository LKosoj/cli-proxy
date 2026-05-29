"""Tests for SSHConnectionWrapper idle-timer and lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.ssh_service import SSHConnectionWrapper


def _make_mock_conn():
    """Build a mock asyncssh connection."""
    conn = MagicMock()
    conn.get_extra_info = MagicMock(return_value=MagicMock())
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()

    completed = SimpleNamespace(
        stdout="ok\n", stderr="", exit_status=0,
        returncode=0, env=None, command="echo ok",
        subsystem=None, exit_signal=None,
    )
    conn.run = AsyncMock(return_value=completed)
    return conn, completed


@pytest.mark.asyncio
async def test_wrapper_is_open_initially():
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    assert wrapper.is_open is True
    await wrapper.close()


@pytest.mark.asyncio
async def test_wrapper_is_closed_after_close():
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    await wrapper.close()
    assert wrapper.is_open is False


@pytest.mark.asyncio
async def test_close_is_idempotent():
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    await wrapper.close()
    await wrapper.close()
    assert conn.close.call_count == 1


@pytest.mark.asyncio
async def test_run_delegates_to_conn():
    conn, completed = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    result = await wrapper.run("echo ok")
    conn.run.assert_awaited_once_with("echo ok")
    assert result.stdout == "ok\n"
    await wrapper.close()


@pytest.mark.asyncio
async def test_idle_timeout_closes_connection():
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=1)
    callback_called = []
    wrapper.on_idle_close = lambda: callback_called.append(True)

    # Wait for idle timeout to fire
    await asyncio.sleep(1.3)

    assert wrapper.is_open is False
    assert callback_called == [True]


@pytest.mark.asyncio
async def test_run_resets_idle_timer():
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=1)

    # Run a command at 0.7s — should reset the timer
    await asyncio.sleep(0.7)
    await wrapper.run("ls")

    # At 1.3s from start (0.6s after reset) — should still be open
    await asyncio.sleep(0.6)
    assert wrapper.is_open is True

    # Wait for full idle timeout after last command
    await asyncio.sleep(0.6)
    assert wrapper.is_open is False
    await wrapper.close()


@pytest.mark.asyncio
async def test_cancel_active_no_process():
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    assert wrapper.cancel_active() is False
    await wrapper.close()


@pytest.mark.asyncio
async def test_cancel_active_sends_signal():
    conn, _ = _make_mock_conn()
    mock_proc = MagicMock()
    mock_proc.send_signal = MagicMock()
    conn.create_process = AsyncMock(return_value=mock_proc)

    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    await wrapper.start_process("tail -f /var/log/syslog")
    assert wrapper.cancel_active() is True
    mock_proc.send_signal.assert_called_once_with("INT")
    await wrapper.close()


@pytest.mark.asyncio
async def test_is_open_false_when_transport_none():
    conn, _ = _make_mock_conn()
    conn.get_extra_info = MagicMock(return_value=None)
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    assert wrapper.is_open is False
    await wrapper.close()


@pytest.mark.asyncio
async def test_wrapper_api_contract():
    """Verify SSHConnectionWrapper exposes the expected public API."""
    conn, _ = _make_mock_conn()
    wrapper = SSHConnectionWrapper(conn, idle_timeout_sec=60)
    assert hasattr(wrapper, "is_open")
    assert hasattr(wrapper, "run")
    assert hasattr(wrapper, "start_process")
    assert hasattr(wrapper, "cancel_active")
    assert hasattr(wrapper, "close")
    assert hasattr(wrapper, "on_idle_close")
    await wrapper.close()
