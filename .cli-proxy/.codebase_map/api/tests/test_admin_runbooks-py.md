# API Spec: `tests/test_admin_runbooks.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_load_runbooks_empty_returns_empty(tmp_path)` (line 42)
- `def test_load_global_runbook_with_full_frontmatter(tmp_path)` (line 46)
- `def test_load_runbook_without_frontmatter_is_skipped(tmp_path)` (line 69)
- `def test_load_server_scoped_runbook(tmp_path)` (line 75)
- `def test_match_runbooks_scores_server_glob(tmp_path)` (line 87)
- `def test_match_runbooks_scores_tags(tmp_path)` (line 95)
- `def test_match_runbooks_combines_server_and_tags(tmp_path)` (line 103)
- `def test_match_runbooks_includes_general_purpose_runbook_with_low_score(tmp_path)` (line 112)
- `def test_match_runbooks_prefers_server_scoped_with_owner_match(tmp_path)` (line 121)
- `def test_duplicate_id_is_deduplicated(tmp_path)` (line 129)
- `def test_summarize_runbooks_respects_limit(tmp_path)` (line 136)
- `def test_unmatched_returns_empty_when_no_generic_runbooks(tmp_path)` (line 147)
