"""Tests for is_ssh_remote_enabled / set_ssh_remote_enabled accessors."""

from types import SimpleNamespace

from session import ModeState
from sessions.session_state_access import is_ssh_remote_enabled, set_ssh_remote_enabled


# ---------------------------------------------------------------------------
# Real Session (ModeState path)
# ---------------------------------------------------------------------------

def test_get_default_false_on_real_session():
    session = SimpleNamespace(modes=ModeState())
    assert is_ssh_remote_enabled(session) is False


def test_set_true_on_real_session():
    session = SimpleNamespace(modes=ModeState())
    set_ssh_remote_enabled(session, True)
    assert session.modes.ssh_remote_enabled is True
    assert is_ssh_remote_enabled(session) is True


def test_set_false_after_true():
    session = SimpleNamespace(modes=ModeState())
    set_ssh_remote_enabled(session, True)
    set_ssh_remote_enabled(session, False)
    assert is_ssh_remote_enabled(session) is False


# ---------------------------------------------------------------------------
# Fake/SimpleNamespace session (fallback path)
# ---------------------------------------------------------------------------

def test_get_default_false_on_fake_session():
    session = SimpleNamespace()
    assert is_ssh_remote_enabled(session) is False


def test_set_true_on_fake_session():
    session = SimpleNamespace()
    set_ssh_remote_enabled(session, True)
    assert is_ssh_remote_enabled(session) is True


def test_fallback_reads_flat_attribute():
    session = SimpleNamespace(ssh_remote_enabled=True)
    assert is_ssh_remote_enabled(session) is True


# ---------------------------------------------------------------------------
# Custom default
# ---------------------------------------------------------------------------

def test_get_with_custom_default():
    session = SimpleNamespace()
    assert is_ssh_remote_enabled(session, default=True) is True
