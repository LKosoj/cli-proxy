"""Verification: set/is_ssh_remote_enabled atomicity under concurrent access.

The accessors operate on in-memory Python attributes (ModeState dataclass),
so true thread-level races are unlikely in CPython due to the GIL.  These
tests verify correctness under concurrent *coroutine* access patterns that
mirror real bot usage (multiple async handlers toggling the flag on the
same session object simultaneously).
"""

import asyncio
from types import SimpleNamespace

import pytest

from session import ModeState
from sessions.session_state_access import is_ssh_remote_enabled, set_ssh_remote_enabled


@pytest.mark.asyncio
async def test_concurrent_toggle_converges():
    """Many coroutines toggling the flag; final state matches last write."""
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))

    async def toggle(value: bool, delay: float) -> None:
        await asyncio.sleep(delay)
        set_ssh_remote_enabled(session, value)

    tasks = []
    for i in range(50):
        tasks.append(toggle(True, 0.001 * i))
    # Last write is True
    await asyncio.gather(*tasks)
    assert is_ssh_remote_enabled(session) is True


@pytest.mark.asyncio
async def test_concurrent_set_and_read():
    """Reads never see a corrupted (non-bool) value during concurrent writes."""
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))
    violations: list[str] = []

    async def writer() -> None:
        for _ in range(100):
            set_ssh_remote_enabled(session, True)
            set_ssh_remote_enabled(session, False)
            await asyncio.sleep(0)

    async def reader() -> None:
        for _ in range(200):
            val = is_ssh_remote_enabled(session)
            if not isinstance(val, bool):
                violations.append(f"non-bool: {val!r}")
            await asyncio.sleep(0)

    await asyncio.gather(writer(), reader())
    assert violations == [], f"corrupted reads: {violations}"


@pytest.mark.asyncio
async def test_independent_sessions_no_crosstalk():
    """Two sessions toggled concurrently don't interfere."""
    s1 = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))
    s2 = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    async def flip_s1() -> None:
        for _ in range(100):
            set_ssh_remote_enabled(s1, True)
            await asyncio.sleep(0)
            set_ssh_remote_enabled(s1, False)
            await asyncio.sleep(0)

    async def flip_s2() -> None:
        for _ in range(100):
            set_ssh_remote_enabled(s2, False)
            await asyncio.sleep(0)
            set_ssh_remote_enabled(s2, True)
            await asyncio.sleep(0)

    await asyncio.gather(flip_s1(), flip_s2())

    # After all flips, s1 ends on False (last write), s2 ends on True
    assert is_ssh_remote_enabled(s1) is False
    assert is_ssh_remote_enabled(s2) is True


@pytest.mark.asyncio
async def test_rapid_set_get_roundtrip():
    """Rapid set→get always returns the just-set value (no stale reads)."""
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))

    for _ in range(1000):
        set_ssh_remote_enabled(session, True)
        assert is_ssh_remote_enabled(session) is True
        set_ssh_remote_enabled(session, False)
        assert is_ssh_remote_enabled(session) is False
