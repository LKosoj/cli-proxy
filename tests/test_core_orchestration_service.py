"""Tests for app.services.core_orchestration_service."""

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.core_orchestration_service import CoreOrchestrationService


def _make_session(session_id: str = "test_session") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=session_id,
        busy=False,
        _runtime_progress=None,
    )


def _make_mode(return_value: str = "result_text") -> MagicMock:
    mode = MagicMock()
    mode.run_pipeline = AsyncMock(return_value=return_value)
    return mode


def _make_service() -> CoreOrchestrationService:
    adv_orch = MagicMock()
    adv_orch.build_handoff_input = MagicMock(return_value="full_previous_output")
    return CoreOrchestrationService(advanced_orchestrator=adv_orch)


def test_core_orchestration_service_delegates_handoff_to_advanced_orchestrator():
    adv_orch = MagicMock()
    adv_orch.build_handoff_input = MagicMock(return_value="full_previous_output_text")
    svc = CoreOrchestrationService(advanced_orchestrator=adv_orch)
    session = _make_session()

    result = svc.build_handoff_input(session=session, original_user_text="original")

    adv_orch.build_handoff_input.assert_called_once_with(
        session=session,
        original_user_text="original",
    )
    assert result == "full_previous_output_text"


@patch("app.services.core_orchestration_service.emit_runtime_progress")
def test_core_orchestration_service_emits_run_started_and_run_finished(mock_emit):
    svc = _make_service()
    mode = _make_mode("output_text")
    session = _make_session()

    result = asyncio.run(svc.execute_mode_run(
        session=session,
        mode=mode,
        mode_id="analyst",
        user_text="prompt",
        bot_app=MagicMock(),
        context=MagicMock(),
        dest={"chat_id": 123},
    ))

    assert result == "output_text"
    mode.run_pipeline.assert_called_once()
    # At least 2 emit calls: run_started + run_finished.
    assert mock_emit.call_count >= 2
    events = [call.args[1] for call in mock_emit.call_args_list]
    event_types = [e.get("event_type") for e in events]
    assert "run_started" in event_types
    assert "run_finished" in event_types


@patch("app.services.core_orchestration_service.emit_runtime_progress")
def test_core_orchestration_service_emits_run_failed_and_reraises(mock_emit):
    svc = _make_service()
    mode = _make_mode()
    mode.run_pipeline = AsyncMock(side_effect=RuntimeError("boom"))
    session = _make_session()

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(svc.execute_mode_run(
            session=session,
            mode=mode,
            mode_id="analyst",
            user_text="prompt",
            bot_app=MagicMock(),
            context=MagicMock(),
            dest={"chat_id": 123},
        ))

    events = [call.args[1] for call in mock_emit.call_args_list]
    event_types = [e.get("event_type") for e in events]
    assert "run_started" in event_types
    assert "run_failed" in event_types
    # run_finished must NOT appear on the error path.
    assert "run_finished" not in event_types


@patch("app.services.core_orchestration_service.emit_runtime_progress")
def test_core_orchestration_service_no_state_leak_between_runs(mock_emit):
    svc = _make_service()

    # First run: analyst.
    mode1 = _make_mode("output_1")
    session1 = _make_session("session_A")
    result1 = asyncio.run(svc.execute_mode_run(
        session=session1, mode=mode1, mode_id="analyst",
        user_text="p1", bot_app=MagicMock(), context=MagicMock(), dest={},
    ))

    first_run_count = mock_emit.call_count

    # Second run: manager.
    mode2 = _make_mode("output_2")
    session2 = _make_session("session_B")
    result2 = asyncio.run(svc.execute_mode_run(
        session=session2, mode=mode2, mode_id="manager",
        user_text="p2", bot_app=MagicMock(), context=MagicMock(), dest={},
    ))

    assert result1 == "output_1"
    assert result2 == "output_2"

    # Verify second run emitted its own events with correct mode_id.
    second_run_events = [
        call.args[1] for call in mock_emit.call_args_list[first_run_count:]
    ]
    for ev in second_run_events:
        assert ev.get("mode_id") == "manager"

    # First run events had mode_id=analyst.
    first_run_events = [
        call.args[1] for call in mock_emit.call_args_list[:first_run_count]
    ]
    for ev in first_run_events:
        assert ev.get("mode_id") == "analyst"
