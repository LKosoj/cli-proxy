from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_ALLOWLIST_PATH = REPO_ROOT / "modes" / "admin" / "allowlist.py"
_SPEC = importlib.util.spec_from_file_location("modes_admin_allowlist_test", _ALLOWLIST_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load admin allowlist module from {_ALLOWLIST_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
is_action_allowlisted = _MODULE.is_action_allowlisted
is_valid_action_id = _MODULE.is_valid_action_id


def test_admin_allowlist_function_is_exported_and_checks_targets() -> None:
    cfg = {
        "admin": {
            "allowlist": {
                "local": ["sync_logs", "rotate_cache"],
                "ssh": ["restart_nginx"],
            }
        }
    }

    assert is_action_allowlisted(cfg, target="local", action_id="sync_logs") is True
    assert is_action_allowlisted(cfg, target="ssh", action_id="restart_nginx") is True
    assert is_action_allowlisted(cfg, target="local", action_id="restart_nginx") is False
    assert is_action_allowlisted(cfg, target="ssh", action_id="sync_logs") is False


def test_admin_allowlist_action_id_validation_blocks_shell_like_tokens() -> None:
    assert is_valid_action_id("safe_action-1") is True
    assert is_valid_action_id("deploy.prod:west") is True
    assert is_valid_action_id("safe_action; rm -rf /") is False
    assert is_valid_action_id("safe_action&&cat /etc/passwd") is False


def test_admin_allowlist_parses_mapping_form_and_blocks_unknown_target() -> None:
    cfg = {
        "admin": {
            "allowlist": {
                "local": {
                    "rotate_logs": {"argv": ["echo", "ok"]},
                },
                "ssh": {
                    "restart_nginx": {
                        "host": "srv.example",
                        "key_path": "/keys/id_rsa",
                        "argv": ["systemctl", "restart", "nginx"],
                    }
                },
            }
        }
    }

    assert is_action_allowlisted(cfg, target="local", action_id="rotate_logs") is True
    assert is_action_allowlisted(cfg, target="ssh", action_id="restart_nginx") is True
    assert is_action_allowlisted(cfg, target="ssh", action_id="rotate_logs") is False
    assert is_action_allowlisted(cfg, target="docker", action_id="restart_nginx") is False


def test_admin_allowlist_returns_false_for_invalid_payload_shapes() -> None:
    assert is_action_allowlisted({}, target="local", action_id="x") is False
    assert is_action_allowlisted({"admin": {"allowlist": []}}, target="local", action_id="x") is False
    assert is_action_allowlisted({"admin": {"allowlist": {"local": "x"}}}, target="local", action_id="x") is False
