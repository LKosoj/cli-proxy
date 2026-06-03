# API Spec: `tests/test_admin_runbook_builder.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_build_writes_runbook_and_scripts(tmp_path)` (line 32)
- `def test_build_tags_include_script_by_default(tmp_path)` (line 53)
- `def test_build_rejects_empty_title(tmp_path)` (line 59)
- `def test_build_rejects_empty_scripts(tmp_path)` (line 64)
- `def test_build_rejects_bad_script_name(tmp_path)` (line 72)
- `def test_build_rejects_non_sh_extension(tmp_path)` (line 80)
- `def test_build_rejects_empty_body(tmp_path)` (line 88)
- `def test_build_rejects_bad_target(tmp_path)` (line 96)
- `def test_build_rejects_duplicate_script_names(tmp_path)` (line 104)
- `def test_build_rejects_existing_rb_id_without_force(tmp_path)` (line 115)
- `def test_build_overwrites_with_force(tmp_path)` (line 121)
- `def test_build_force_cleans_orphaned_scripts(tmp_path)` (line 133)
- `def test_build_generated_rb_ids_unique(tmp_path)` (line 160)
- `def test_build_matches_on_dev_server(tmp_path)` (line 166)
- `def test_build_multi_step(tmp_path)` (line 175)
- `def test_build_writes_audit_note_to_dev_server(tmp_path)` (line 195)
- `def test_build_scripts_dir_mode_restricted(tmp_path)` (line 203)
