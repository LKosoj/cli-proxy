# API Spec: `tests/test_admin_prereqs.py`

Generated: 2026-04-27T22:43:22Z

## Symbols
- `def test_manifest_required_has_bash_awk_ss()` (line 20)
- `def test_manifest_each_tool_has_pkg_per_pm()` (line 25)
- `def test_prereqs_command_covers_all_tools()` (line 35)
- `def test_prereqs_command_with_subset()` (line 41)
- `def test_prereqs_command_sanitizes_dangerous_chars()` (line 48)
- `def test_prereqs_command_empty_returns_true()` (line 54)
- `def test_parse_prereqs_output_basic()` (line 58)
- `def test_parse_prereqs_output_ignores_garbage()` (line 64)
- `def test_evaluate_all_present_is_ok()` (line 73)
- `def test_evaluate_required_missing_blocks_ok()` (line 82)
- `def test_evaluate_id_like_fallback()` (line 91)
- `def test_evaluate_unknown_distro_yields_no_installable()` (line 97)
- `def test_generate_bootstrap_apt()` (line 106)
- `def test_generate_bootstrap_dnf()` (line 117)
- `def test_generate_bootstrap_apk()` (line 126)
- `def test_generate_bootstrap_pacman()` (line 134)
- `def test_generate_bootstrap_nothing_to_install()` (line 142)
- `def test_generate_bootstrap_unknown_pm_emits_fallback()` (line 149)
- `def test_facade_check_server_prereqs_reads_baseline(tmp_path)` (line 189)
- `def test_facade_check_server_prereqs_missing_baseline_returns_all_missing(tmp_path)` (line 203)
- `def test_facade_generate_bootstrap_runbook_creates_manual_runbook(tmp_path)` (line 212)
- `def test_facade_generate_bootstrap_runbook_no_missing(tmp_path)` (line 230)
- `def test_facade_generate_bootstrap_runbook_writes_prereqs_audit_note(tmp_path)` (line 241)
- `def test_facade_generate_bootstrap_runbook_force_rebuild(tmp_path)` (line 261)
