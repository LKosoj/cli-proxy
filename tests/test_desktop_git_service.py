import subprocess
from types import SimpleNamespace

import pytest

from desktop.services.desktop_git_service import DesktopGitService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _run(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _make_repo(path) -> str:
    """Init a git repo with a first commit; return cwd string."""
    path.mkdir(exist_ok=True)
    _git(["init"], str(path))
    _git(["config", "user.email", "test@example.com"], str(path))
    _git(["config", "user.name", "Test"], str(path))
    (path / "readme.txt").write_text("init", encoding="utf-8")
    _git(["add", "-A"], str(path))
    _git(["commit", "-m", "init"], str(path))
    return str(path)


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


# ---------------------------------------------------------------------------
# Фича A: fetch / merge / rebase
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_no_remote_returns_nonzero(tmp_path) -> None:
    """fetch on a local repo without any remote must return an error tuple."""
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    svc = DesktopGitService(timeout_sec=5)
    code, out = await svc.fetch(session)
    assert isinstance(code, int)
    assert code != 0


@pytest.mark.asyncio
async def test_merge_ff_ok(tmp_path) -> None:
    """Merge a branch that is strictly ahead of HEAD returns status='ok'."""
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    _git(["checkout", "-b", "feature"], cwd)
    (tmp_path / "repo" / "feature.txt").write_text("new", encoding="utf-8")
    _git(["add", "-A"], cwd)
    _git(["commit", "-m", "feature"], cwd)
    _git(["checkout", "master" if _branch_exists(cwd, "master") else "main"], cwd)

    svc = DesktopGitService(timeout_sec=5)
    result = await svc.merge(session, "feature", strategy="ff")

    assert result["status"] == "ok"
    assert result["code"] == 0


@pytest.mark.asyncio
async def test_merge_nonexistent_branch_returns_error(tmp_path) -> None:
    """Merge a non-existent branch returns status='error'."""
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    svc = DesktopGitService(timeout_sec=5)
    result = await svc.merge(session, "no-such-branch")
    assert result["status"] == "error"
    assert result["code"] != 0


@pytest.mark.asyncio
async def test_rebase_nonexistent_branch_returns_error(tmp_path) -> None:
    """Rebase onto a non-existent branch returns status='error'."""
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    svc = DesktopGitService(timeout_sec=5)
    result = await svc.rebase(session, "no-such-branch")
    assert result["status"] == "error"
    assert result["code"] != 0


@pytest.mark.asyncio
async def test_rebase_self_returns_ok(tmp_path) -> None:
    """Rebase HEAD onto the same branch is a no-op and succeeds."""
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    main = "master" if _branch_exists(cwd, "master") else "main"
    svc = DesktopGitService(timeout_sec=5)
    result = await svc.rebase(session, main)
    assert result["status"] == "ok"


def test_classify_git_result_ok() -> None:
    from desktop.services.desktop_git_service import DesktopGitService
    r = DesktopGitService._classify_git_result(0, "Fast-forward")
    assert r == {"status": "ok", "code": 0, "output": "Fast-forward"}


def test_classify_git_result_conflict() -> None:
    out = "Auto-merging foo.txt\nCONFLICT (content): Merge conflict in foo.txt"
    r = DesktopGitService._classify_git_result(1, out)
    assert r["status"] == "conflict"
    assert r["code"] == 1
    assert "files" in r


def test_classify_git_result_error() -> None:
    r = DesktopGitService._classify_git_result(128, "fatal: not a git repository")
    assert r["status"] == "error"
    assert r["code"] == 128


# ---------------------------------------------------------------------------
# Фича B: GIT_ASKPASS / token injection
# ---------------------------------------------------------------------------

def test_git_env_no_token_has_terminal_prompt_off() -> None:
    svc = DesktopGitService(github_token=None)
    env = svc._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_ASKPASS" not in env


def test_git_env_with_token_sets_askpass() -> None:
    svc = DesktopGitService(github_token="ghp_fake_token")
    env = svc._git_env()
    assert env.get("GIT_ASKPASS_TOKEN") == "ghp_fake_token"
    assert "GIT_ASKPASS" in env


def test_ensure_askpass_creates_executable(tmp_path) -> None:
    import os
    import stat
    svc = DesktopGitService(github_token="ghp_fake_token")
    path = svc._ensure_askpass()
    assert path is not None
    assert os.path.isfile(path)
    mode = os.stat(path).st_mode
    assert mode & stat.S_IXUSR  # owner executable bit set


def test_ensure_askpass_cached(tmp_path) -> None:
    """Calling _ensure_askpass twice returns the same path."""
    svc = DesktopGitService(github_token="ghp_fake_token")
    p1 = svc._ensure_askpass()
    p2 = svc._ensure_askpass()
    assert p1 == p2


@pytest.mark.asyncio
async def test_push_with_token_auto_upstream(tmp_path) -> None:
    """push() on a fresh no-remote repo returns nonzero regardless of token."""
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    svc = DesktopGitService(timeout_sec=5, github_token="ghp_fake")
    code, out = await svc.push(session)
    assert code != 0  # no remote configured


@pytest.mark.asyncio
async def test_pull_ff_strategy_no_upstream_returns_error(tmp_path) -> None:
    cwd = _make_repo(tmp_path / "repo")
    session = SimpleNamespace(workdir=cwd)
    svc = DesktopGitService(timeout_sec=5)
    code, out = await svc.pull(session, strategy="ff")
    assert code != 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _branch_exists(cwd: str, branch: str) -> bool:
    import subprocess
    r = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return r.returncode == 0
