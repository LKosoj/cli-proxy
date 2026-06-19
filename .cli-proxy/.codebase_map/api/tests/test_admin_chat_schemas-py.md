# API Spec: `tests/test_admin_chat_schemas.py`

Generated: 2026-06-17T10:46:18Z

## Symbols
- `def test_parse_answer_requires_text()` (line 56)
- `def test_parse_unknown_type_rejected()` (line 63)
- `def test_run_readonly_happy_path()` (line 68)
- `def test_actions_mapping_uses_mapping_key_as_action_id_when_missing_field()` (line 81)
- `def test_run_readonly_rejects_not_read_only_action()` (line 109)
- `def test_run_readonly_rejects_unknown_alias()` (line 121)
- `def test_propose_action_accepts_known_action()` (line 133)
- `def test_propose_action_rejects_unknown_action_id()` (line 147)
- `def test_propose_new_action_requires_risk_and_argv()` (line 160)
- `def test_propose_new_action_denylist_rm_rf_slash()` (line 177)
- `def test_propose_new_action_denylist_rm_force_recursive_root_variants(argv)` (line 199)
- `def test_propose_new_action_denylist_custom_pattern()` (line 213)
- `def test_propose_plan_mixed_steps()` (line 231)
- `def test_propose_plan_rejects_step_without_argv_or_action()` (line 251)
- `def test_update_memory_requires_append_text()` (line 259)
- `def test_ask_clarification_options_parsed()` (line 266)
