"""Tests for SddState integration: defaults, Session field, round-trip, compat, helpers."""
from __future__ import annotations

import os

from app.services.state_repository import get_state_repository
from config import load_config
from session import SddState, SessionManager
from modes.sdd.state import clear_sdd_gate, get_sdd_state, set_sdd_phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path):
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config_example.yaml"))
    cfg.defaults.state_path = str(tmp_path / "state.json")
    cfg.defaults.workdir = str(tmp_path)
    return cfg


# ---------------------------------------------------------------------------
# 1. SddState defaults
# ---------------------------------------------------------------------------

def test_sdd_state_defaults():
    s = SddState()
    assert s.feature_slug is None
    assert s.spec_dir is None
    assert s.phase == "idle"
    assert s.pending_gate is None
    assert s.constitution_path is None
    assert s.source_intent is None
    assert s.project_init_status == "idle"
    assert s.project_init_step == ""
    assert s.project_init_kind == ""
    assert s.project_init_error == ""


# ---------------------------------------------------------------------------
# 2. Session created with default sdd field
# ---------------------------------------------------------------------------

def test_session_has_default_sdd(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    assert isinstance(session.sdd, SddState)
    assert session.sdd.phase == "idle"
    assert session.sdd.feature_slug is None


# ---------------------------------------------------------------------------
# 3. Round-trip: all 6 fields survive persist → new SessionManager
# ---------------------------------------------------------------------------

def test_sdd_state_round_trip(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))

    session.sdd.feature_slug = "my-feature"
    session.sdd.spec_dir = "specs/001-my-feature"
    session.sdd.phase = "plan"
    session.sdd.pending_gate = "approve"
    session.sdd.constitution_path = "specs/CONSTITUTION.md"
    session.sdd.source_intent = "build login flow"
    session.sdd.project_init_status = "done"
    session.sdd.project_init_step = "done"
    session.sdd.project_init_kind = "existing_codebase"
    session.sdd.project_profile_path = "specs/_project/project-profile.generated.md"
    session.sdd.project_init_snapshot_path = "specs/_project/project-init-snapshot.json"

    sm._persist_sessions()

    sm2 = SessionManager(cfg)
    restored = sm2.get(1, session.id)
    assert restored is not None
    assert restored.sdd.feature_slug == "my-feature"
    assert restored.sdd.spec_dir == "specs/001-my-feature"
    assert restored.sdd.phase == "plan"
    assert restored.sdd.pending_gate == "approve"
    assert restored.sdd.constitution_path == "specs/CONSTITUTION.md"
    assert restored.sdd.source_intent == "build login flow"
    assert restored.sdd.project_init_status == "done"
    assert restored.sdd.project_init_step == "done"
    assert restored.sdd.project_init_kind == "existing_codebase"
    assert restored.sdd.project_profile_path == "specs/_project/project-profile.generated.md"
    assert restored.sdd.project_init_snapshot_path == "specs/_project/project-init-snapshot.json"


# ---------------------------------------------------------------------------
# 4. Backward compatibility: payload without "sdd" key → default SddState
# ---------------------------------------------------------------------------

def test_sdd_backward_compat_missing_key(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    sm._persist_sessions()

    # Remove the "sdd" key from persisted state to simulate old payload.
    repo = get_state_repository(cfg.defaults.state_path)
    by_chat = repo.load_sessions_by_chat()
    entry = by_chat["1"]["sessions"][session.id]
    entry.pop("sdd", None)
    repo.save_sessions_by_chat(by_chat)

    sm2 = SessionManager(cfg)
    restored = sm2.get(1, session.id)
    assert restored is not None
    assert isinstance(restored.sdd, SddState)
    assert restored.sdd.phase == "idle"
    assert restored.sdd.feature_slug is None
    assert restored.sdd.spec_dir is None
    assert restored.sdd.pending_gate is None
    assert restored.sdd.constitution_path is None
    assert restored.sdd.source_intent is None
    assert restored.sdd.project_init_status == "idle"
    assert restored.sdd.project_init_step == ""
    assert restored.sdd.project_init_kind == ""


# ---------------------------------------------------------------------------
# 5. Helpers: get_sdd_state, set_sdd_phase, clear_sdd_gate
# ---------------------------------------------------------------------------

def test_get_sdd_state_returns_sdd(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    state = get_sdd_state(session)
    assert state is session.sdd
    assert isinstance(state, SddState)


def test_set_sdd_phase(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    set_sdd_phase(session, "tasks")
    assert session.sdd.phase == "tasks"


def test_set_sdd_phase_empty_defaults_to_idle(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    set_sdd_phase(session, "")
    assert session.sdd.phase == "idle"


def test_clear_sdd_gate(tmp_path):
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    session.sdd.pending_gate = "review"
    clear_sdd_gate(session)
    assert session.sdd.pending_gate is None


def test_get_sdd_state_repairs_corrupt_field(tmp_path):
    """If sdd field is somehow not SddState, get_sdd_state repairs it."""
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    session.sdd = None  # type: ignore[assignment]
    state = get_sdd_state(session)
    assert isinstance(state, SddState)
    assert state.phase == "idle"


# ---------------------------------------------------------------------------
# 6. Round-trip for last_action field (persist + restore)
# ---------------------------------------------------------------------------

def test_sdd_last_action_round_trip(tmp_path):
    """last_action survives persist → new SessionManager (revise flow restart-safe)."""
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))

    session.sdd.pending_gate = "specify"
    session.sdd.phase = "specify"
    session.sdd.last_action = "gate_revise"

    sm._persist_sessions()

    sm2 = SessionManager(cfg)
    restored = sm2.get(1, session.id)
    assert restored is not None
    assert restored.sdd.pending_gate == "specify"
    assert restored.sdd.phase == "specify"
    assert restored.sdd.last_action == "gate_revise"


def test_sdd_last_action_default_empty(tmp_path):
    """last_action defaults to empty string and survives a no-op round-trip."""
    cfg = _make_config(tmp_path)
    sm = SessionManager(cfg)
    session = sm.create(1, "codex", str(tmp_path))
    assert session.sdd.last_action == ""

    sm._persist_sessions()
    sm2 = SessionManager(cfg)
    restored = sm2.get(1, session.id)
    assert restored is not None
    assert restored.sdd.last_action == ""
