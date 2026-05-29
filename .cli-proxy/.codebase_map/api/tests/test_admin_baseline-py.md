# API Spec: `tests/test_admin_baseline.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _FakeResult` (line 23)
- `def __init__(stdout)` (line 24)

### `class _FakeLocalTransport` (line 32)
- `def __init__(responses)` (line 33)
- `async def run(spec)` (line 37)

## Symbols
- `def test_scanner_runs_checks_and_returns_profile(tmp_path)` (line 57)
- `def test_scanner_records_check_error_in_profile()` (line 75)
- `def test_scanner_timed_out_becomes_error()` (line 89)
- `def test_scanner_records_unknown_transport_per_check_error()` (line 101)
- `def test_scanner_ssh_requires_host_and_key()` (line 111)
- `def test_apply_scan_result_first_time_creates_baseline(tmp_path)` (line 123)
- `def test_apply_scan_result_second_time_goes_to_proposed(tmp_path)` (line 132)
- `def test_accept_proposed_baseline_moves_files(tmp_path)` (line 140)
- `def test_accept_proposed_fails_when_no_proposed(tmp_path)` (line 152)
- `def test_discard_proposed_baseline(tmp_path)` (line 158)
- `def test_load_baseline_missing_returns_none(tmp_path)` (line 166)
- `def test_load_baseline_rejects_non_mapping(tmp_path)` (line 171)
- `def test_baseline_paths_are_isolated_per_server(tmp_path)` (line 179)
