"""Tests for RemoteControlService, dataclasses and ExecutionTarget enum."""

import asyncio
import dataclasses
import time
from types import SimpleNamespace

from app.services.remote_control_service import (
    EffectiveState,
    ExecutionTarget,
    PREFLIGHT_CACHE_TTL_SEC,
    PreflightResult,
    RemoteControlService,
    TransitionRequest,
    ValidationResult,
    build_remote_control_audit_extra,
    build_remote_file_audit_extra,
)
from config import SSHHostConfig
from session import ModeState


# ---------------------------------------------------------------------------
# ExecutionTarget enum
# ---------------------------------------------------------------------------


def test_execution_target_values():
    assert ExecutionTarget.LOCAL.value == "local"
    assert ExecutionTarget.REMOTE.value == "remote"


def test_execution_target_members():
    assert set(ExecutionTarget.__members__) == {"LOCAL", "REMOTE"}


def test_execution_target_roundtrip_by_value():
    assert ExecutionTarget("local") is ExecutionTarget.LOCAL
    assert ExecutionTarget("remote") is ExecutionTarget.REMOTE


# ---------------------------------------------------------------------------
# EffectiveState dataclass
# ---------------------------------------------------------------------------


def test_effective_state_fields():
    fields = {f.name for f in dataclasses.fields(EffectiveState)}
    assert fields == {"execution_target", "host_alias", "remote_project_root", "git_available"}


def test_effective_state_defaults():
    state = EffectiveState(execution_target=ExecutionTarget.LOCAL)
    assert state.host_alias is None
    assert state.remote_project_root is None
    assert state.git_available is True


def test_effective_state_all_fields():
    state = EffectiveState(
        execution_target=ExecutionTarget.REMOTE,
        host_alias="prod",
        remote_project_root="/srv/app",
        git_available=False,
    )
    assert state.execution_target == ExecutionTarget.REMOTE
    assert state.host_alias == "prod"
    assert state.remote_project_root == "/srv/app"
    assert state.git_available is False


# ---------------------------------------------------------------------------
# PreflightResult dataclass
# ---------------------------------------------------------------------------


def test_preflight_result_fields():
    fields = {f.name for f in dataclasses.fields(PreflightResult)}
    assert fields == {"ok", "host_alias", "remote_project_root", "checked_at", "error"}


def test_preflight_result_defaults():
    r = PreflightResult(ok=True)
    assert r.host_alias is None
    assert r.remote_project_root is None
    assert r.checked_at is None
    assert r.error is None


def test_preflight_result_full():
    ts = time.time()
    r = PreflightResult(
        ok=False,
        host_alias="staging",
        remote_project_root="/opt/proj",
        checked_at=ts,
        error="connection refused",
    )
    assert r.ok is False
    assert r.host_alias == "staging"
    assert r.checked_at == ts
    assert r.error == "connection refused"


# ---------------------------------------------------------------------------
# TransitionRequest dataclass
# ---------------------------------------------------------------------------


def test_transition_request_fields():
    fields = {f.name for f in dataclasses.fields(TransitionRequest)}
    assert fields == {"enable", "host_alias"}


def test_transition_request_defaults():
    tr = TransitionRequest(enable=True)
    assert tr.host_alias is None


# ---------------------------------------------------------------------------
# Audit payload builders
# ---------------------------------------------------------------------------


def _make_audit_session():
    return SimpleNamespace(
        id="sess-42",
        name="Audit Session",
        conversation_scope=SimpleNamespace(chat_id=777, session_uid="owner:42:alpha"),
    )


def test_build_remote_control_audit_extra_for_enabled_and_disabled_actions():
    session = _make_audit_session()
    host_cfg = SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/app")

    enabled = build_remote_control_audit_extra(
        session=session,
        actor="desktop:default",
        surface="desktop",
        action="remote_control_enabled",
        host_alias="prod",
        host_cfg=host_cfg,
        result="ok",
    )
    disabled = build_remote_control_audit_extra(
        session=session,
        actor="desktop:default",
        surface="desktop",
        action="remote_control_disabled",
        host_alias="prod",
        host_cfg=host_cfg,
        result="ok",
    )

    for action, extra in (
        ("remote_control_enabled", enabled),
        ("remote_control_disabled", disabled),
    ):
        assert extra["chat_id"] == "777"
        assert extra["session_id"] == "sess-42"
        assert extra["session_uid"] == "owner:42:alpha"
        assert extra["session_name"] == "Audit Session"
        assert extra["actor"] == "desktop:default"
        assert extra["surface"] == "desktop"
        assert extra["provider"] == "local"
        assert extra["host_alias"] == "prod"
        assert extra["host"] == "10.0.0.1"
        assert extra["remote_project_root"] == "/srv/app"
        assert extra["action"] == action
        assert extra["result"] == "ok"
        assert extra["status"] == "ok"
        assert extra["reason"] == ""
        assert extra["error"] == ""
        assert extra["path"] == "10.0.0.1"


