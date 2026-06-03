# API Spec: `tests/test_admin_memory.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_layout_paths(tmp_path)` (line 17)
- `def test_get_facts_empty(tmp_path)` (line 23)
- `def test_update_fact_persists_and_records_meta(tmp_path)` (line 28)
- `def test_update_fact_tracks_previous_value(tmp_path)` (line 40)
- `def test_delete_fact_removes_key(tmp_path)` (line 49)
- `def test_reserved_meta_key_rejected(tmp_path)` (line 60)
- `def test_empty_key_rejected(tmp_path)` (line 66)
- `def test_append_note_and_read_back(tmp_path)` (line 72)
- `def test_iter_note_entries_parses_multiple_blocks(tmp_path)` (line 82)
- `def test_append_note_rejects_empty(tmp_path)` (line 94)
- `def test_notes_stats_counts_entries(tmp_path)` (line 100)
- `def test_should_compact_threshold(tmp_path)` (line 109)
- `def test_compact_notes_noop_when_below_threshold(tmp_path)` (line 117)
- `def test_compact_notes_collapses_head_with_naive_summarizer(tmp_path)` (line 124)
- `def test_compact_notes_uses_llm_summarizer(tmp_path)` (line 140)
- `def test_compact_notes_llm_failure_fallback(tmp_path)` (line 154)
- `def test_compact_force_even_below_threshold(tmp_path)` (line 167)
- `def test_two_servers_memory_isolated(tmp_path)` (line 175)
- `def test_default_threshold_is_sensible()` (line 189)
