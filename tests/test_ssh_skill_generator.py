"""Tests for SSH skill generator."""

from types import SimpleNamespace

from app.services.ssh_skill_generator import generate_ssh_skill_text
from session import ModeState


def _write_ssh_config(tmp_path, yaml_text):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / "ssh.yaml").write_text(yaml_text)


def test_returns_none_when_no_hosts(tmp_path):
    assert generate_ssh_skill_text(str(tmp_path)) is None


def test_returns_none_when_ssh_disabled(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  prod:\n    host: 1.2.3.4\n    user: u\n")
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=False))
    assert generate_ssh_skill_text(str(tmp_path), session=session) is None


def test_returns_markdown_when_enabled(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  prod:\n    host: 1.2.3.4\n    user: deploy\n")
    session = SimpleNamespace(modes=ModeState(ssh_remote_enabled=True))
    text = generate_ssh_skill_text(str(tmp_path), session=session)
    assert text is not None
    assert "# SSH Remote Access" in text
    assert "prod" in text
    assert "deploy@1.2.3.4" in text


def test_returns_markdown_without_session(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  prod:\n    host: 1.2.3.4\n    user: u\n")
    text = generate_ssh_skill_text(str(tmp_path))
    assert text is not None
    assert "prod" in text


def test_key_auth_shows_ssh_i(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    auth: key\n"
        "    key_file: .cli-proxy/ssh_keys/prod.key\n"
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    assert "ssh -i" in text
    assert "prod.key" in text


def test_password_auth_shows_sshpass(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  staging:\n"
        "    host: 10.0.0.2\n"
        "    user: admin\n"
        "    auth: password\n"
        "    password_env: STAG_PASS\n"
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    assert "sshpass" in text
    assert "$STAG_PASS" in text


def test_sudo_instructions_included(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    sudo: true\n"
        "    sudo_password_env: SUDO_PASS\n"
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    assert "sudo -S" in text
    assert "$SUDO_PASS" in text


def test_roles_and_description(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  db:\n"
        "    host: 10.0.0.3\n"
        "    user: dba\n"
        "    roles: [db, master]\n"
        '    description: "PostgreSQL master"\n'
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    assert "db, master" in text
    assert "PostgreSQL master" in text


def test_multiple_hosts(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  prod:\n"
        "    host: 1.1.1.1\n"
        "    user: u1\n"
        "  staging:\n"
        "    host: 2.2.2.2\n"
        "    user: u2\n"
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    assert "prod" in text
    assert "staging" in text
    assert "1.1.1.1" in text
    assert "2.2.2.2" in text


# ---------------------------------------------------------------------------
# Markdown structure validation
# ---------------------------------------------------------------------------

def test_markdown_has_h1_heading(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  x:\n    host: 1.1.1.1\n    user: u\n")
    text = generate_ssh_skill_text(str(tmp_path))
    lines = text.splitlines()
    assert lines[0] == "# SSH Remote Access"


def test_markdown_has_h2_available_servers(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  x:\n    host: 1.1.1.1\n    user: u\n")
    text = generate_ssh_skill_text(str(tmp_path))
    assert "## Available Servers" in text


def test_markdown_has_h3_per_host(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  alpha:\n"
        "    host: 1.1.1.1\n"
        "    user: u\n"
        "  beta:\n"
        "    host: 2.2.2.2\n"
        "    user: v\n"
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    h3_lines = [line for line in text.splitlines() if line.startswith("### ")]
    assert len(h3_lines) == 2
    assert any("alpha" in line for line in h3_lines)
    assert any("beta" in line for line in h3_lines)


def test_markdown_has_code_blocks(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  x:\n    host: 1.1.1.1\n    user: u\n")
    text = generate_ssh_skill_text(str(tmp_path))
    assert "```bash" in text
    assert text.count("```") >= 2  # open + close


def test_markdown_custom_port(tmp_path):
    _write_ssh_config(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.1.1.1\n"
        "    user: u\n"
        "    port: 2222\n"
    ))
    text = generate_ssh_skill_text(str(tmp_path))
    assert "-p 2222" in text


def test_markdown_ends_with_newline(tmp_path):
    _write_ssh_config(tmp_path, "hosts:\n  x:\n    host: 1.1.1.1\n    user: u\n")
    text = generate_ssh_skill_text(str(tmp_path))
    assert text.endswith("\n")