def test_build_remote_control_audit_extra_for_host_changed_action():
    extra = build_remote_control_audit_extra(
        session=_make_audit_session(),
        actor="desktop:default",
        surface="desktop",
        action="remote_control_host_changed",
        host_alias="staging",
        host_cfg=SSHHostConfig(host="10.0.0.2", user="deploy", remote_project_root="/srv/staging"),
        result="ok",
    )

    assert extra["action"] == "remote_control_host_changed"
    assert extra["host_alias"] == "staging"
    assert extra["host"] == "10.0.0.2"
    assert extra["remote_project_root"] == "/srv/staging"
    assert extra["result"] == "ok"
    assert extra["status"] == "ok"


def test_build_remote_control_audit_extra_for_preflight_failed_action():
    extra = build_remote_control_audit_extra(
        session=_make_audit_session(),
        actor="desktop:default",
        surface="desktop",
        action="remote_control_preflight_failed",
        host_alias="prod",
        host_cfg=SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/app"),
        result="error",
        reason="permission denied",
    )

    assert extra["action"] == "remote_control_preflight_failed"
    assert extra["host"] == "10.0.0.1"
    assert extra["remote_project_root"] == "/srv/app"
    assert extra["result"] == "error"
    assert extra["status"] == "error"
    assert extra["reason"] == "permission denied"
    assert extra["error"] == "permission denied"


def test_build_remote_control_audit_extra_for_admin_override_action():
    extra = build_remote_control_audit_extra(
        session=_make_audit_session(),
        actor="1->42",
        surface="miniapp",
        action="admin_remote_override",
        host_alias="prod",
        host_cfg=SSHHostConfig(host="10.0.0.1", user="deploy", remote_project_root="/srv/app"),
        result="ok",
    )

    assert extra["actor"] == "1->42"
    assert extra["surface"] == "miniapp"
    assert extra["action"] == "admin_remote_override"
    assert extra["result"] == "ok"
    assert extra["status"] == "ok"


def test_build_remote_file_audit_extra_for_conflict_event():
    extra = build_remote_file_audit_extra(
        actor=42,
        session_uid="owner:42:alpha",
        surface="miniapp",
        action="remote_file_conflict_detected",
        path="notes.txt",
        result="conflict",
        expected_revision="rev-a",
        current_revision="rev-b",
        reason="revision conflict",
        chat_id=42,
        user_id=42,
    )

    assert extra["chat_id"] == 42
    assert extra["user_id"] == 42
    assert extra["actor"] == 42
    assert extra["session_uid"] == "owner:42:alpha"
    assert extra["surface"] == "miniapp"
    assert extra["provider"] == ""
    assert extra["host"] == ""
    assert extra["remote_project_root"] == ""
    assert extra["path"] == "notes.txt"
    assert extra["old_revision"] == ""
    assert extra["new_revision"] == ""
    assert extra["expected_revision"] == "rev-a"
    assert extra["current_revision"] == "rev-b"
    assert extra["action"] == "remote_file_conflict_detected"
    assert extra["status"] == "conflict"
    assert extra["result"] == "conflict"
    assert extra["reason"] == "revision conflict"
    assert extra["error"] == "revision conflict"


def test_build_remote_file_audit_extra_for_force_saved_event():
    extra = build_remote_file_audit_extra(
        actor=7,
        session_uid="desktop:7",
        surface="desktop",
        action="remote_file_force_saved",
        path="notes.txt",
        result="ok",
        provider="local",
        old_revision="old-rev",
        new_revision="new-rev",
    )

    assert extra["actor"] == 7
    assert extra["session_uid"] == "desktop:7"
    assert extra["surface"] == "desktop"
    assert extra["provider"] == "local"
    assert extra["host"] == ""
    assert extra["remote_project_root"] == ""
    assert extra["path"] == "notes.txt"
    assert extra["old_revision"] == "old-rev"
    assert extra["new_revision"] == "new-rev"
    assert extra["expected_revision"] == ""
    assert extra["current_revision"] == ""
    assert extra["action"] == "remote_file_force_saved"
    assert extra["status"] == "ok"
    assert extra["result"] == "ok"
    assert extra["reason"] == ""
    assert extra["error"] == ""


