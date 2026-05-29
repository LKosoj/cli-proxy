# API Spec: `tests/test_admin_plugin_tools.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class _FakeSession` (line 19)
- `def __init__(workdir)` (line 20)

### `class _FakeLocalResult` (line 24)
- `def __init__()` (line 25)

### `class _FakeLocalTransport` (line 32)
- `def __init__(result)` (line 33)
- `async def run(spec)` (line 38)

### `class _FakeStateStore` (line 45)
- `def __init__()` (line 46)
- `def record_incident()` (line 49)

## Symbols
- `def test_resolve_workdir_from_session(tmp_path)` (line 78)
- `def test_resolve_workdir_from_cwd_fallback(tmp_path)` (line 83)
- `def test_resolve_workdir_missing_raises()` (line 88)
- `def test_find_allowlisted_action_matches_local()` (line 93)
- `def test_find_allowlisted_action_rejects_missing()` (line 101)
- `def test_find_allowlisted_action_invalid_hint()` (line 106)
- `def test_find_allowlisted_action_respects_hint()` (line 111)
- `def test_run_allowlisted_action_dry_run_returns_preview(tmp_path)` (line 124)
- `def test_run_allowlisted_action_executes_local_success(tmp_path)` (line 147)
- `def test_run_allowlisted_action_failure_returns_error(tmp_path)` (line 170)
- `def test_run_allowlisted_action_non_zero_exit_is_failure(tmp_path)` (line 191)
- `def test_run_allowlisted_action_rejects_unknown_action(tmp_path)` (line 210)
- `def test_run_allowlisted_action_ssh_requires_host_key(tmp_path)` (line 225)
- `def test_write_escalation_records_incident_and_note(tmp_path)` (line 243)
- `def test_write_escalation_rejects_empty_reason(tmp_path)` (line 260)
- `def test_write_escalation_rejects_invalid_urgency(tmp_path)` (line 268)
- `def test_build_server_dossier_collects_sections(tmp_path)` (line 276)
- `def test_action_run_result_to_dict_roundtrip()` (line 303)
