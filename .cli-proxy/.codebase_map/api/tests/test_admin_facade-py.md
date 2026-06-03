# API Spec: `tests/test_admin_facade.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_parse_server_specs_basic()` (line 29)
- `def test_parse_server_specs_unknown_transport_falls_back_to_local()` (line 51)
- `def test_list_servers_empty_when_no_config(tmp_path)` (line 56)
- `def test_list_servers_returns_summaries(tmp_path)` (line 61)
- `def test_server_summary_reflects_baseline_and_drifts(tmp_path)` (line 72)
- `def test_server_summary_for_unknown_returns_none(tmp_path)` (line 86)
- `def test_orphan_server_on_disk_is_surfaced(tmp_path)` (line 92)
- `def test_get_baseline_and_accept_discard_cycle(tmp_path)` (line 101)
- `def test_memory_facts_and_notes_through_facade(tmp_path)` (line 119)
- `def test_compact_memory_through_facade(tmp_path)` (line 131)
- `def test_drift_list_and_ack_through_facade(tmp_path)` (line 141)
- `def test_runbooks_list_through_facade(tmp_path)` (line 153)
- `def test_rescan_server_with_custom_reconciler(tmp_path)` (line 170)
- `def test_global_summary_aggregates_statuses(tmp_path)` (line 192)
- `def test_workdir_is_required(tmp_path)` (line 205)
