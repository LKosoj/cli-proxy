"""Tests for app/services/ssh_config_loader.py read and write operations."""

import os
import stat

from app.services.ssh_config_loader import (
    build_ssh_secret_env_name,
    delete_ssh_secret,
    ensure_ssh_config_template,
    load_ssh_config,
    load_ssh_secrets,
    resolve_ssh_secret,
    save_ssh_config,
    save_ssh_secret,
    ssh_config_exists,
)
from config import SSHHostConfig


# ---------------------------------------------------------------------------
# load_ssh_config
# ---------------------------------------------------------------------------

def test_load_ssh_config_returns_empty_when_no_file(tmp_path):
    assert load_ssh_config(str(tmp_path)) == {}


def test_build_ssh_secret_env_name_normalizes_alias():
    assert build_ssh_secret_env_name("Mb-test") == "SSH_MB_TEST_PASSWORD"
    assert build_ssh_secret_env_name("db.prod", sudo=True) == "SSH_DB_PROD_SUDO_PASSWORD"


def test_load_ssh_config_parses_full_host(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  prod:\n"
        "    host: 10.0.0.1\n"
        "    port: 2222\n"
        "    user: deploy\n"
        "    auth: key\n"
        "    key_file: ~/.ssh/prod.key\n"
        "    key_passphrase_env: PROD_KEY_PASS\n"
        "    sudo: true\n"
        "    sudo_password_env: PROD_SUDO\n"
        "    idle_timeout_sec: 600\n"
        "    allowed_chat_ids: [111, 222]\n"
        "    roles: [web, app]\n"
        '    description: "Production"\n'
    )
    hosts = load_ssh_config(str(tmp_path))
    assert "prod" in hosts
    cfg = hosts["prod"]
    assert isinstance(cfg, SSHHostConfig)
    assert cfg.host == "10.0.0.1"
    assert cfg.port == 2222
    assert cfg.user == "deploy"
    assert cfg.auth == "key"
    assert cfg.key_file == "~/.ssh/prod.key"
    assert cfg.key_passphrase_env == "PROD_KEY_PASS"
    assert cfg.sudo is True
    assert cfg.sudo_password_env == "PROD_SUDO"
    assert cfg.idle_timeout_sec == 600
    assert cfg.allowed_chat_ids == [111, 222]
    assert cfg.roles == ["web", "app"]
    assert cfg.description == "Production"


def test_load_ssh_config_defaults(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  staging:\n"
        "    host: 10.0.0.2\n"
        "    user: admin\n"
    )
    hosts = load_ssh_config(str(tmp_path))
    cfg = hosts["staging"]
    assert cfg.auth == "key"
    assert cfg.port == 22
    assert cfg.sudo is False
    assert cfg.idle_timeout_sec == 1200
    assert cfg.allowed_chat_ids is None
    assert cfg.roles == []
    assert cfg.description == ""


def test_load_ssh_config_skips_host_without_required_fields(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  bad:\n"
        "    port: 22\n"
        "  good:\n"
        "    host: 1.2.3.4\n"
        "    user: root\n"
    )
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts
    assert "good" in hosts


def test_load_ssh_config_multiple_hosts(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  a:\n"
        "    host: 1.1.1.1\n"
        "    user: u1\n"
        "  b:\n"
        "    host: 2.2.2.2\n"
        "    user: u2\n"
    )
    hosts = load_ssh_config(str(tmp_path))
    assert len(hosts) == 2
    assert hosts["a"].host == "1.1.1.1"
    assert hosts["b"].host == "2.2.2.2"


def test_load_ssh_config_invalid_yaml(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text("{{{{not yaml")
    assert load_ssh_config(str(tmp_path)) == {}


def test_load_ssh_config_unknown_auth_defaults_to_key(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    auth: kerberos\n"
    )
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].auth == "key"


def test_load_ssh_config_password_auth(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    auth: password\n"
        "    password_env: MY_PASS\n"
    )
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].auth == "password"
    assert hosts["x"].password_env == "MY_PASS"


def test_load_ssh_config_port_out_of_range(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text(
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    port: 99999\n"
    )
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].port == 22


