# API Spec: `tests/test_admin_chat_autopilot_verdict.py`

Generated: 2026-06-17T10:46:18Z

## Symbols
- `def test_disabled_policy_blocks_all()` (line 14)
- `def test_propose_action_allowed_when_in_allowlist()` (line 23)
- `def test_propose_action_blocked_when_not_in_allowlist()` (line 32)
- `def test_propose_action_blocked_when_action_id_empty()` (line 41)
- `def test_propose_new_action_allowed_when_argv_head_in_allowlist()` (line 50)
- `def test_propose_new_action_blocked_when_argv_head_missing()` (line 58)
- `def test_propose_new_action_blocked_when_argv_empty()` (line 67)
- `def test_propose_plan_allowed_when_all_steps_pass()` (line 75)
- `def test_propose_plan_blocked_when_one_step_fails()` (line 93)
- `def test_propose_plan_blocked_when_empty()` (line 113)
- `def test_propose_plan_step_without_action_or_argv_blocked()` (line 122)
- `def test_unknown_intent_type_blocked()` (line 131)
- `def test_resolve_intent_server_id_single_target()` (line 140)
- `def test_resolve_intent_server_id_local_is_empty()` (line 146)
- `def test_resolve_intent_server_id_plan_consistent()` (line 152)
- `def test_resolve_intent_server_id_plan_mixed_returns_empty()` (line 164)
- `def test_resolve_intent_server_id_plan_local_only_returns_empty()` (line 176)
