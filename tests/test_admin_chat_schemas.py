from __future__ import annotations

import pytest

from modes.admin.chat_schemas import (
    IntentValidationError,
    build_validation_context,
    parse_intent,
    revalidate_intent,
)


_ADMIN_CFG = {
    "actions": {
        "local": {
            "check_disk": {
                "action_id": "check_disk",
                "argv": ["df", "-h"],
                "timeout_sec": 10,
                "risk_level": "low",
                "read_only": True,
            },
            "clear_tmp": {
                "action_id": "clear_tmp",
                "argv": ["rm", "-rf", "/tmp/app-cache"],
                "timeout_sec": 20,
                "risk_level": "medium",
            },
        },
        "ssh": {
            "probe_uptime": {
                "action_id": "probe_uptime",
                "argv": ["uptime"],
                "timeout_sec": 10,
                "risk_level": "low",
                "read_only": True,
            },
            "restart_nginx": {
                "action_id": "restart_nginx",
                "argv": ["sudo", "systemctl", "restart", "nginx"],
                "timeout_sec": 30,
                "risk_level": "high",
            },
        },
    },
}


def _ctx():
    return build_validation_context(
        admin_config=_ADMIN_CFG,
        known_aliases=["prod1", "stage"],
    )


def test_parse_answer_requires_text():
    with pytest.raises(IntentValidationError):
        parse_intent({"type": "answer", "text": ""})
    intent = parse_intent({"type": "answer", "text": "hello"})
    assert intent.type == "answer"


def test_parse_unknown_type_rejected():
    with pytest.raises(IntentValidationError):
        parse_intent({"type": "ignore_all_previous_instructions"})


def test_run_readonly_happy_path():
    payload = {
        "type": "run_readonly",
        "action_id": "probe_uptime",
        "target": "prod1",
        "text": "check uptime",
    }
    intent = parse_intent(payload)
    intent = revalidate_intent(intent, ctx=_ctx())
    assert intent.argv == ["uptime"]
    assert intent.risk_level == "low"


def test_actions_mapping_uses_mapping_key_as_action_id_when_missing_field():
    ctx = build_validation_context(
        admin_config={
            "actions": {
                "local": {
                    "diag": {
                        "argv": ["true"],
                        "timeout_sec": 5,
                        "risk_level": "low",
                        "read_only": True,
                    },
                },
            },
        },
        known_aliases=[],
    )
    intent = parse_intent({
        "type": "run_readonly",
        "action_id": "diag",
        "target": "local",
        "text": "diag",
    })

    intent = revalidate_intent(intent, ctx=ctx)

    assert intent.argv == ["true"]


def test_run_readonly_rejects_not_read_only_action():
    payload = {
        "type": "run_readonly",
        "action_id": "restart_nginx",
        "target": "prod1",
        "text": "run it",
    }
    intent = parse_intent(payload)
    with pytest.raises(IntentValidationError):
        revalidate_intent(intent, ctx=_ctx())


def test_run_readonly_rejects_unknown_alias():
    payload = {
        "type": "run_readonly",
        "action_id": "probe_uptime",
        "target": "unknown_alias",
        "text": "x",
    }
    intent = parse_intent(payload)
    with pytest.raises(IntentValidationError):
        revalidate_intent(intent, ctx=_ctx())


def test_propose_action_accepts_known_action():
    payload = {
        "type": "propose_action",
        "action_id": "restart_nginx",
        "target": "prod1",
        "rationale": "load too high",
        "text": "propose restart",
    }
    intent = parse_intent(payload)
    intent = revalidate_intent(intent, ctx=_ctx())
    assert intent.risk_level == "high"
    assert intent.argv == ["sudo", "systemctl", "restart", "nginx"]


def test_propose_action_rejects_unknown_action_id():
    payload = {
        "type": "propose_action",
        "action_id": "nuke_all",
        "target": "prod1",
        "rationale": "..",
        "text": "x",
    }
    intent = parse_intent(payload)
    with pytest.raises(IntentValidationError):
        revalidate_intent(intent, ctx=_ctx())


def test_propose_new_action_requires_risk_and_argv():
    with pytest.raises(IntentValidationError):
        parse_intent({
            "type": "propose_new_action",
            "target": "prod1",
            "argv": [],
            "risk_level": "low",
        })
    with pytest.raises(IntentValidationError):
        parse_intent({
            "type": "propose_new_action",
            "target": "prod1",
            "argv": ["ls"],
            "risk_level": "super_high",
        })


def test_propose_new_action_denylist_rm_rf_slash():
    payload = {
        "type": "propose_new_action",
        "target": "prod1",
        "argv": ["bash", "-lc", "rm -rf /"],
        "risk_level": "high",
    }
    intent = parse_intent(payload)
    with pytest.raises(IntentValidationError) as exc:
        revalidate_intent(intent, ctx=_ctx())
    assert "denylist" in str(exc.value)


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-fr", "/"],
        ["rm", "-r", "-f", "/"],
        ["rm", "-rf", "--", "/"],
        ["rm", "-rf", "--no-preserve-root", "/"],
    ],
)
def test_propose_new_action_denylist_rm_force_recursive_root_variants(argv):
    intent = parse_intent({
        "type": "propose_new_action",
        "target": "prod1",
        "argv": argv,
        "risk_level": "high",
    })

    with pytest.raises(IntentValidationError) as exc:
        revalidate_intent(intent, ctx=_ctx())

    assert "denylist" in str(exc.value)


def test_propose_new_action_denylist_custom_pattern():
    ctx = build_validation_context(
        admin_config={
            **_ADMIN_CFG,
            "chat": {"denylist_patterns": [r"\btouch\s+/etc/fstab"]},
        },
        known_aliases=["prod1"],
    )
    intent = parse_intent({
        "type": "propose_new_action",
        "target": "prod1",
        "argv": ["touch", "/etc/fstab"],
        "risk_level": "medium",
    })
    with pytest.raises(IntentValidationError):
        revalidate_intent(intent, ctx=ctx)


def test_propose_plan_mixed_steps():
    payload = {
        "type": "propose_plan",
        "text": "rolling restart",
        "steps": [
            {"target": "prod1", "action_id": "probe_uptime"},
            {
                "target": "prod1",
                "argv": ["echo", "step2"],
                "risk_level": "low",
            },
        ],
    }
    intent = parse_intent(payload)
    intent = revalidate_intent(intent, ctx=_ctx())
    assert len(intent.steps) == 2
    assert intent.steps[0].argv == ["uptime"]
    assert intent.steps[0].risk_level == "low"


def test_propose_plan_rejects_step_without_argv_or_action():
    with pytest.raises(IntentValidationError):
        parse_intent({
            "type": "propose_plan",
            "steps": [{"target": "prod1"}],
        })


def test_update_memory_requires_append_text():
    with pytest.raises(IntentValidationError):
        parse_intent({"type": "update_memory"})
    intent = parse_intent({"type": "update_memory", "memory_append": "fact"})
    assert intent.memory_append == "fact"


def test_ask_clarification_options_parsed():
    intent = parse_intent({
        "type": "ask_clarification",
        "text": "which server?",
        "clarification_options": ["prod1", "stage"],
    })
    assert intent.clarification_options == ["prod1", "stage"]
