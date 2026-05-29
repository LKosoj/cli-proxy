from modes.admin.autonomy_policy import AutonomyPolicy
from modes.admin.chat_service import (
    _evaluate_autopilot,
    _resolve_intent_server_id,
)


def _policy(**overrides) -> AutonomyPolicy:
    defaults = dict(enabled=True, auto_exec_actions=[], auto_exec_adhoc_commands=[])
    defaults.update(overrides)
    return AutonomyPolicy(**defaults)


def test_disabled_policy_blocks_all():
    verdict = _evaluate_autopilot(
        {"type": "propose_action", "action_id": "systemd.restart_nginx"},
        AutonomyPolicy(enabled=False, auto_exec_actions=["systemd.restart_nginx"]),
    )
    assert verdict.allowed is False
    assert "disabled" in (verdict.reason or "")


def test_propose_action_allowed_when_in_allowlist():
    verdict = _evaluate_autopilot(
        {"type": "propose_action", "action_id": "systemd.restart_nginx"},
        _policy(auto_exec_actions=["systemd.restart_nginx"]),
    )
    assert verdict.allowed is True
    assert verdict.reason is None


def test_propose_action_blocked_when_not_in_allowlist():
    verdict = _evaluate_autopilot(
        {"type": "propose_action", "action_id": "systemd.reload"},
        _policy(auto_exec_actions=["systemd.restart_nginx"]),
    )
    assert verdict.allowed is False
    assert "systemd.reload" in (verdict.reason or "")


def test_propose_action_blocked_when_action_id_empty():
    verdict = _evaluate_autopilot(
        {"type": "propose_action", "action_id": ""},
        _policy(auto_exec_actions=["a"]),
    )
    assert verdict.allowed is False
    assert "missing" in (verdict.reason or "")


def test_propose_new_action_allowed_when_argv_head_in_allowlist():
    verdict = _evaluate_autopilot(
        {"type": "propose_new_action", "argv": ["ls", "-la"]},
        _policy(auto_exec_adhoc_commands=["ls"]),
    )
    assert verdict.allowed is True


def test_propose_new_action_blocked_when_argv_head_missing():
    verdict = _evaluate_autopilot(
        {"type": "propose_new_action", "argv": ["rm", "-rf", "/tmp/x"]},
        _policy(auto_exec_adhoc_commands=["ls"]),
    )
    assert verdict.allowed is False
    assert "rm" in (verdict.reason or "")


def test_propose_new_action_blocked_when_argv_empty():
    verdict = _evaluate_autopilot(
        {"type": "propose_new_action", "argv": []},
        _policy(auto_exec_adhoc_commands=["ls"]),
    )
    assert verdict.allowed is False


def test_propose_plan_allowed_when_all_steps_pass():
    intent = {
        "type": "propose_plan",
        "steps": [
            {"target": "local", "action_id": "systemd.restart_nginx"},
            {"target": "local", "argv": ["ls", "-la"]},
        ],
    }
    verdict = _evaluate_autopilot(
        intent,
        _policy(
            auto_exec_actions=["systemd.restart_nginx"],
            auto_exec_adhoc_commands=["ls"],
        ),
    )
    assert verdict.allowed is True


def test_propose_plan_blocked_when_one_step_fails():
    intent = {
        "type": "propose_plan",
        "steps": [
            {"target": "local", "action_id": "systemd.restart_nginx"},
            {"target": "local", "argv": ["rm", "-rf", "/x"]},
        ],
    }
    verdict = _evaluate_autopilot(
        intent,
        _policy(
            auto_exec_actions=["systemd.restart_nginx"],
            auto_exec_adhoc_commands=["ls"],
        ),
    )
    assert verdict.allowed is False
    assert "step 2" in (verdict.reason or "")
    assert "rm" in (verdict.reason or "")


def test_propose_plan_blocked_when_empty():
    verdict = _evaluate_autopilot(
        {"type": "propose_plan", "steps": []},
        _policy(auto_exec_actions=["x"]),
    )
    assert verdict.allowed is False
    assert "empty" in (verdict.reason or "")


def test_propose_plan_step_without_action_or_argv_blocked():
    verdict = _evaluate_autopilot(
        {"type": "propose_plan", "steps": [{"target": "local"}]},
        _policy(auto_exec_actions=["x"], auto_exec_adhoc_commands=["ls"]),
    )
    assert verdict.allowed is False
    assert "neither action_id nor argv" in (verdict.reason or "")


def test_unknown_intent_type_blocked():
    verdict = _evaluate_autopilot(
        {"type": "answer"},
        _policy(auto_exec_actions=["x"]),
    )
    assert verdict.allowed is False
    assert "unsupported" in (verdict.reason or "")


def test_resolve_intent_server_id_single_target():
    assert _resolve_intent_server_id(
        {"type": "propose_action", "target": "web-01"}
    ) == "web-01"


def test_resolve_intent_server_id_local_is_empty():
    assert _resolve_intent_server_id(
        {"type": "propose_action", "target": "local"}
    ) == ""


def test_resolve_intent_server_id_plan_consistent():
    assert _resolve_intent_server_id(
        {
            "type": "propose_plan",
            "steps": [
                {"target": "web-01"},
                {"target": "web-01"},
            ],
        }
    ) == "web-01"


def test_resolve_intent_server_id_plan_mixed_returns_empty():
    assert _resolve_intent_server_id(
        {
            "type": "propose_plan",
            "steps": [
                {"target": "web-01"},
                {"target": "db-02"},
            ],
        }
    ) == ""


def test_resolve_intent_server_id_plan_local_only_returns_empty():
    assert _resolve_intent_server_id(
        {
            "type": "propose_plan",
            "steps": [
                {"target": "local"},
                {"target": "local"},
            ],
        }
    ) == ""
