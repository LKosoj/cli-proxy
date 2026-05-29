"""Integration tests for RemoteShellService with effective remote target selection."""

import asyncio
from types import SimpleNamespace

from app.services.remote_control_service import ExecutionTarget, RemoteControlService
from app.services.remote_shell_service import RemoteShellService
from app.services.ssh_config_loader import load_ssh_config, save_ssh_config
from config import SSHHostConfig
from session import ModeState


class _RecordingSSHService:
    def __init__(self, *, is_git: bool = True) -> None:
        self._is_git = bool(is_git)
        self.calls: list[tuple[str, str, str, int]] = []

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((str(workdir), str(host_alias), str(command), int(timeout_sec)))

        if "git rev-parse --is-inside-work-tree" in command:
            if self._is_git:
                return SimpleNamespace(stdout="true\n", stderr="", exit_code=0)
            return SimpleNamespace(stdout="", stderr="", exit_code=128)
        if "git status --porcelain" in command:
            return SimpleNamespace(stdout=" M src/app.py\n", stderr="", exit_code=0)
        if "git diff" in command:
            return SimpleNamespace(
                stdout="diff --git a/src/app.py b/src/app.py\n",
                stderr="",
                exit_code=0,
            )
        if "git branch -a" in command:
            return SimpleNamespace(
                stdout="* main\n  develop\n  remotes/origin/main\n",
                stderr="",
                exit_code=0,
            )
        if "git log" in command:
            return SimpleNamespace(
                stdout=(
                    "abc123|Alice|2025-01-01T00:00:00+00:00|Initial commit\n"
                    "def456|Bob|2025-01-02T00:00:00+00:00|Add search\n"
                ),
                stderr="",
                exit_code=0,
            )

        return SimpleNamespace(stdout="executed remotely\n", stderr="", exit_code=0)


def _make_session(workdir, host_alias: str):
    modes = ModeState(
        ssh_remote_enabled=True,
        remote_control_enabled=True,
        remote_control_host_alias=host_alias,
    )
    return SimpleNamespace(id="s1", workdir=str(workdir), modes=modes, busy=False)


def test_remote_shell_execute_command_uses_effective_target_from_ssh_config(tmp_path) -> None:
    workdir = tmp_path / "remote-shell"
    workdir.mkdir()
    save_ssh_config(str(workdir), {
        "prod": SSHHostConfig(
            host="1.1.1.1",
            user="deploy",
            remote_project_root="/srv/app",
        ),
    })

    rc_service = RemoteControlService()
    session = _make_session(workdir, "prod")
    effective = rc_service.compute_effective_state(session, load_ssh_config(str(workdir)))
    assert effective.execution_target == ExecutionTarget.REMOTE
    assert effective.host_alias == "prod"
    assert effective.remote_project_root == "/srv/app"

    ssh = _RecordingSSHService()
    shell = RemoteShellService(ssh)
    result = asyncio.run(
        shell.execute_command(
            str(workdir),
            effective.host_alias or "",
            "pwd",
            cwd=effective.remote_project_root,
        )
    )

    assert result.execution_target == "remote"
    assert result.stdout == "executed remotely\n"
    assert ssh.calls == [
        (str(workdir), "prod", "cd '/srv/app' && pwd", 30),
    ]


def test_remote_shell_sequential_sessions_keep_independent_targets(tmp_path) -> None:
    workdir = tmp_path / "remote-shell-sequential"
    workdir.mkdir()
    save_ssh_config(str(workdir), {
        "prod": SSHHostConfig(
            host="1.1.1.1",
            user="deploy",
            remote_project_root="/srv/prod",
        ),
        "staging": SSHHostConfig(
            host="2.2.2.2",
            user="deploy",
            remote_project_root="/srv/staging",
        ),
    })

    rc_service = RemoteControlService()
    ssh = _RecordingSSHService()
    shell = RemoteShellService(ssh)

    prod_effective = rc_service.compute_effective_state(
        _make_session(workdir, "prod"),
        load_ssh_config(str(workdir)),
    )
    staging_effective = rc_service.compute_effective_state(
        _make_session(workdir, "staging"),
        load_ssh_config(str(workdir)),
    )

    asyncio.run(
        shell.execute_command(
            str(workdir),
            prod_effective.host_alias or "",
            "whoami",
            cwd=prod_effective.remote_project_root,
        )
    )
    asyncio.run(
        shell.execute_command(
            str(workdir),
            staging_effective.host_alias or "",
            "whoami",
            cwd=staging_effective.remote_project_root,
        )
    )

    assert [call[1] for call in ssh.calls] == ["prod", "staging"]
    assert [call[2] for call in ssh.calls] == [
        "cd '/srv/prod' && whoami",
        "cd '/srv/staging' && whoami",
    ]


