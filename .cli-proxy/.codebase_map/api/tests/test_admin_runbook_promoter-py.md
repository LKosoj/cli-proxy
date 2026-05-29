# API Spec: `tests/test_admin_runbook_promoter.py`

Generated: 2026-04-27T22:43:22Z

## Symbols
- `def test_promote_adds_prod_server(tmp_path)` (line 36)
- `def test_promote_deduplicates_when_already_present(tmp_path)` (line 47)
- `def test_promote_raises_confidence(tmp_path)` (line 57)
- `def test_promote_rejects_out_of_range_confidence(tmp_path)` (line 68)
- `def test_promote_rejects_empty_add_servers(tmp_path)` (line 76)
- `def test_promote_rejects_unknown_rb(tmp_path)` (line 82)
- `def test_promote_rejects_invalid_rb_id(tmp_path)` (line 87)
- `def test_promote_fails_when_validation_fails(tmp_path)` (line 92)
- `def test_promote_skips_validation_when_disabled(tmp_path)` (line 104)
- `def test_promote_preserves_body_and_other_fields(tmp_path)` (line 117)
- `def test_promote_makes_runbook_match_prod_after_promote(tmp_path)` (line 130)
- `def test_promote_writes_audit_note_to_added_servers(tmp_path)` (line 140)
- `def test_promote_normalizes_server_ids(tmp_path)` (line 149)
- `def test_promote_handles_utf8_bom_in_runbook_file(tmp_path)` (line 158)
