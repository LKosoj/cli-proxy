# API Spec: `tests/test_admin_monitor.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class _FakeLocalTransport` (line 53)
- `def __init__()` (line 54)
- `async def run(spec)` (line 57)

### `class _FakeSSHTransport` (line 81)
- `def __init__()` (line 82)
- `async def run(spec)` (line 85)

### `class _SlowCountingLocalTransport` (line 113)
- `def __init__()` (line 114)
- `async def run(spec)` (line 118)

## Symbols
- `def test_admin_monitor_collects_snapshot_for_multiple_servers(tmp_path)` (line 141)
- `def test_admin_monitor_continues_when_single_server_fails_or_times_out(tmp_path)` (line 185)
- `def test_admin_monitor_snapshots_isolated_between_sequential_runs(tmp_path)` (line 245)
- `def test_admin_monitor_limits_concurrent_polls(tmp_path)` (line 283)
- `def test_admin_monitor_resolves_ssh_host_from_ssh_yaml(tmp_path)` (line 317)
- `def test_admin_monitor_resolves_password_ssh_host_from_ssh_yaml(tmp_path)` (line 354)
- `def test_admin_monitor_ssh_resolver_raises_when_alias_missing(tmp_path)` (line 393)
