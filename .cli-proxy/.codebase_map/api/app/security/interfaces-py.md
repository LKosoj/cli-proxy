# API Spec: `app/security/interfaces.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class AuthDecision` (line 9)

### `class AuthenticationResult` (line 19)

### `class PathValidationResult` (line 28)

### `class RateLimitDecision` (line 36)

### `class AuditRecord` (line 51)
- `def to_payload()` (line 63)

### `class AuthService(Protocol)` (line 78)
- `def authorize(chat_id)` (line 79)
- `def authenticate(credentials)` (line 82)

### `class AuthenticationStrategy(Protocol)` (line 91)
- `def authenticate(credentials)` (line 94)

### `class ValidatorService(Protocol)` (line 98)
- `def require_text(value)` (line 99)
- `def resolve_path(root, rel_path)` (line 108)

### `class AuditService(Protocol)` (line 124)
- `async def emit(record)` (line 125)
- `def list_records()` (line 128)

### `class RateLimitService(Protocol)` (line 140)
- `def consume(scope, subject)` (line 141)
