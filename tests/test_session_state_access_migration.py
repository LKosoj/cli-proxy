from __future__ import annotations

import types

from sessions.session_state_access import (
    get_active_mode,
    get_orchestrator_last_mode_output,
    get_orchestrator_pending_input,
    is_orchestrator_enabled,
    set_active_mode,
    set_orchestrator_enabled,
    set_orchestrator_last_mode_id,
    set_orchestrator_last_mode_output,
    set_orchestrator_pending_input,
)


def test_run_scoped_nested_state_does_not_leak_between_sequential_intents() -> None:
    session = types.SimpleNamespace(
        modes=types.SimpleNamespace(active_mode=None, analyst_mode="spec"),
        orchestrator=types.SimpleNamespace(
            enabled=False,
            pending_input=None,
            last_mode_output=None,
            last_mode_id=None,
        ),
    )

    set_active_mode(session, "analyst")
    set_orchestrator_enabled(session, True)
    set_orchestrator_pending_input(session, {"text": "intent-a"})
    set_orchestrator_last_mode_output(session, "report-a")
    set_orchestrator_last_mode_id(session, "analyst")

    first_pending = dict(get_orchestrator_pending_input(session, {}) or {})
    first_output = str(get_orchestrator_last_mode_output(session, "") or "")

    set_active_mode(session, "manager")
    set_orchestrator_pending_input(session, {"text": "intent-b"})
    set_orchestrator_last_mode_output(session, "report-b")
    set_orchestrator_last_mode_id(session, "manager")

    second_pending = dict(get_orchestrator_pending_input(session, {}) or {})
    second_output = str(get_orchestrator_last_mode_output(session, "") or "")

    assert is_orchestrator_enabled(session) is True
    assert get_active_mode(session, "") == "manager"
    assert first_pending.get("text") == "intent-a"
    assert second_pending.get("text") == "intent-b"
    assert second_pending != first_pending
    assert first_output == "report-a"
    assert second_output == "report-b"
    assert second_output != first_output
