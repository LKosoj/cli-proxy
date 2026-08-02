import asyncio
import logging
import time

import pytest

from app.services.loop_lag_monitor import LoopLagMonitor


@pytest.mark.asyncio
async def test_loop_lag_monitor_reports_stack_of_blocking_code(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.loop_lag_monitor")
    monitor = LoopLagMonitor(tick_interval_sec=0.02, warn_threshold_sec=0.1, repeat_sec=0.1)
    monitor.start()
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.4)
        await asyncio.sleep(0.05)
    finally:
        await monitor.stop()

    messages = [record.getMessage() for record in caplog.records]
    lag_messages = [message for message in messages if "event loop заблокирован" in message]
    assert lag_messages
    assert "test_loop_lag_monitor_reports_stack_of_blocking_code" in lag_messages[0]


@pytest.mark.asyncio
async def test_loop_lag_monitor_stays_quiet_when_loop_is_responsive(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.loop_lag_monitor")
    monitor = LoopLagMonitor(tick_interval_sec=0.02, warn_threshold_sec=0.3, repeat_sec=0.3)
    monitor.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0.01)
    finally:
        await monitor.stop()

    assert not [record for record in caplog.records if "event loop заблокирован" in record.getMessage()]


@pytest.mark.asyncio
async def test_loop_lag_monitor_stop_is_idempotent_and_releases_watchdog() -> None:
    monitor = LoopLagMonitor(tick_interval_sec=0.02, warn_threshold_sec=0.1)
    monitor.start()
    watchdog = monitor._watchdog
    assert watchdog is not None

    await monitor.stop()
    await monitor.stop()

    assert not watchdog.is_alive()
    assert monitor._heartbeat_task is None
