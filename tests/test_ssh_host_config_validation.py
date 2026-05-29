"""Verification tests: SSHHostConfig mapping from YAML handles invalid fields correctly."""

from app.services.ssh_config_loader import load_ssh_config


def _write_yaml(tmp_path, text):
    ssh_dir = tmp_path / ".cli-proxy"
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / "ssh.yaml").write_text(text)


def test_missing_host_field_skips_entry(tmp_path):
    _write_yaml(tmp_path, "hosts:\n  bad:\n    user: deploy\n")
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts


def test_missing_user_field_skips_entry(tmp_path):
    _write_yaml(tmp_path, "hosts:\n  bad:\n    host: 1.2.3.4\n")
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts


def test_empty_host_string_skips_entry(tmp_path):
    _write_yaml(tmp_path, 'hosts:\n  bad:\n    host: ""\n    user: u\n')
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts


def test_empty_user_string_skips_entry(tmp_path):
    _write_yaml(tmp_path, 'hosts:\n  bad:\n    host: 1.2.3.4\n    user: ""\n')
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts


def test_invalid_auth_defaults_to_key(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    auth: kerberos\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].auth == "key"


def test_port_zero_defaults_to_22(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    port: 0\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].port == 22


def test_port_negative_defaults_to_22(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    port: -1\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].port == 22


def test_port_above_65535_defaults_to_22(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    port: 99999\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].port == 22


def test_non_dict_host_entry_skipped(tmp_path):
    _write_yaml(tmp_path, "hosts:\n  bad: just_a_string\n")
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts


def test_hosts_not_a_dict_returns_empty(tmp_path):
    _write_yaml(tmp_path, "hosts:\n  - item1\n  - item2\n")
    hosts = load_ssh_config(str(tmp_path))
    assert hosts == {}


def test_root_not_a_dict_returns_empty(tmp_path):
    _write_yaml(tmp_path, "just a string\n")
    hosts = load_ssh_config(str(tmp_path))
    assert hosts == {}


def test_invalid_yaml_returns_empty(tmp_path):
    _write_yaml(tmp_path, "{{{{not yaml")
    hosts = load_ssh_config(str(tmp_path))
    assert hosts == {}


def test_allowed_chat_ids_non_list_ignored(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    allowed_chat_ids: 42\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].allowed_chat_ids is None


def test_roles_non_list_becomes_empty(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  x:\n"
        "    host: 1.2.3.4\n"
        "    user: u\n"
        "    roles: not_a_list\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert hosts["x"].roles == []


def test_valid_entry_alongside_invalid(tmp_path):
    _write_yaml(tmp_path, (
        "hosts:\n"
        "  bad:\n"
        "    port: 22\n"
        "  good:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
    ))
    hosts = load_ssh_config(str(tmp_path))
    assert "bad" not in hosts
    assert "good" in hosts
    assert hosts["good"].host == "10.0.0.1"
