"""Verification: ssh.env secrets are injected into CLI subprocess environment.

Reproduces the exact env-construction logic from session.py _run_headless
(lines 666-669) to verify secrets from ssh.env appear in the env dict
that would be passed to asyncio.create_subprocess_exec().

Includes an integration test that actually runs 'env' as a subprocess
and verifies the secrets are visible in its output.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.services.ssh_config_loader import load_ssh_secrets, save_ssh_secret
from app.services.ssh_skill_generator import generate_ssh_skill_text
from session import ModeState


def _setup_project(tmp_path):
    workdir = str(tmp_path)
    ssh_dir = os.path.join(workdir, ".cli-proxy")
    os.makedirs(ssh_dir, exist_ok=True)
    with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
        f.write(
            "hosts:\n"
            "  prod:\n"
            "    host: 10.0.0.1\n"
            "    user: deploy\n"
            "    auth: password\n"
            "    password_env: SSH_PROD_PASS\n"
            "    sudo: true\n"
            "    sudo_password_env: SSH_PROD_SUDO\n"
        )
    save_ssh_secret(workdir, "SSH_PROD_PASS", "my_password_123")
    save_ssh_secret(workdir, "SSH_PROD_SUDO", "sudo_secret_456")
    return workdir


def _simulate_env_construction(workdir, session):
    """Reproduce the exact logic from session.py _run_headless lines 631-669."""
    ssh_skill = generate_ssh_skill_text(workdir, session=session)

    env = os.environ.copy()
    if ssh_skill:
        secrets = load_ssh_secrets(workdir)
        env.update(secrets)
    return env, ssh_skill


def test_secrets_injected_when_ssh_enabled(tmp_path):
    workdir = _setup_project(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    env, ssh_skill = _simulate_env_construction(workdir, session)

    assert ssh_skill is not None
    assert env.get("SSH_PROD_PASS") == "my_password_123"
    assert env.get("SSH_PROD_SUDO") == "sudo_secret_456"


def test_secrets_not_injected_when_ssh_disabled(tmp_path):
    workdir = _setup_project(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))

    env, ssh_skill = _simulate_env_construction(workdir, session)

    assert ssh_skill is None
    assert "SSH_PROD_PASS" not in env
    assert "SSH_PROD_SUDO" not in env


def test_secrets_not_injected_when_no_hosts(tmp_path):
    workdir = str(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    env, ssh_skill = _simulate_env_construction(workdir, session)

    assert ssh_skill is None
    assert "SSH_PROD_PASS" not in env


def test_multiple_secrets_all_injected(tmp_path):
    workdir = _setup_project(tmp_path)
    save_ssh_secret(workdir, "EXTRA_KEY", "extra_value")
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    env, _ = _simulate_env_construction(workdir, session)

    assert env["SSH_PROD_PASS"] == "my_password_123"
    assert env["SSH_PROD_SUDO"] == "sudo_secret_456"
    assert env["EXTRA_KEY"] == "extra_value"


def test_secrets_dont_leak_to_os_environ(tmp_path):
    workdir = _setup_project(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    _simulate_env_construction(workdir, session)

    assert "SSH_PROD_PASS" not in os.environ
    assert "SSH_PROD_SUDO" not in os.environ


# ---------------------------------------------------------------------------
# Integration: real subprocess sees the secrets via 'env' command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subprocess_env_command_sees_secrets(tmp_path):
    """Run actual 'env' subprocess with constructed env and verify secrets."""
    workdir = _setup_project(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))
    env, ssh_skill = _simulate_env_construction(workdir, session)

    assert ssh_skill is not None

    proc = await asyncio.create_subprocess_exec(
        "env",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode()

    assert "SSH_PROD_PASS=my_password_123" in output
    assert "SSH_PROD_SUDO=sudo_secret_456" in output


@pytest.mark.asyncio
async def test_subprocess_printenv_sees_specific_secret(tmp_path):
    """Run 'printenv SSH_PROD_PASS' and verify exact value."""
    workdir = _setup_project(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))
    env, _ = _simulate_env_construction(workdir, session)

    proc = await asyncio.create_subprocess_exec(
        "printenv", "SSH_PROD_PASS",
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    assert stdout.decode().strip() == "my_password_123"
    assert proc.returncode == 0