# ---------------------------------------------------------------------------
# RemoteControlService basic contract
# ---------------------------------------------------------------------------


def test_remote_control_service_basic_api_contract():
    svc = RemoteControlService()

    assert svc._cache_ttl_sec == PREFLIGHT_CACHE_TTL_SEC
    assert svc._preflight_cache == {}

    required_api = {
        "compute_effective_state",
        "normalize_setting_change",
        "validate_transition",
        "validate_idle",
        "validate_and_preflight",
        "run_preflight",
        "get_cached_preflight",
        "invalidate_preflight",
    }
    assert required_api.issubset(set(dir(svc)))

    for name in required_api:
        assert callable(getattr(svc, name))


# ---------------------------------------------------------------------------
# RemoteControlService.compute_effective_state
# ---------------------------------------------------------------------------


def _make_hosts(**kwargs):
    """Helper to create ssh_hosts dict."""
    hosts = {}
    for alias, overrides in kwargs.items():
        hosts[alias] = SSHHostConfig(**overrides)
    return hosts


class TestComputeEffectiveState:
    def setup_method(self):
        self.svc = RemoteControlService()

    def test_disabled_returns_local(self):
        state = SimpleNamespace(modes=ModeState(remote_control_enabled=False, remote_control_host_alias="prod"))
        hosts = _make_hosts(prod={"host": "1.1.1.1", "user": "u"})
        result = self.svc.compute_effective_state(state, hosts)
        assert result.execution_target == ExecutionTarget.LOCAL

    def test_enabled_no_alias_returns_local(self):
        state = SimpleNamespace(modes=ModeState(remote_control_enabled=True, remote_control_host_alias=None))
        result = self.svc.compute_effective_state(state, {})
        assert result.execution_target == ExecutionTarget.LOCAL

    def test_enabled_alias_not_in_hosts_returns_local(self):
        state = SimpleNamespace(modes=ModeState(remote_control_enabled=True, remote_control_host_alias="missing"))
        hosts = _make_hosts(prod={"host": "1.1.1.1", "user": "u"})
        result = self.svc.compute_effective_state(state, hosts)
        assert result.execution_target == ExecutionTarget.LOCAL

    def test_enabled_valid_alias_returns_remote(self):
        state = SimpleNamespace(modes=ModeState(
            remote_control_enabled=True,
            remote_control_host_alias="prod",
        ))
        hosts = _make_hosts(prod={"host": "1.1.1.1", "user": "u", "remote_project_root": "/srv/app"})
        result = self.svc.compute_effective_state(state, hosts)
        assert result.execution_target == ExecutionTarget.REMOTE
        assert result.host_alias == "prod"
        assert result.remote_project_root == "/srv/app"
        assert result.git_available is True

    def test_remote_without_project_root_git_unavailable(self):
        state = SimpleNamespace(modes=ModeState(
            remote_control_enabled=True,
            remote_control_host_alias="staging",
        ))
        hosts = _make_hosts(staging={"host": "2.2.2.2", "user": "ci"})
        result = self.svc.compute_effective_state(state, hosts)
        assert result.execution_target == ExecutionTarget.REMOTE
        assert result.host_alias == "staging"
        assert result.remote_project_root is None
        assert result.git_available is False

    def test_flat_session_state_without_modes(self):
        state = SimpleNamespace(remote_control_enabled=True, remote_control_host_alias="db")
        hosts = _make_hosts(db={"host": "3.3.3.3", "user": "dba", "remote_project_root": "/data"})
        result = self.svc.compute_effective_state(state, hosts)
        assert result.execution_target == ExecutionTarget.REMOTE
        assert result.host_alias == "db"

    def test_empty_hosts_dict(self):
        state = SimpleNamespace(modes=ModeState(remote_control_enabled=True, remote_control_host_alias="x"))
        result = self.svc.compute_effective_state(state, {})
        assert result.execution_target == ExecutionTarget.LOCAL

    def test_default_mode_state_returns_local(self):
        state = SimpleNamespace(modes=ModeState())
        result = self.svc.compute_effective_state(state, {})
        assert result.execution_target == ExecutionTarget.LOCAL


# ---------------------------------------------------------------------------
# Fake SSH service for preflight tests
# ---------------------------------------------------------------------------


