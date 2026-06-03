# API Spec: `tests/test_admin_mode_lifecycle.py`

Generated: 2026-06-03T02:24:28Z

## Symbols
- `def test_admin_on_enable_activates_runtime_and_marks_session_enabled(tmp_path)` (line 70)
- `def test_admin_on_enable_is_idempotent(tmp_path)` (line 102)
- `def test_admin_on_disable_cancels_runtime_and_marks_session_disabled(tmp_path)` (line 134)
- `def test_admin_on_disable_without_prior_enable_is_safe(tmp_path)` (line 166)
- `def test_admin_on_enable_without_chat_id_returns_none(tmp_path)` (line 192)
- `def test_admin_on_enable_then_disable_then_enable_idempotent(tmp_path)` (line 216)
