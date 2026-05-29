import types

import pytest

from app.services.run_operations_policy import PolicyDecision, RunOperationsPolicy


def _session(chat_id=42):
    return types.SimpleNamespace(
        chat_id=chat_id,
        conversation_scope=types.SimpleNamespace(chat_id=chat_id),
    )


@pytest.mark.parametrize("surface", ["telegram", "miniapp", "desktop"])
@pytest.mark.parametrize("operation", ["doctor", "recover", "resume", "apply_recommendation", "promote_skills"])
def test_run_operations_policy_allows_admin_on_supported_surfaces(surface, operation) -> None:
    decision = RunOperationsPolicy().can_run_operation(
        operation=operation,
        user_id=999,
        is_admin=True,
        session=_session(chat_id=42),
        surface=surface,
    )

    assert isinstance(decision, PolicyDecision)
    assert decision.allowed is True
    assert decision.reason == "admin_allowed"
    assert decision.visibility == "show"


@pytest.mark.parametrize("surface", ["telegram", "miniapp", "desktop"])
def test_run_operations_policy_allows_owner_non_admin_doctor(surface) -> None:
    decision = RunOperationsPolicy().can_run_operation(
        operation="doctor",
        user_id=42,
        is_admin=False,
        session=_session(chat_id=42),
        surface=surface,
    )

    assert decision.allowed is True
    assert decision.reason == "owner_allowed"
    assert decision.visibility == "show"


@pytest.mark.parametrize("surface", ["telegram", "miniapp", "desktop"])
@pytest.mark.parametrize("operation", ["recover", "resume", "apply_recommendation", "promote_skills"])
def test_run_operations_policy_denies_non_admin_write_operations(surface, operation) -> None:
    decision = RunOperationsPolicy().can_run_operation(
        operation=operation,
        user_id=42,
        is_admin=False,
        session=_session(chat_id=42),
        surface=surface,
    )

    assert decision.allowed is False
    assert decision.reason == "admin_required"
    assert decision.visibility == "hide"


@pytest.mark.parametrize("surface", ["telegram", "miniapp", "desktop"])
def test_run_operations_policy_denies_unknown_operation(surface) -> None:
    decision = RunOperationsPolicy().can_run_operation(
        operation="delete_everything",
        user_id=42,
        is_admin=True,
        session=_session(chat_id=42),
        surface=surface,
    )

    assert decision.allowed is False
    assert decision.reason == "unknown_operation"
    assert decision.visibility == "hide"


@pytest.mark.parametrize("surface", ["telegram", "miniapp", "desktop"])
def test_run_operations_policy_denies_non_owner_non_admin_doctor(surface) -> None:
    decision = RunOperationsPolicy().can_run_operation(
        operation="doctor",
        user_id=777,
        is_admin=False,
        session=_session(chat_id=42),
        surface=surface,
    )

    assert decision.allowed is False
    assert decision.reason == "owner_or_admin_required"
    assert decision.visibility == "hide"