class _FakeSSHService:
    """Minimal fake that mimics SSHService.exec for preflight testing."""

    def __init__(self, responses=None, raise_on=None):
        self.calls = []
        self._responses = responses or {}
        self._raise_on = raise_on or {}

    async def exec(self, workdir, host_alias, command, *, timeout_sec=30, chat_id=None):
        self.calls.append((workdir, host_alias, command))
        exc = self._raise_on.get(command)
        if exc is None:
            exc = self._raise_on.get(command.replace("'", ""))
        if exc is not None:
            raise exc
        resp = self._responses.get(command)
        if resp is None:
            resp = self._responses.get(command.replace("'", ""))
        if resp is None:
            resp = SimpleNamespace(stdout="ok\n", stderr="", exit_code=0)
        return resp


def _ok_resp(stdout="ok\n"):
    return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)


def _fail_resp(stderr="fail", exit_code=1):
    return SimpleNamespace(stdout="", stderr=stderr, exit_code=exit_code)


# ---------------------------------------------------------------------------
# Preflight tests
# ---------------------------------------------------------------------------


class TestRunPreflight:
    def setup_method(self):
        self.svc = RemoteControlService(cache_ttl_sec=60.0)

    def test_preflight_fails_without_project_root(self):
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "prod", cfg)
        )
        assert pf.ok is False
        assert pf.host_alias == "prod"
        assert pf.remote_project_root is None
        assert "remote_project_root is not configured" in pf.error
        assert pf.checked_at is not None
        assert len(ssh.calls) == 0

    def test_successful_preflight_with_project_root(self):
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "prod", cfg)
        )
        assert pf.ok is True
        assert pf.remote_project_root == "/srv/app"
        # Shell check + directory check + file ops check
        assert len(ssh.calls) == 3
        assert "/srv/app" in ssh.calls[1][2]

    def test_failed_ssh_connection(self):
        ssh = _FakeSSHService(raise_on={"echo ok": ConnectionError("refused")})
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "bad", cfg)
        )
        assert pf.ok is False
        assert "SSH connection failed" in pf.error
        assert pf.host_alias == "bad"

    def test_failed_shell_check(self):
        ssh = _FakeSSHService(responses={"echo ok": _fail_resp("shell error", 127)})
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "broken", cfg)
        )
        assert pf.ok is False
        assert "Remote shell check failed" in pf.error

    def test_failed_directory_check(self):
        dir_cmd = "test -d /srv/app -a -r /srv/app -a -w /srv/app && echo ok"
        ssh = _FakeSSHService(responses={dir_cmd: _fail_resp("", 1)})
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "nodir", cfg)
        )
        assert pf.ok is False
        assert "not accessible" in pf.error

    def test_failed_path_not_writable(self):
        dir_cmd = "test -d /readonly -a -r /readonly -a -w /readonly && echo ok"
        ssh = _FakeSSHService(responses={dir_cmd: _fail_resp("permission denied", 1)})
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/readonly")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "readonly", cfg)
        )
        assert pf.ok is False
        assert pf.host_alias == "readonly"
        assert pf.remote_project_root == "/readonly"
        assert "not accessible" in pf.error

    def test_directory_check_exception(self):
        dir_cmd = "test -d /data -a -r /data -a -w /data && echo ok"
        ssh = _FakeSSHService(raise_on={dir_cmd: OSError("timeout")})
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/data")

        pf = asyncio.run(
            self.svc.run_preflight(ssh, "/w", "slow", cfg)
        )
        assert pf.ok is False
        assert "Directory check command failed" in pf.error


# ---------------------------------------------------------------------------
# Preflight cache tests
# ---------------------------------------------------------------------------


