"""Tests for SSH skill text being prepended to CLI prompt."""

import os
from types import SimpleNamespace

from app.services.ssh_skill_generator import generate_ssh_skill_text
from session import ModeState
from config import AppConfig, DefaultsConfig, MCPConfig, TelegramConfig, ToolConfig


def _build_config(tmp_path):
    workdir = str(tmp_path / "project")
    os.makedirs(workdir, exist_ok=True)
    return AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1]),
        tools={"qwen": ToolConfig(name="qwen", mode="headless", cmd=["echo", "{prompt}"])},
        defaults=DefaultsConfig(
            workdir=workdir,
            state_path=str(tmp_path / "state.db"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
    ), workdir


def _write_ssh_config(workdir, yaml_text):
    ssh_dir = os.path.join(workdir, ".cli-proxy")
    os.makedirs(ssh_dir, exist_ok=True)
    with open(os.path.join(ssh_dir, "ssh.yaml"), "w") as f:
        f.write(yaml_text)


def test_generate_ssh_skill_prepends_to_prompt(tmp_path):
    """Verify the skill text is generated when SSH is enabled with hosts."""
    cfg, workdir = _build_config(tmp_path)
    _write_ssh_config(workdir, "hosts:\n  prod:\n    host: 1.2.3.4\n    user: deploy\n")

    session = SimpleNamespace(
        modes=ModeState(ssh_remote_enabled=True),
        workdir=workdir,
    )
    skill = generate_ssh_skill_text(workdir, session=session)
    assert skill is not None
    assert "# SSH Remote Access" in skill
    assert "prod" in skill

    # Simulate prompt prepending (same logic as session.py _run_headless)
    prompt = "Hello"
    prompt_with_skill = f"{skill}\n\n---\n\n{prompt}"
    assert prompt_with_skill.startswith("# SSH Remote Access")
    assert prompt_with_skill.endswith("Hello")
    assert "\n\n---\n\n" in prompt_with_skill


def test_no_skill_when_ssh_disabled(tmp_path):
    cfg, workdir = _build_config(tmp_path)
    _write_ssh_config(workdir, "hosts:\n  prod:\n    host: 1.2.3.4\n    user: u\n")

    session = SimpleNamespace(
        modes=ModeState(ssh_remote_enabled=False),
        workdir=workdir,
    )
    skill = generate_ssh_skill_text(workdir, session=session)
    assert skill is None


def test_no_skill_when_no_hosts(tmp_path):
    cfg, workdir = _build_config(tmp_path)
    session = SimpleNamespace(
        modes=ModeState(ssh_remote_enabled=True),
        workdir=workdir,
    )
    skill = generate_ssh_skill_text(workdir, session=session)
    assert skill is None


def test_ssh_env_loaded_when_skill_present(tmp_path):
    """Verify ssh.env secrets would be available for CLI process."""
    cfg, workdir = _build_config(tmp_path)
    _write_ssh_config(workdir, (
        "hosts:\n"
        "  prod:\n"
        "    host: 1.2.3.4\n"
        "    user: deploy\n"
        "    auth: password\n"
        "    password_env: MY_SSH_PASS\n"
    ))
    ssh_dir = os.path.join(workdir, ".cli-proxy")
    with open(os.path.join(ssh_dir, "ssh.env"), "w") as f:
        f.write("MY_SSH_PASS=secret123\n")

    from app.services.ssh_config_loader import load_ssh_secrets
    secrets = load_ssh_secrets(workdir)
    assert secrets["MY_SSH_PASS"] == "secret123"

    # Simulate env augmentation (same logic as session.py _run_headless)
    env = os.environ.copy()
    env.update(secrets)
    assert env["MY_SSH_PASS"] == "secret123"
