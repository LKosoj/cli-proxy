# API Spec: `app/services/access_policy_service.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class AccessPolicyService` (line 15)
- `def __init__(bot_app)` (line 31)
- `def authorize(chat_id)` (line 34)
- `def is_allowed(chat_id)` (line 51)
- `def is_admin(chat_id)` (line 54)
- `def is_user(chat_id)` (line 57)
- `def is_whitelisted(chat_id)` (line 61)
- `async def ensure_allowed(chat_id, context)` (line 64)
- `def admin_denied_text(scope)` (line 77)
- `async def require_admin(chat_id, context)` (line 81)
- `def can_input_project_path(chat_id)` (line 87)
- `def callback_admin_scope(chat_id, data)` (line 92)
- `async def require_scope_session(chat_id, context)` (line 112)
- `def user_modes(chat_id)` (line 144)
- `def allowed_mode_ids_for_chat(chat_id)` (line 155)
- `def is_mode_allowed_for_chat(chat_id, mode_id)` (line 175)
- `def is_direct_cli_allowed_for_chat(chat_id)` (line 181)
- `def is_orchestrator_allowed_for_chat(chat_id)` (line 184)
- `def default_mode_id_for_chat(chat_id)` (line 187)
- `def apply_default_mode_for_session(session)` (line 206)