class TestPreflightCache:
    def test_cache_hit_within_ttl(self):
        svc = RemoteControlService(cache_ttl_sec=60.0)
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w", "h1", cfg)
        )
        cached = svc.get_cached_preflight("/w", "h1")
        assert cached is not None
        assert cached.ok is True
        assert cached.host_alias == "h1"

    def test_cache_miss_no_entry(self):
        svc = RemoteControlService()
        assert svc.get_cached_preflight("/w", "nope") is None

    def test_cache_miss_expired(self):
        svc = RemoteControlService(cache_ttl_sec=0.0)
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w", "exp", cfg)
        )
        # TTL=0 means immediately expired
        assert svc.get_cached_preflight("/w", "exp") is None

    def test_invalidate_specific(self):
        svc = RemoteControlService(cache_ttl_sec=300.0)
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w", "a", cfg)
        )
        asyncio.run(
            svc.run_preflight(ssh, "/w", "b", cfg)
        )
        svc.invalidate_preflight("/w", "a")
        assert svc.get_cached_preflight("/w", "a") is None
        assert svc.get_cached_preflight("/w", "b") is not None

    def test_invalidate_by_workdir(self):
        svc = RemoteControlService(cache_ttl_sec=300.0)
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w1", "a", cfg)
        )
        asyncio.run(
            svc.run_preflight(ssh, "/w2", "a", cfg)
        )
        svc.invalidate_preflight(workdir="/w1")
        assert svc.get_cached_preflight("/w1", "a") is None
        assert svc.get_cached_preflight("/w2", "a") is not None

    def test_invalidate_all(self):
        svc = RemoteControlService(cache_ttl_sec=300.0)
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w1", "a", cfg)
        )
        asyncio.run(
            svc.run_preflight(ssh, "/w2", "b", cfg)
        )
        svc.invalidate_preflight()
        assert svc.get_cached_preflight("/w1", "a") is None
        assert svc.get_cached_preflight("/w2", "b") is None

    def test_failed_preflight_also_cached(self):
        svc = RemoteControlService(cache_ttl_sec=300.0)
        ssh = _FakeSSHService(raise_on={"echo ok": ConnectionError("refused")})
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w", "fail", cfg)
        )
        cached = svc.get_cached_preflight("/w", "fail")
        assert cached is not None
        assert cached.ok is False

    def test_rerun_overwrites_cache(self):
        svc = RemoteControlService(cache_ttl_sec=300.0)
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        # First: fail
        ssh_fail = _FakeSSHService(raise_on={"echo ok": ConnectionError("down")})
        asyncio.run(
            svc.run_preflight(ssh_fail, "/w", "h", cfg)
        )
        assert svc.get_cached_preflight("/w", "h").ok is False

        # Second: succeed
        svc.invalidate_preflight("/w", "h")
        ssh_ok = _FakeSSHService()
        asyncio.run(
            svc.run_preflight(ssh_ok, "/w", "h", cfg)
        )
        assert svc.get_cached_preflight("/w", "h").ok is True

    def test_sequential_hosts_keep_independent_cached_results(self):
        svc = RemoteControlService(cache_ttl_sec=300.0)
        ssh = _FakeSSHService()
        cfg = SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app")

        asyncio.run(
            svc.run_preflight(ssh, "/w", "alpha", cfg)
        )
        asyncio.run(
            svc.run_preflight(ssh, "/w", "beta", cfg)
        )

        alpha = svc.get_cached_preflight("/w", "alpha")
        beta = svc.get_cached_preflight("/w", "beta")
        assert alpha is not None
        assert beta is not None
        assert alpha.host_alias == "alpha"
        assert beta.host_alias == "beta"
        assert alpha is not beta

    def test_default_ttl(self):
        assert PREFLIGHT_CACHE_TTL_SEC == 300.0


# ---------------------------------------------------------------------------
# ValidationResult dataclass
# ---------------------------------------------------------------------------


def test_validation_result_ok():
    vr = ValidationResult(ok=True)
    assert vr.ok is True
    assert vr.error is None


def test_validation_result_error():
    vr = ValidationResult(ok=False, error="busy")
    assert vr.ok is False
    assert vr.error == "busy"


# ---------------------------------------------------------------------------
# Helpers for transition tests
# ---------------------------------------------------------------------------


def _idle_session():
    """Fake idle session (not busy, no run_lock, no tick activity)."""
    lock = asyncio.Lock()
    return SimpleNamespace(
        busy=False,
        run_lock=lock,
        is_active_by_tick=lambda: False,
        modes=ModeState(ssh_remote_enabled=True),
    )


def _busy_session():
    s = SimpleNamespace(
        busy=True,
        run_lock=asyncio.Lock(),
        is_active_by_tick=lambda: False,
        modes=ModeState(ssh_remote_enabled=True),
    )
    return s


class _PreLockedLock:
    """Fake lock that always reports as locked."""
    def locked(self) -> bool:
        return True


def _locked_session():
    return SimpleNamespace(
        busy=False,
        run_lock=_PreLockedLock(),
        is_active_by_tick=lambda: False,
        modes=ModeState(ssh_remote_enabled=True),
    )


def _tick_active_session():
    return SimpleNamespace(
        busy=False,
        run_lock=asyncio.Lock(),
        is_active_by_tick=lambda: True,
        modes=ModeState(ssh_remote_enabled=True),
    )


# ---------------------------------------------------------------------------
# validate_transition tests
# ---------------------------------------------------------------------------


