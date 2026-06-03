# API Spec: `app/security/facade.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class SecurityFacade` (line 30)
*Unified entrypoint for auth, validation, audit and rate limits.*
- `def __init__()` (line 33)
- `def from_config(cls, auth_config)` (line 50)
- `def from_app_config(cls, config)` (line 97)
- `def authorize(chat_id)` (line 186)
- `async def authorize_mode_launch(chat_id)` (line 189)
- `def authenticate(credentials)` (line 262)
- `def require_text(value)` (line 270)
- `def resolve_path(root, rel_path)` (line 279)
- `def consume_rate_limit(scope, subject)` (line 296)
- `async def emit_audit()` (line 317)
- `def list_audit_logs()` (line 347)
