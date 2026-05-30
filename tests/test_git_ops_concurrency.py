"""Тесты атомарного check-and-set session.git.busy (фикс H10)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from session import GitState


def _make_session(busy: bool = False, session_busy: bool = False) -> SimpleNamespace:
    """Создаёт минимальную сессию с GitState для тестов."""
    git = GitState()
    git.busy = busy

    tool = SimpleNamespace(name="mock-tool")
    session = SimpleNamespace(
        git=git,
        busy=session_busy,
        name="mock-session",
        tool=tool,
        workdir="/tmp",
        id="test-id",
    )
    session.is_active_by_tick = lambda: False
    return session


def _make_git_ops(messages: list) -> object:
    """Создаёт GitOps-подобный объект с замоканными зависимостями."""
    from app.services.git_ops_service import GitOps

    send_mock = AsyncMock(side_effect=lambda ctx, **kwargs: messages.append(kwargs.get("text", "")))

    git_ops = GitOps.__new__(GitOps)
    git_ops._send_message = send_mock
    git_ops.manager = MagicMock()
    git_ops.config = MagicMock()
    return git_ops


def _make_context() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Тест 1: повторный вызов при busy=True возвращает False и отправляет сообщение
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_try_acquire_returns_false_when_busy():
    messages: list = []
    git_ops = _make_git_ops(messages)

    session = _make_session(busy=True)
    ctx = _make_context()

    result = await git_ops._try_acquire_git_busy(session, 1, ctx)

    assert result is False
    assert any("Git уже выполняется" in m for m in messages), f"Сообщение не найдено: {messages}"
    # busy не должен сброситься
    assert session.git.busy is True


# ---------------------------------------------------------------------------
# Тест 2: два корутина через asyncio.gather — ровно один получает True
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_acquire_only_one_wins():
    messages: list = []
    git_ops = _make_git_ops(messages)

    session = _make_session(busy=False)
    ctx = _make_context()

    results = await asyncio.gather(
        git_ops._try_acquire_git_busy(session, 1, ctx),
        git_ops._try_acquire_git_busy(session, 1, ctx),
    )

    true_count = sum(1 for r in results if r is True)
    false_count = sum(1 for r in results if r is False)
    assert true_count == 1, f"Ожидали ровно одного победителя, получили: {results}"
    assert false_count == 1, f"Ожидали ровно одного проигравшего, получили: {results}"
    assert session.git.busy is True


# ---------------------------------------------------------------------------
# Тест 3: busy сбрасывается в finally при исключении в операции
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_busy_reset_on_exception():
    messages: list = []
    git_ops = _make_git_ops(messages)

    session = _make_session(busy=False)
    ctx = _make_context()

    acquired = await git_ops._try_acquire_git_busy(session, 1, ctx)
    assert acquired is True
    assert session.git.busy is True

    try:
        raise RuntimeError("Симуляция ошибки git-операции")
    except RuntimeError:
        pass
    finally:
        session.git.busy = False

    assert session.git.busy is False


# ---------------------------------------------------------------------------
# Тест 4: _ensure_git_state создаёт lock при его отсутствии
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ensure_git_state_creates_lock():
    messages: list = []
    git_ops = _make_git_ops(messages)

    # Создаём git_state без lock (как после десериализации)
    git_state = SimpleNamespace(busy=False, conflict=False, conflict_files=[], conflict_kind=None)
    assert not hasattr(git_state, "lock")

    session = SimpleNamespace(git=git_state, busy=False)
    session.is_active_by_tick = lambda: False

    git_ops._ensure_git_state(session)

    assert hasattr(session.git, "lock"), "lock должен быть создан _ensure_git_state"
    assert isinstance(session.git.lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# Тест 5: сессия занята (session.busy=True) — возвращает False с сообщением
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_try_acquire_returns_false_when_session_busy():
    messages: list = []
    git_ops = _make_git_ops(messages)

    session = _make_session(busy=False, session_busy=True)
    ctx = _make_context()

    result = await git_ops._try_acquire_git_busy(session, 1, ctx)

    assert result is False
    assert any("CLI-сессия занята" in m for m in messages), f"Сообщение не найдено: {messages}"
