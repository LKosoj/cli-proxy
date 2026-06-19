# API Spec: `tests/test_admin_chat_service.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class _FakeLocalResult` (line 42)

### `class _FakeSshResult` (line 52)

### `class _FakeLocalTransport` (line 64)
- `def __init__(result)` (line 65)
- `async def run(spec)` (line 69)

### `class _FakeSshTransport` (line 81)
- `def __init__(result)` (line 82)
- `async def run(spec)` (line 86)

## Symbols
- `def test_list_messages_empty(tmp_path)` (line 167)
- `def test_list_messages_returns_appended_entries(tmp_path)` (line 172)
- `def test_list_pending_sorted(tmp_path)` (line 181)
- `def test_memory_md_read_and_save_roundtrip(tmp_path)` (line 191)
- `def test_counters_track_messages_and_pending(tmp_path)` (line 198)
- `def test_reject_pending_removes_file_and_logs_memory(tmp_path)` (line 209)
- `def test_reject_pending_missing(tmp_path)` (line 220)
- `def test_reject_pending_invalid_id(tmp_path)` (line 227)
- `async def test_send_answer(tmp_path)` (line 236)
- `async def test_send_propose_action_persists_pending(tmp_path)` (line 251)
- `async def test_send_empty_text_rejected(tmp_path)` (line 274)
- `async def test_execute_pending_propose_action_local(tmp_path)` (line 287)
- `async def test_execute_pending_propose_action_ssh(tmp_path)` (line 317)
- `async def test_execute_pending_propose_new_action_local(tmp_path)` (line 344)
- `async def test_execute_pending_propose_new_action_ssh(tmp_path)` (line 374)
- `async def test_execute_pending_not_found(tmp_path)` (line 406)
- `async def test_execute_pending_corrupt_intent(tmp_path)` (line 415)
- `async def test_execute_pending_propose_plan_empty_steps(tmp_path)` (line 426)
- `async def test_execute_pending_ssh_alias_unknown(tmp_path)` (line 440)
- `async def test_execute_pending_plan_all_steps_local(tmp_path)` (line 463)
- `async def test_execute_pending_plan_stops_on_error(tmp_path)` (line 494)
- `async def test_execute_pending_plan_continues_when_stop_on_error_false(tmp_path)` (line 524)
- `async def test_execute_pending_plan_ssh_step_uses_config(tmp_path)` (line 552)
- `async def test_execute_pending_plan_rejects_unknown_ssh_alias(tmp_path)` (line 575)
- `async def test_execute_pending_plan_saves_runbook_on_success(tmp_path)` (line 596)
- `async def test_execute_pending_plan_skips_runbook_on_failure(tmp_path)` (line 624)
- `async def test_execute_pending_plan_sanitizes_runbook_id(tmp_path)` (line 650)
- `async def test_send_autopilot_disabled_falls_back_to_pending(tmp_path)` (line 685)
- `async def test_send_autopilot_action_allowlisted_executes_without_pending(tmp_path)` (line 707)
- `async def test_send_autopilot_action_not_in_allowlist_blocked(tmp_path)` (line 736)
- `async def test_send_autopilot_adhoc_argv_allowlisted(tmp_path)` (line 763)
- `async def test_send_autopilot_adhoc_argv_blocked(tmp_path)` (line 789)
- `async def test_send_autopilot_plan_all_steps_pass(tmp_path)` (line 810)
- `async def test_send_autopilot_plan_blocked_when_any_step_not_allowlisted(tmp_path)` (line 841)
- `async def test_send_autopilot_per_server_override(tmp_path)` (line 869)
  - *per-server block разрешает action только на web-01; для local должно падать в pending.*
