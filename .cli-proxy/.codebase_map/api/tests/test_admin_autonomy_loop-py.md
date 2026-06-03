# API Spec: `tests/test_admin_autonomy_loop.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class _FakeResult` (line 29)
- `def to_dict()` (line 44)

### `class _RunnerRecorder` (line 62)
- `def __init__()` (line 63)
- `async def __call__()` (line 69)

## Symbols
- `def test_rule_based_picks_runbook_with_auto_action(tmp_path)` (line 128)
- `def test_rule_based_ignores_low_confidence_auto_action(tmp_path)` (line 148)
- `def test_rule_based_escalates_on_warn_without_runbook()` (line 163)
- `def test_rule_based_ignores_on_info_without_runbook()` (line 170)
- `def test_loop_does_nothing_when_policy_disabled(tmp_path)` (line 182)
- `def test_loop_alarm_always_escalated_even_if_permitted(tmp_path)` (line 190)
- `def test_loop_gates_action_not_in_allowlist(tmp_path)` (line 217)
- `def test_loop_executes_when_everything_permits(tmp_path)` (line 241)
- `def test_loop_dry_run_failure_denies_real(tmp_path)` (line 269)
- `def test_loop_rate_limit_blocks(tmp_path)` (line 294)
- `def test_loop_cooldown_blocks(tmp_path)` (line 326)
- `def test_loop_audit_note_written_on_execute(tmp_path)` (line 353)
- `def test_baseline_auto_accept_counts_stable_scans(tmp_path)` (line 380)
- `def test_baseline_auto_accept_resets_on_drift(tmp_path)` (line 404)
- `def test_baseline_auto_accept_disabled_when_policy_off(tmp_path)` (line 424)
- `def test_facade_autonomy_tick_disabled_returns_shape(tmp_path)` (line 443)
