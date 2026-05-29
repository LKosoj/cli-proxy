"""Verification: ssh.env persistence with 0o600 file permissions."""

import stat

from app.services.ssh_config_loader import (
    delete_ssh_secret,
    load_ssh_secrets,
    save_ssh_secret,
)


def test_new_ssh_env_created_with_0600(tmp_path):
    save_ssh_secret(str(tmp_path), "SECRET", "value")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_ssh_env_permissions_after_update(tmp_path):
    save_ssh_secret(str(tmp_path), "A", "1")
    save_ssh_secret(str(tmp_path), "B", "2")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 after update, got {oct(mode)}"


def test_ssh_env_permissions_after_delete(tmp_path):
    save_ssh_secret(str(tmp_path), "A", "1")
    save_ssh_secret(str(tmp_path), "B", "2")
    delete_ssh_secret(str(tmp_path), "A")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 after delete, got {oct(mode)}"


def test_no_group_read(tmp_path):
    save_ssh_secret(str(tmp_path), "K", "V")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode
    assert not (mode & stat.S_IRGRP)
    assert not (mode & stat.S_IWGRP)
    assert not (mode & stat.S_IXGRP)


def test_no_other_read(tmp_path):
    save_ssh_secret(str(tmp_path), "K", "V")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode
    assert not (mode & stat.S_IROTH)
    assert not (mode & stat.S_IWOTH)
    assert not (mode & stat.S_IXOTH)


def test_secrets_roundtrip_preserves_values(tmp_path):
    save_ssh_secret(str(tmp_path), "PASS_A", "secret1")
    save_ssh_secret(str(tmp_path), "PASS_B", "secret2")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets["PASS_A"] == "secret1"
    assert secrets["PASS_B"] == "secret2"


def test_delete_preserves_other_secrets(tmp_path):
    save_ssh_secret(str(tmp_path), "KEEP", "yes")
    save_ssh_secret(str(tmp_path), "DROP", "no")
    delete_ssh_secret(str(tmp_path), "DROP")
    secrets = load_ssh_secrets(str(tmp_path))
    assert secrets == {"KEEP": "yes"}


def test_ssh_env_not_world_readable(tmp_path):
    """Filesystem is protected: no read access for group or other users."""
    save_ssh_secret(str(tmp_path), "TOP_SECRET", "classified")
    path = tmp_path / ".cli-proxy" / "ssh.env"
    mode = path.stat().st_mode & 0o077
    assert mode == 0, f"group/other bits must be zero, got {oct(mode)}"