class TestValidateTransition:
    def setup_method(self):
        self.svc = RemoteControlService()
        self.hosts = {
            "prod": SSHHostConfig(
                host="1.1.1.1",
                user="u",
                allowed_chat_ids=[100, 200],
                remote_project_root="/srv/app",
            ),
            "open": SSHHostConfig(
                host="2.2.2.2",
                user="u",
                remote_project_root="/srv/open",
            ),  # no ACL
        }

    # --- idle-only guard (REQ-12) ---

    def test_idle_session_allowed(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, chat_id=100,
        )
        assert vr.ok is True

    def test_busy_session_rejected(self):
        vr = self.svc.validate_transition(
            _busy_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, chat_id=100,
        )
        assert vr.ok is False
        assert "busy" in vr.error

    def test_locked_session_rejected(self):
        vr = self.svc.validate_transition(
            _locked_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, chat_id=100,
        )
        assert vr.ok is False
        assert "busy" in vr.error

    def test_tick_active_session_rejected(self):
        vr = self.svc.validate_transition(
            _tick_active_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, chat_id=100,
        )
        assert vr.ok is False
        assert "busy" in vr.error

    def test_recovery_after_busy_cleared(self):
        session = _busy_session()
        session.busy = False
        vr = self.svc.validate_transition(
            session, TransitionRequest(enable=True, host_alias="open"),
            self.hosts, chat_id=1,
        )
        assert vr.ok is True

    def test_recovery_after_lock_released(self):
        session = _locked_session()
        session.run_lock = asyncio.Lock()  # replace with unlocked lock
        vr = self.svc.validate_transition(
            session, TransitionRequest(enable=True, host_alias="open"),
            self.hosts, chat_id=1,
        )
        assert vr.ok is True

    def test_recovery_after_tick_cleared(self):
        session = SimpleNamespace(
            busy=False,
            run_lock=asyncio.Lock(),
            is_active_by_tick=lambda: False,
            modes=ModeState(ssh_remote_enabled=True),
        )
        vr = self.svc.validate_transition(
            session, TransitionRequest(enable=True, host_alias="open"),
            self.hosts, chat_id=1,
        )
        assert vr.ok is True

    # --- ACL checks (REQ-11) ---

    def test_acl_denied_chat(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, chat_id=999,
        )
        assert vr.ok is False
        assert "not allowed" in vr.error

    def test_acl_no_restriction(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=True, host_alias="open"),
            self.hosts, chat_id=999,
        )
        assert vr.ok is True

    def test_acl_admin_override(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, chat_id=999, is_admin=True,
        )
        assert vr.ok is True

    def test_host_not_found(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=True, host_alias="missing"),
            self.hosts, chat_id=100,
        )
        assert vr.ok is False
        assert "not found" in vr.error

    def test_disable_skips_acl(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=False, host_alias="prod"),
            self.hosts, chat_id=999,
        )
        assert vr.ok is True

    def test_enable_no_alias_skips_acl(self):
        vr = self.svc.validate_transition(
            _idle_session(), TransitionRequest(enable=True, host_alias=None),
            self.hosts, chat_id=999,
        )
        assert vr.ok is False
        assert "remote_control_host_alias is required" in vr.error

    # --- fallback for SimpleNamespace without run_lock/is_active_by_tick ---

    def test_minimal_session_no_lock(self):
        session = SimpleNamespace(busy=False, ssh_remote_enabled=True)
        vr = self.svc.validate_transition(
            session, TransitionRequest(enable=True, host_alias="open"),
            self.hosts, chat_id=1,
        )
        assert vr.ok is True

    def test_minimal_session_busy(self):
        session = SimpleNamespace(busy=True)
        vr = self.svc.validate_transition(
            session, TransitionRequest(enable=True, host_alias="open"),
            self.hosts, chat_id=1,
        )
        assert vr.ok is False


# ---------------------------------------------------------------------------
# validate_and_preflight tests
# ---------------------------------------------------------------------------


