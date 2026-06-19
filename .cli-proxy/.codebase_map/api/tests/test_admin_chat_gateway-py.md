# API Spec: `tests/test_admin_chat_gateway.py`

Generated: 2026-06-17T10:46:18Z

## Symbols
- `async def test_answer_happy_path(tmp_path)` (line 66)
- `async def test_run_readonly_allowed(tmp_path)` (line 80)
- `async def test_run_readonly_rejects_non_readonly_action(tmp_path)` (line 97)
- `async def test_propose_action_produces_pending(tmp_path)` (line 113)
- `async def test_propose_new_action_denylist_blocks(tmp_path)` (line 131)
- `async def test_propose_new_action_custom_denylist_blocks(tmp_path)` (line 148)
- `async def test_invalid_json_reported(tmp_path)` (line 164)
- `async def test_out_of_allowlist_action_id(tmp_path)` (line 171)
- `async def test_update_memory_writes_md(tmp_path)` (line 188)
- `async def test_ask_clarification_stores_options(tmp_path)` (line 203)
- `async def test_prompt_injection_in_user_text_does_not_leak_into_system(tmp_path)` (line 218)
- `async def test_history_in_user_prompt(tmp_path)` (line 233)
- `async def test_llm_unavailable_returns_error_decision(tmp_path)` (line 249)
