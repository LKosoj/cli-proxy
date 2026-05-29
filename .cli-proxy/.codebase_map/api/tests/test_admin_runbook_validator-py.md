# API Spec: `tests/test_admin_runbook_validator.py`

Generated: 2026-04-27T22:43:22Z

## Symbols
- `def test_validate_ok_for_freshly_built(tmp_path)` (line 25)
- `def test_validate_fails_on_checksum_mismatch(tmp_path)` (line 36)
- `def test_validate_fails_on_missing_file(tmp_path)` (line 45)
- `def test_validate_fails_on_bad_bash_syntax(tmp_path)` (line 54)
- `def test_validate_reports_unknown_rb_id(tmp_path)` (line 62)
- `def test_validate_rejects_symlink_in_scripts_dir(tmp_path)` (line 68)
- `def test_validate_warns_when_checksum_missing(tmp_path)` (line 81)
