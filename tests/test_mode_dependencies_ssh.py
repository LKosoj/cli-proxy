"""Tests for SSHService field in ModeDependencies."""

from unittest.mock import MagicMock

from app.mode_dependencies import ModeDependencies


def _make_deps(**kwargs):
    return ModeDependencies(
        session_manager=MagicMock(),
        registry=MagicMock(),
        pipeline=MagicMock(),
        **kwargs,
    )


def test_ssh_field_defaults_to_none():
    deps = _make_deps()
    assert deps.ssh is None


def test_ssh_field_accepts_service():
    mock_ssh = MagicMock()
    deps = _make_deps(ssh=mock_ssh)
    assert deps.ssh is mock_ssh


def test_with_overrides_preserves_ssh():
    mock_ssh = MagicMock()
    deps = _make_deps(ssh=mock_ssh)
    overridden = deps.with_overrides(tasks=MagicMock())
    assert overridden.ssh is mock_ssh


def test_with_overrides_replaces_ssh():
    deps = _make_deps()
    mock_ssh = MagicMock()
    overridden = deps.with_overrides(ssh=mock_ssh)
    assert overridden.ssh is mock_ssh