class TestValidateAndPreflight:
    def setup_method(self):
        self.svc = RemoteControlService(cache_ttl_sec=60.0)
        self.hosts = {
            "prod": SSHHostConfig(
                host="1.1.1.1",
                user="u",
                allowed_chat_ids=[100],
                remote_project_root="/srv/app",
            ),
        }

    def test_validation_fails_no_preflight(self):
        vr, pf = asyncio.run(self.svc.validate_and_preflight(
            _busy_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, _FakeSSHService(), "/w", chat_id=100,
        ))
        assert vr.ok is False
        assert pf is None

    def test_disable_skips_preflight(self):
        vr, pf = asyncio.run(self.svc.validate_and_preflight(
            _idle_session(), TransitionRequest(enable=False),
            self.hosts, _FakeSSHService(), "/w", chat_id=100,
        ))
        assert vr.ok is True
        assert pf is None

    def test_enable_runs_preflight(self):
        ssh = _FakeSSHService()
        vr, pf = asyncio.run(self.svc.validate_and_preflight(
            _idle_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, ssh, "/w", chat_id=100,
        ))
        assert vr.ok is True
        assert pf is not None
        assert pf.ok is True
        assert len(ssh.calls) > 0

    def test_acl_fails_no_preflight(self):
        ssh = _FakeSSHService()
        vr, pf = asyncio.run(self.svc.validate_and_preflight(
            _idle_session(), TransitionRequest(enable=True, host_alias="prod"),
            self.hosts, ssh, "/w", chat_id=999,
        ))
        assert vr.ok is False
        assert pf is None
        assert len(ssh.calls) == 0

    def test_host_not_found_no_preflight(self):
        ssh = _FakeSSHService()
        vr, pf = asyncio.run(self.svc.validate_and_preflight(
            _idle_session(), TransitionRequest(enable=True, host_alias="missing"),
            self.hosts, ssh, "/w", chat_id=100,
        ))
        assert vr.ok is False
        assert pf is None


# ---------------------------------------------------------------------------
# normalize_setting_change tests
# ---------------------------------------------------------------------------


class TestNormalizeSettingChange:
    def setup_method(self):
        self.svc = RemoteControlService(cache_ttl_sec=300.0)
        self.hosts = {
            "prod": SSHHostConfig(host="1.1.1.1", user="u", remote_project_root="/srv/app"),
            "staging": SSHHostConfig(host="2.2.2.2", user="ci"),
        }

    def test_ssh_disabled_cascades_to_remote_control(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=True,
            remote_control_host_alias="prod",
        ))
        es = self.svc.normalize_setting_change(session, "ssh_remote_enabled", False, self.hosts)
        assert session.modes.remote_control_enabled is False
        assert session.modes.remote_control_host_alias == "prod"  # alias preserved
        assert es.execution_target == ExecutionTarget.LOCAL

    def test_ssh_disabled_noop_if_rc_already_off(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=False,
        ))
        es = self.svc.normalize_setting_change(session, "ssh_remote_enabled", False, self.hosts)
        assert session.modes.remote_control_enabled is False
        assert es.execution_target == ExecutionTarget.LOCAL

    def test_ssh_enabled_no_cascade(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=False,
            remote_control_enabled=False,
        ))
        es = self.svc.normalize_setting_change(session, "ssh_remote_enabled", True, self.hosts)
        assert session.modes.remote_control_enabled is False
        assert es.execution_target == ExecutionTarget.LOCAL

    def test_host_alias_change_invalidates_cache(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=True,
            remote_control_host_alias="prod",
        ))
        # Prime the cache
        self.svc._preflight_cache[("/w", "prod")] = PreflightResult(
            ok=True, host_alias="prod", checked_at=time.time(),
        )
        self.svc.normalize_setting_change(
            session, "remote_control_host_alias", "staging", self.hosts, "/w",
        )
        assert session.modes.remote_control_host_alias == "staging"
        assert self.svc.get_cached_preflight("/w", "prod") is None

    def test_host_alias_change_to_same_no_invalidation(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=True,
            remote_control_host_alias="prod",
        ))
        self.svc._preflight_cache[("/w", "prod")] = PreflightResult(
            ok=True, host_alias="prod", checked_at=time.time(),
        )
        self.svc.normalize_setting_change(
            session, "remote_control_host_alias", "prod", self.hosts, "/w",
        )
        assert self.svc.get_cached_preflight("/w", "prod") is not None

    def test_rc_enabled_recomputes_effective_state(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=False,
            remote_control_host_alias="prod",
        ))
        es = self.svc.normalize_setting_change(
            session, "remote_control_enabled", True, self.hosts,
        )
        assert session.modes.remote_control_enabled is True
        assert es.execution_target == ExecutionTarget.REMOTE
        assert es.host_alias == "prod"

    def test_rc_enabled_blocked_without_ssh(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=False,
            remote_control_enabled=False,
            remote_control_host_alias="prod",
        ))
        es = self.svc.normalize_setting_change(
            session, "remote_control_enabled", True, self.hosts,
        )
        assert session.modes.remote_control_enabled is False
        assert es.execution_target == ExecutionTarget.LOCAL

    def test_rc_disabled_recomputes_to_local(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=True,
            remote_control_host_alias="prod",
        ))
        es = self.svc.normalize_setting_change(
            session, "remote_control_enabled", False, self.hosts,
        )
        assert session.modes.remote_control_enabled is False
        assert es.execution_target == ExecutionTarget.LOCAL

    def test_host_alias_clear_to_none(self):
        session = SimpleNamespace(modes=ModeState(
            ssh_remote_enabled=True,
            remote_control_enabled=True,
            remote_control_host_alias="prod",
        ))
        self.svc._preflight_cache[("/w", "prod")] = PreflightResult(
            ok=True, host_alias="prod", checked_at=time.time(),
        )
        es = self.svc.normalize_setting_change(
            session, "remote_control_host_alias", "", self.hosts, "/w",
        )
        assert session.modes.remote_control_host_alias is None
        assert self.svc.get_cached_preflight("/w", "prod") is None
        assert es.execution_target == ExecutionTarget.LOCAL


