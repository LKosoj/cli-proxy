# API Spec: `tests/test_admin_executor.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class _FakeLocalTransport` (line 52)
- `def __init__()` (line 53)
- `async def run(spec)` (line 56)

### `class _NeverSSHTransport` (line 68)
- `async def run(_spec)` (line 69)

### `class _CountingLocalTransport` (line 73)
- `def __init__()` (line 74)
- `async def run(spec)` (line 77)

## Symbols
- `def test_admin_executor_applies_notify_admin_decision_and_logs_action(tmp_path)` (line 89)
- `def test_admin_executor_no_action_skips_action_and_incident_logs(tmp_path)` (line 121)
- `def test_admin_executor_persists_incident_and_alert_state_with_sla_metric(tmp_path)` (line 148)
- `def test_admin_executor_applies_restart_decision_via_transport_and_logs_action(tmp_path)` (line 198)
- `def test_admin_executor_blocks_risky_action_when_user_rejects_confirmation(tmp_path)` (line 252)
- `def test_admin_executor_requests_confirmation_for_low_confidence_decision(tmp_path)` (line 299)
- `def test_admin_executor_low_confidence_without_ask_user_uses_safe_default(tmp_path)` (line 341)
- `def test_admin_executor_requests_confirmation_for_signal_and_policy_conflicts(tmp_path)` (line 373)
- `def test_admin_executor_persists_override_and_skips_repeated_ask_user(tmp_path)` (line 413)
- `def test_admin_executor_override_hash_depends_on_action_parameters(tmp_path)` (line 479)
- `def test_admin_executor_analyzer_decisions_isolated_between_sequential_runs(tmp_path)` (line 552)
- `def test_admin_executor_dry_run_blocks_execution_and_logs_intent(tmp_path)` (line 605)
- `def test_admin_executor_rejects_action_in_cooldown(tmp_path)` (line 648)
- `def test_admin_executor_rejects_action_when_rate_limit_exceeded(tmp_path)` (line 698)
- `def test_admin_executor_requires_notify_before_restart_postgresql(tmp_path)` (line 759)
- `def test_admin_executor_rejects_action_outside_maintenance_window(tmp_path)` (line 818)
- `def test_admin_executor_does_not_duplicate_allowlist_validation(tmp_path)` (line 861)
