"""Tests for SSHHostConfig dataclass in config.py."""

import dataclasses

from config import SSHHostConfig


def test_ssh_host_config_required_fields():
    cfg = SSHHostConfig(host="10.0.0.1", user="deploy")
    assert cfg.host == "10.0.0.1"
    assert cfg.user == "deploy"


def test_ssh_host_config_defaults():
    cfg = SSHHostConfig(host="10.0.0.1", user="deploy")
    assert cfg.auth == "key"
    assert cfg.port == 22
    assert cfg.key_file is None
    assert cfg.key_passphrase_env is None
    assert cfg.password_env is None
    assert cfg.sudo is False
    assert cfg.sudo_password_env is None
    assert cfg.idle_timeout_sec == 1200
    assert cfg.allowed_chat_ids is None
    assert cfg.roles == []
    assert cfg.description == ""


def test_ssh_host_config_all_fields():
    cfg = SSHHostConfig(
        host="10.0.0.2",
        user="admin",
        auth="password",
        port=2222,
        key_file="/home/bot/.ssh/id_ed25519",
        key_passphrase_env="PROD_KEY_PASS",
        password_env="SSH_STAGING_PASS",
        sudo=True,
        sudo_password_env="SSH_STAGING_SUDO",
        idle_timeout_sec=600,
        allowed_chat_ids=[111, 222],
        roles=["web", "app", "db"],
        description="Staging server",
    )
    assert cfg.host == "10.0.0.2"
    assert cfg.user == "admin"
    assert cfg.auth == "password"
    assert cfg.port == 2222
    assert cfg.key_file == "/home/bot/.ssh/id_ed25519"
    assert cfg.key_passphrase_env == "PROD_KEY_PASS"
    assert cfg.password_env == "SSH_STAGING_PASS"
    assert cfg.sudo is True
    assert cfg.sudo_password_env == "SSH_STAGING_SUDO"
    assert cfg.idle_timeout_sec == 600
    assert cfg.allowed_chat_ids == [111, 222]
    assert cfg.roles == ["web", "app", "db"]
    assert cfg.description == "Staging server"


def test_ssh_host_config_is_dataclass():
    assert dataclasses.is_dataclass(SSHHostConfig)


def test_ssh_host_config_roles_isolation():
    cfg1 = SSHHostConfig(host="a", user="u")
    cfg2 = SSHHostConfig(host="b", user="u")
    cfg1.roles.append("web")
    assert cfg2.roles == [], "default_factory must create independent lists"