def test_remote_shell_execute_command_works_for_non_git_target(tmp_path) -> None:
    workdir = tmp_path / "remote-shell-non-git"
    workdir.mkdir()
    save_ssh_config(str(workdir), {
        "plain": SSHHostConfig(
            host="1.1.1.1",
            user="deploy",
            remote_project_root="/srv/plain",
        ),
    })

    rc_service = RemoteControlService()
    session = _make_session(workdir, "plain")
    effective = rc_service.compute_effective_state(session, load_ssh_config(str(workdir)))
    ssh = _RecordingSSHService(is_git=False)
    shell = RemoteShellService(ssh)

    result = asyncio.run(
        shell.execute_command(
            str(workdir),
            effective.host_alias or "",
            "pwd",
            cwd=effective.remote_project_root,
        )
    )

    assert result.execution_target == "remote"
    assert result.stdout == "executed remotely\n"
    assert ssh.calls == [
        (str(workdir), "plain", "cd '/srv/plain' && pwd", 30),
    ]
    assert all("git rev-parse" not in call[2] for call in ssh.calls)


def test_remote_shell_git_commands_use_effective_remote_target(tmp_path) -> None:
    workdir = tmp_path / "remote-git"
    workdir.mkdir()
    save_ssh_config(str(workdir), {
        "prod": SSHHostConfig(
            host="1.1.1.1",
            user="deploy",
            remote_project_root="/srv/app",
        ),
    })

    rc_service = RemoteControlService()
    session = _make_session(workdir, "prod")
    effective = rc_service.compute_effective_state(session, load_ssh_config(str(workdir)))
    ssh = _RecordingSSHService()
    shell = RemoteShellService(ssh)

    status = asyncio.run(
        shell.git_status(
            str(workdir),
            effective.host_alias or "",
            effective.remote_project_root or "",
        )
    )
    diff = asyncio.run(
        shell.git_diff(
            str(workdir),
            effective.host_alias or "",
            effective.remote_project_root or "",
        )
    )
    branch = asyncio.run(
        shell.git_branch(
            str(workdir),
            effective.host_alias or "",
            effective.remote_project_root or "",
        )
    )
    log = asyncio.run(
        shell.git_log(
            str(workdir),
            effective.host_alias or "",
            effective.remote_project_root or "",
            max_count=2,
        )
    )

    assert status.git_available is True
    assert status.entries[0].path == "src/app.py"
    assert diff.git_available is True
    assert "diff --git" in diff.output
    assert branch.git_available is True
    assert branch.entries[0]["name"] == "main"
    assert log.git_available is True
    assert log.entries[0]["hash"] == "abc123"
    assert log.entries[1]["author"] == "Bob"

    assert all(call[1] == "prod" for call in ssh.calls)
    commands = [call[2] for call in ssh.calls]
    assert any("git status --porcelain" in command for command in commands)
    assert any("git diff" in command for command in commands)
    assert any("git branch -a" in command for command in commands)
    assert any("git log --format='%H|%an|%aI|%s' -n 2" in command for command in commands)


def test_remote_shell_git_status_reports_unavailable_for_non_git_target(tmp_path) -> None:
    workdir = tmp_path / "remote-git-non-git"
    workdir.mkdir()
    save_ssh_config(str(workdir), {
        "plain": SSHHostConfig(
            host="1.1.1.1",
            user="deploy",
            remote_project_root="/srv/plain",
        ),
    })

    rc_service = RemoteControlService()
    session = _make_session(workdir, "plain")
    effective = rc_service.compute_effective_state(session, load_ssh_config(str(workdir)))
    ssh = _RecordingSSHService(is_git=False)
    shell = RemoteShellService(ssh)

    status = asyncio.run(
        shell.git_status(
            str(workdir),
            effective.host_alias or "",
            effective.remote_project_root or "",
        )
    )

    assert status.git_available is False
    assert status.error == "not a git repository"
    assert ssh.calls == [
        (
            str(workdir),
            "plain",
            "cd '/srv/plain' && git rev-parse --is-inside-work-tree 2>/dev/null",
            10,
        ),
    ]