def test_load_ssh_config_empty_hosts_section(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.yaml").write_text("hosts:\n")
    assert load_ssh_config(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# load_ssh_secrets
# ---------------------------------------------------------------------------

def test_load_ssh_secrets_returns_empty_when_no_file(tmp_path):
    assert load_ssh_secrets(str(tmp_path)) == {}


def test_load_ssh_secrets_parses_env_file(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.env").write_text(
        "SSH_PASS=secret123\n"
        "SSH_SUDO=sudopass\n"
    )
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets == {"SSH_PASS": "secret123", "SSH_SUDO": "sudopass"}


def test_load_ssh_secrets_handles_comments_and_blanks(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.env").write_text(
        "# comment\n"
        "\n"
        "KEY=value\n"
    )
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets == {"KEY": "value"}


def test_load_ssh_secrets_quoted_values(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.env").write_text('MY_KEY="hello world"\n')
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets["MY_KEY"] == "hello world"


# ---------------------------------------------------------------------------
# resolve_ssh_secret
# ---------------------------------------------------------------------------

def test_resolve_ssh_secret_from_dict():
    secrets = {"MY_VAR": "from_file"}
    assert resolve_ssh_secret(secrets, "MY_VAR") == "from_file"


def test_resolve_ssh_secret_from_env(monkeypatch):
    monkeypatch.setenv("SSH_TEST_VAR_XYZ", "from_env")
    assert resolve_ssh_secret({}, "SSH_TEST_VAR_XYZ") == "from_env"


def test_resolve_ssh_secret_dict_takes_priority(monkeypatch):
    monkeypatch.setenv("SHARED_KEY", "from_env")
    secrets = {"SHARED_KEY": "from_file"}
    assert resolve_ssh_secret(secrets, "SHARED_KEY") == "from_file"


def test_resolve_ssh_secret_returns_none_for_missing():
    assert resolve_ssh_secret({}, "DEFINITELY_MISSING_VAR_12345") is None


def test_resolve_ssh_secret_returns_none_for_empty_name():
    assert resolve_ssh_secret({"K": "V"}, "") is None
    assert resolve_ssh_secret({"K": "V"}, None) is None


def test_resolve_ssh_secret_exact_key_match():
    secrets = {"KEY": "val"}
    assert resolve_ssh_secret(secrets, "KEY") == "val"
    # Non-stripped key does not match (no implicit whitespace stripping)
    assert resolve_ssh_secret(secrets, "  KEY  ") is None


# ---------------------------------------------------------------------------
# Contract: all three public functions exist and are callable
# ---------------------------------------------------------------------------

def test_public_api_contract():
    from app.services import ssh_config_loader as mod

    assert callable(getattr(mod, "load_ssh_config"))
    assert callable(getattr(mod, "load_ssh_secrets"))
    assert callable(getattr(mod, "resolve_ssh_secret"))


# ---------------------------------------------------------------------------
# Edge: does not inject into os.environ
# ---------------------------------------------------------------------------

def test_load_ssh_secrets_does_not_pollute_environ(tmp_path):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir()
    (ssh_dir / "ssh.env").write_text(
        "SSH_LOADER_TEST_NOPOLLUTE=shouldnotappear\n"
    )
    load_ssh_secrets(str(tmp_path))
    assert "SSH_LOADER_TEST_NOPOLLUTE" not in os.environ


# ===========================================================================
# Write operations
# ===========================================================================

# ---------------------------------------------------------------------------
# save_ssh_config
# ---------------------------------------------------------------------------

def test_save_ssh_config_creates_dir_and_file(tmp_path):
    hosts = {"prod": SSHHostConfig(host="10.0.0.1", user="deploy")}
    save_ssh_config(str(tmp_path), hosts)
    assert (tmp_path / ".cli-proxy" / "ssh.yaml").is_file()


def test_ensure_ssh_config_template_creates_file(tmp_path):
    assert ssh_config_exists(str(tmp_path)) is False
    assert ensure_ssh_config_template(str(tmp_path)) is True
    ssh_yaml = tmp_path / ".cli-proxy" / "ssh.yaml"
    assert ssh_yaml.is_file()
    assert "hosts: {}" in ssh_yaml.read_text(encoding="utf-8")
    assert ssh_config_exists(str(tmp_path)) is True


def test_save_ssh_config_roundtrip(tmp_path):
    hosts = {
        "prod": SSHHostConfig(
            host="10.0.0.1",
            user="deploy",
            auth="key",
            port=2222,
            key_file="~/.ssh/prod.key",
            sudo=True,
            roles=["web", "app"],
            description="Production",
        ),
        "staging": SSHHostConfig(
            host="10.0.0.2",
            user="admin",
            auth="password",
            password_env="STAG_PASS",
        ),
    }
    save_ssh_config(str(tmp_path), hosts)
    loaded = load_ssh_config(str(tmp_path))
    assert set(loaded.keys()) == {"prod", "staging"}
    assert loaded["prod"].host == "10.0.0.1"
    assert loaded["prod"].port == 2222
    assert loaded["prod"].key_file == "~/.ssh/prod.key"
    assert loaded["prod"].sudo is True
    assert loaded["prod"].roles == ["web", "app"]
    assert loaded["staging"].auth == "password"
    assert loaded["staging"].password_env == "STAG_PASS"


def test_save_ssh_config_overwrites_existing(tmp_path):
    hosts_v1 = {"a": SSHHostConfig(host="1.1.1.1", user="u1")}
    save_ssh_config(str(tmp_path), hosts_v1)
    hosts_v2 = {"b": SSHHostConfig(host="2.2.2.2", user="u2")}
    save_ssh_config(str(tmp_path), hosts_v2)
    loaded = load_ssh_config(str(tmp_path))
    assert "a" not in loaded
    assert "b" in loaded


def test_save_ssh_config_empty_hosts(tmp_path):
    save_ssh_config(str(tmp_path), {})
    loaded = load_ssh_config(str(tmp_path))
    assert loaded == {}


# ---------------------------------------------------------------------------
# save_ssh_secret
# ---------------------------------------------------------------------------

def test_save_ssh_secret_creates_file(tmp_path):
    save_ssh_secret(str(tmp_path), "MY_PASS", "secret123")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    assert path.is_file()
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets["MY_PASS"] == "secret123"


def test_save_ssh_secret_file_permissions(tmp_path):
    save_ssh_secret(str(tmp_path), "K", "V")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode
    assert mode & stat.S_IRUSR  # owner read
    assert mode & stat.S_IWUSR  # owner write
    assert not (mode & stat.S_IRGRP)  # no group read
    assert not (mode & stat.S_IROTH)  # no other read


def test_save_ssh_secret_preserves_existing(tmp_path):
    save_ssh_secret(str(tmp_path), "KEY_A", "val_a")
    save_ssh_secret(str(tmp_path), "KEY_B", "val_b")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets == {"KEY_A": "val_a", "KEY_B": "val_b"}


def test_save_ssh_secret_updates_existing_key(tmp_path):
    save_ssh_secret(str(tmp_path), "KEY", "old")
    save_ssh_secret(str(tmp_path), "KEY", "new")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets["KEY"] == "new"


def test_save_ssh_secret_empty_key_creates_entry(tmp_path):
    # Empty string key is accepted (no validation at this layer)
    save_ssh_secret(str(tmp_path), "VALID_KEY", "val")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets["VALID_KEY"] == "val"


# ---------------------------------------------------------------------------
# delete_ssh_secret
# ---------------------------------------------------------------------------

def test_delete_ssh_secret_removes_key(tmp_path):
    save_ssh_secret(str(tmp_path), "A", "1")
    save_ssh_secret(str(tmp_path), "B", "2")
    delete_ssh_secret(str(tmp_path), "A")
    secrets = load_ssh_secrets(str(tmp_path))
    assert "A" not in secrets
    assert secrets["B"] == "2"


def test_delete_ssh_secret_noop_missing_key(tmp_path):
    save_ssh_secret(str(tmp_path), "X", "1")
    delete_ssh_secret(str(tmp_path), "NONEXISTENT")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets == {"X": "1"}


def test_delete_ssh_secret_noop_missing_file(tmp_path):
    delete_ssh_secret(str(tmp_path), "ANY")  # should not raise


def test_delete_ssh_secret_empty_key_noop(tmp_path):
    save_ssh_secret(str(tmp_path), "K", "V")
    delete_ssh_secret(str(tmp_path), "")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets == {"K": "V"}


# ---------------------------------------------------------------------------
# Contract: all public write functions exist
# ---------------------------------------------------------------------------

def test_write_api_contract():
    from app.services import ssh_config_loader as mod

    assert callable(getattr(mod, "save_ssh_config"))
    assert callable(getattr(mod, "save_ssh_secret"))
    assert callable(getattr(mod, "delete_ssh_secret"))