# ---------------------------------------------------------------------------
# Integration: BotApp has remote_control_service
# ---------------------------------------------------------------------------


def test_botapp_has_remote_control_service(tmp_path):
    import yaml
    from bot import BotApp
    from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
    from miniapp.services.config_service import app_config_to_dict

    cfg = AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["echo"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=True),
    )
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(app_config_to_dict(cfg), f, sort_keys=False)

    app_inst = BotApp(cfg)
    assert hasattr(app_inst, "remote_control_service")
    from app.services.remote_control_service import RemoteControlService
    assert isinstance(app_inst.remote_control_service, RemoteControlService)
    app_inst.shutdown_html_process_pool()


# ---------------------------------------------------------------------------
# Integration: MiniApp settings normalization
# ---------------------------------------------------------------------------


def test_miniapp_ssh_disable_cascades_remote_control(tmp_path):
    """MiniApp: disabling ssh_remote_enabled cascades to remote_control_enabled."""
    import hashlib
    import hmac
    import json
    from urllib.parse import quote

    import yaml
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from bot import BotApp
    from config import AppConfig, DefaultsConfig, MCPConfig, MiniAppConfig, TelegramConfig, ToolConfig
    from miniapp.routes import MiniAppRoutes
    from miniapp.services.config_service import app_config_to_dict

    cfg = AppConfig(
        telegram=TelegramConfig(token="t", whitelist_chat_ids=[1], admlist_chat_ids=[1]),
        tools={"dummy": ToolConfig(name="dummy", mode="headless", cmd=["echo"])},
        defaults=DefaultsConfig(
            workdir=str(tmp_path),
            state_path=str(tmp_path / "state.json"),
            toolhelp_path=str(tmp_path / "toolhelp.json"),
            log_path=str(tmp_path / "bot.log"),
        ),
        mcp=MCPConfig(enabled=False),
        mcp_clients=[],
        presets=[],
        path=str(tmp_path / "config.yaml"),
        miniapp=MiniAppConfig(enabled=True),
    )
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.safe_dump(app_config_to_dict(cfg), f, sort_keys=False)

    def _init_data(bot_token, user_id):
        payload = {
            "auth_date": str(int(time.time())),
            "query_id": "q1",
            "user": json.dumps({"id": user_id, "username": f"u{user_id}", "first_name": "U"}, ensure_ascii=False),
        }
        check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        sig = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return f"auth_date={payload['auth_date']}&query_id=q1&user={quote(payload['user'])}&hash={sig}"

    async def _run():
        app_inst = BotApp(cfg)
        session = app_inst.manager.create(1, "dummy", str(tmp_path))
        # Pre-set: SSH enabled + remote control enabled
        session.modes.ssh_remote_enabled = True
        session.modes.remote_control_enabled = True
        session.modes.remote_control_host_alias = "prod"

        routes = MiniAppRoutes(app_inst)
        web_app = web.Application()
        routes.register(web_app)

        admin_data = _init_data("t", 1)
        from session import session_runtime_uid
        uid = session_runtime_uid(session)

        async with TestClient(TestServer(web_app)) as client:
            # Disable SSH
            resp = await client.put(
                f"/api/session/{uid}/settings",
                json={"ssh_remote_enabled": False},
                headers={"X-Telegram-Init-Data": admin_data},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert "ssh_remote_enabled" in body["changed"]

        # Verify cascade
        assert session.modes.ssh_remote_enabled is False
        assert session.modes.remote_control_enabled is False
        assert session.modes.remote_control_host_alias == "prod"  # preserved

        app_inst.shutdown_html_process_pool()

    asyncio.run(_run())
