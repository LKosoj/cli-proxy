# API Spec: `tests/test_admin_facade.py`

Generated: 2026-04-27T22:43:22Z

## Symbols
- `def test_parse_server_specs_basic()` (line 29)
- `def test_parse_server_specs_unknown_transport_falls_back_to_local()` (line 50)
- `def test_list_servers_empty_when_no_config(tmp_path)` (line 55)
- `def test_list_servers_returns_summaries(tmp_path)` (line 60)
- `def test_server_summary_reflects_baseline_and_drifts(tmp_path)` (line 71)
- `def test_server_summary_for_unknown_returns_none(tmp_path)` (line 85)
- `def test_orphan_server_on_disk_is_surfaced(tmp_path)` (line 91)
- `def test_get_baseline_and_accept_discard_cycle(tmp_path)` (line 100)
- `def test_memory_facts_and_notes_through_facade(tmp_path)` (line 118)
- `def test_compact_memory_through_facade(tmp_path)` (line 130)
- `def test_drift_list_and_ack_through_facade(tmp_path)` (line 140)
- `def test_runbooks_list_through_facade(tmp_path)` (line 152)
- `def test_rescan_server_with_custom_reconciler(tmp_path)` (line 169)
- `def test_global_summary_aggregates_statuses(tmp_path)` (line 191)
- `def test_workdir_is_required(tmp_path)` (line 204)
