# API Spec: `app/security/facade.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class SecurityFacade` (line 26)
*Unified entrypoint for auth, validation, audit and rate limits.*
- `def __init__()` (line 29)
- `def from_config(cls, auth_config)` (line 46)
- `def for_bot_app(cls, bot_app)` (line 93)
- `def authorize(chat_id)` (line 144)
- `async def authorize_mode_launch(chat_id)` (line 147)
- `def authenticate(credentials)` (line 220)
- `def require_text(value)` (line 228)
- `def resolve_path(root, rel_path)` (line 237)
- `def consume_rate_limit(scope, subject)` (line 254)
- `async def emit_audit()` (line 275)
- `def list_audit_logs()` (line 305)
