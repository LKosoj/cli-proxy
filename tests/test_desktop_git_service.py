import subprocess
from types import SimpleNamespace

import pytest

from desktop.services.desktop_git_service import DesktopGitService


def _run(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.asyncio
async def test_desktop_git_service_status_uses_session_cwd(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], str(repo))

    (repo / "a.txt").write_text("x", encoding="utf-8")
    session = SimpleNamespace(workdir=str(repo))

    svc = DesktopGitService(timeout_sec=5)
    code, out = await svc.status_text(session)

    assert code == 0
    assert "a.txt" in out


@pytest.mark.asyncio
async def test_desktop_git_service_commit_adds_all_and_commits(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], str(repo))
    _run(["git", "config", "user.email", "test@example.com"], str(repo))
    _run(["git", "config", "user.name", "Test"], str(repo))

    (repo / "a.txt").write_text("x", encoding="utf-8")
    session = SimpleNamespace(workdir=str(repo))

    svc = DesktopGitService(timeout_sec=5)
    code, out = await svc.commit(session, "initial")

    assert code == 0
    assert out  # git usually prints commit summary

    code2, out2 = await svc.status_text(session)
    assert code2 == 0
    assert "a.txt" not in out2  # should be clean (no staged/untracked)


@pytest.mark.asyncio
async def test_desktop_git_service_push_returns_error_tuple_when_no_remote(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], str(repo))
    session = SimpleNamespace(workdir=str(repo))

    svc = DesktopGitService(timeout_sec=5)
    code, out = await svc.push(session)

    assert isinstance(code, int)
    assert isinstance(out, str)
    assert code != 0


@pytest.mark.asyncio
async def test_desktop_git_service_handles_subprocess_exception(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session = SimpleNamespace(workdir=str(repo))

    def _boom(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", _boom)

    svc = DesktopGitService(timeout_sec=5)
    code, out = await svc.status_text(session)

    assert code != 0
    assert "OSError" in out or "boom" in out
