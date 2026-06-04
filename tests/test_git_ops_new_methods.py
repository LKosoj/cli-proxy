"""Тесты новых методов GitOps: git_branch_create, git_checkout, git_stash_pop, git_show."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from session import GitState


def _make_session() -> SimpleNamespace:
    git = GitState()
    git.busy = False
    tool = SimpleNamespace(name="mock-tool")
    session = SimpleNamespace(
        git=git,
        busy=False,
        name="test-session",
        tool=tool,
        workdir="/tmp/repo",
        id="sess-1",
    )
    session.is_active_by_tick = lambda: False
    return session


def _make_git_ops(run_result: tuple[int, str] = (0, "ok")) -> object:
    from app.services.git_ops_service import GitOps

    git_ops = GitOps.__new__(GitOps)
    git_ops._send_message = AsyncMock()
    git_ops.manager = MagicMock()
    git_ops.config = MagicMock()
    git_ops._git_askpass_path = None
    git_ops._run_git = AsyncMock(return_value=run_result)
    return git_ops


@pytest.mark.asyncio
async def test_git_branch_create_calls_checkout_b():
    git_ops = _make_git_ops((0, "Switched to a new branch 'feature'"))
    session = _make_session()

    code, out = await git_ops.git_branch_create(session, "feature")

    assert code == 0
    git_ops._run_git.assert_awaited_once_with(session, ["checkout", "-b", "feature"])


@pytest.mark.asyncio
async def test_git_checkout_calls_checkout():
    git_ops = _make_git_ops((0, "Switched to branch 'main'"))
    session = _make_session()

    code, out = await git_ops.git_checkout(session, "main")

    assert code == 0
    git_ops._run_git.assert_awaited_once_with(session, ["checkout", "main"])


@pytest.mark.asyncio
async def test_git_stash_pop_calls_stash_pop():
    git_ops = _make_git_ops((0, ""))
    session = _make_session()

    code, _out = await git_ops.git_stash_pop(session)

    assert code == 0
    git_ops._run_git.assert_awaited_once_with(session, ["stash", "pop"])


@pytest.mark.asyncio
async def test_git_show_defaults_to_head():
    git_ops = _make_git_ops((0, "commit abc"))
    session = _make_session()

    code, out = await git_ops.git_show(session)

    assert code == 0
    git_ops._run_git.assert_awaited_once_with(session, ["--no-pager", "show", "--stat", "HEAD"])


@pytest.mark.asyncio
async def test_git_show_with_explicit_ref():
    git_ops = _make_git_ops((0, "commit def"))
    session = _make_session()

    code, out = await git_ops.git_show(session, "abc123")

    git_ops._run_git.assert_awaited_once_with(session, ["--no-pager", "show", "--stat", "abc123"])


@pytest.mark.asyncio
async def test_git_branch_create_propagates_error_code():
    git_ops = _make_git_ops((128, "fatal: branch already exists"))
    session = _make_session()

    code, out = await git_ops.git_branch_create(session, "dup")

    assert code == 128
    assert "already exists" in out
