# API Spec: `tests/test_admin_monitor.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _FakeLocalTransport` (line 53)
- `def __init__()` (line 54)
- `async def run(spec)` (line 57)

### `class _FakeSSHTransport` (line 81)
- `def __init__()` (line 82)
- `async def run(spec)` (line 85)

## Symbols
- `def test_admin_monitor_collects_snapshot_for_multiple_servers(tmp_path)` (line 120)
- `def test_admin_monitor_continues_when_single_server_fails_or_times_out(tmp_path)` (line 164)
- `def test_admin_monitor_snapshots_isolated_between_sequential_runs(tmp_path)` (line 224)
- `def test_admin_monitor_resolves_ssh_host_from_ssh_yaml(tmp_path)` (line 262)
- `def test_admin_monitor_ssh_resolver_raises_when_alias_missing(tmp_path)` (line 299)
