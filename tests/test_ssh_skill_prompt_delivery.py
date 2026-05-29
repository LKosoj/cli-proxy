"""Verification: SSH skill Markdown text is delivered in the CLI prompt.

Tests the prompt construction logic that prepends SSH skill text
before passing to build_command() in _run_headless().
"""

import os
from types import SimpleNamespace

from app.services.ssh_skill_generator import generate_ssh_skill_text
from session import ModeState


def _write_ssh_config(workdir):
    ssh_dir = os.path.join(workdir, ".cli-proxy")
    os.makedirs(ssh_dir, exist_ok=True)
    with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
        f.write(
            "hosts:\n"
            "  prod:\n"
            "    host: 10.0.0.1\n"
            "    user: deploy\n"
            "    auth: key\n"
            "    key_file: ~/.ssh/prod.key\n"
            "    roles: [web, app]\n"
            '    description: "Production server"\n'
            "  staging:\n"
            "    host: 10.0.0.2\n"
            "    user: admin\n"
            "    auth: password\n"
            "    password_env: STAG_PASS\n"
            "    sudo: true\n"
            "    sudo_password_env: STAG_SUDO\n"
        )


def _simulate_prompt_construction(workdir, session, user_prompt):
    """Replicate the exact logic from session.py _run_headless lines 631-633."""
    ssh_skill = generate_ssh_skill_text(workdir, session=session)
    if ssh_skill:
        return f"{ssh_skill}\n\n---\n\n{user_prompt}"
    return user_prompt


def test_prompt_contains_ssh_header_when_enabled(tmp_path):
    workdir = str(tmp_path)
    _write_ssh_config(workdir)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    prompt = _simulate_prompt_construction(workdir, session, "deploy the app")

    assert prompt.startswith("# SSH Remote Access")
    assert "\n\n---\n\n" in prompt
    assert prompt.endswith("deploy the app")


def test_prompt_contains_host_descriptions(tmp_path):
    workdir = str(tmp_path)
    _write_ssh_config(workdir)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    prompt = _simulate_prompt_construction(workdir, session, "check logs")

    assert "prod" in prompt
    assert "staging" in prompt
    assert "deploy@10.0.0.1" in prompt
    assert "admin@10.0.0.2" in prompt
    assert "Production server" in prompt


def test_prompt_contains_ssh_commands(tmp_path):
    workdir = str(tmp_path)
    _write_ssh_config(workdir)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    prompt = _simulate_prompt_construction(workdir, session, "restart")

    assert "ssh -i" in prompt
    assert "sshpass" in prompt
    assert "$STAG_PASS" in prompt


def test_prompt_contains_sudo_instructions(tmp_path):
    workdir = str(tmp_path)
    _write_ssh_config(workdir)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    prompt = _simulate_prompt_construction(workdir, session, "restart")

    assert "sudo -S" in prompt
    assert "$STAG_SUDO" in prompt


def test_prompt_unchanged_when_ssh_disabled(tmp_path):
    workdir = str(tmp_path)
    _write_ssh_config(workdir)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))

    prompt = _simulate_prompt_construction(workdir, session, "hello")

    assert prompt == "hello"
    assert "SSH" not in prompt


def test_prompt_unchanged_when_no_hosts(tmp_path):
    workdir = str(tmp_path)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    prompt = _simulate_prompt_construction(workdir, session, "hello")

    assert prompt == "hello"


def test_prompt_markdown_structure(tmp_path):
    workdir = str(tmp_path)
    _write_ssh_config(workdir)
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))

    prompt = _simulate_prompt_construction(workdir, session, "task")

    lines = prompt.split("\n\n---\n\n")[0].splitlines()
    assert lines[0] == "# SSH Remote Access"
    assert any(line.startswith("## ") for line in lines)
    assert any(line.startswith("### ") for line in lines)
    assert "```bash" in prompt
