# API Spec: `tests/test_admin_analyzer.py`

Generated: 2026-04-27T22:43:22Z

## Symbols
- `def test_admin_analyzer_returns_valid_contract_for_valid_json()` (line 91)
- `def test_admin_analyzer_returns_notify_admin_on_invalid_json()` (line 108)
- `def test_admin_analyzer_returns_notify_admin_on_schema_validation_error()` (line 117)
- `def test_admin_shared_analyzer_schema_contains_optional_secondary_cli_command()` (line 133)
- `def test_admin_analyzer_source_removes_service_specific_hardcoded_strings()` (line 142)
- `def test_admin_analyzer_service_specific_php_snapshot_now_uses_llm_fallback()` (line 148)
- `def test_admin_analyzer_preserves_optional_secondary_cli_command_from_llm()` (line 171)
- `def test_admin_analyzer_generates_cli_step_for_low_confidence_service_case()` (line 189)
- `def test_admin_analyzer_service_specific_db_snapshot_now_uses_llm_fallback()` (line 224)
- `def test_admin_analyzer_rule_engine_detects_disk_high_and_recommends_cleanup()` (line 248)
- `def test_admin_analyzer_rule_engine_cpu_high_without_root_cause_notifies_only()` (line 263)
- `def test_admin_analyzer_rule_engine_detects_ssl_expiry_warning_and_critical()` (line 278)
- `def test_admin_analyzer_removed_service_rules_delegate_to_llm_parser(monkeypatch)` (line 299)
- `def test_admin_analyzer_cli_post_step_refines_low_confidence_notify_admin()` (line 328)
- `def test_admin_analyzer_cli_post_step_does_not_override_non_low_primary_decision()` (line 349)
- `def test_admin_analyzer_cli_post_step_ignores_invalid_cli_output()` (line 370)
- `def test_admin_analyzer_cli_feedback_envelope_finalizes_low_confidence_notify_admin()` (line 384)
- `def test_admin_analyzer_isolated_between_sequential_intents()` (line 411)
- `def test_admin_analyzer_loads_prompts_yaml_and_builds_fallback_prompt(tmp_path)` (line 438)
- `def test_admin_analyzer_analyze_uses_loaded_prompt_for_llm_path(tmp_path)` (line 464)
- `def test_admin_analyzer_loads_real_prompts_file()` (line 493)
- `def test_admin_analyzer_real_prompt_explains_secondary_cli_policy()` (line 508)
