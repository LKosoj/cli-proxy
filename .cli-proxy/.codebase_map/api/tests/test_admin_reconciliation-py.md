# API Spec: `tests/test_admin_reconciliation.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _FakeResult` (line 21)
- `def __init__(stdout)` (line 22)

### `class _Transport` (line 30)
- `def __init__(responses)` (line 31)
- `async def run(spec)` (line 34)

## Symbols
- `def test_first_reconcile_creates_baseline_and_no_drifts(tmp_path)` (line 55)
- `def test_second_reconcile_detects_drifts_and_persists(tmp_path)` (line 76)
- `def test_alarm_hook_invoked_on_alarm(tmp_path)` (line 109)
- `def test_tick_aggregates_reports_for_multiple_servers(tmp_path)` (line 135)
- `def test_tick_keeps_going_on_single_server_failure(tmp_path)` (line 151)
- `def test_daily_maintenance_cleans_and_compacts(tmp_path)` (line 171)
- `def test_reconciler_with_no_servers_is_noop(tmp_path)` (line 199)
