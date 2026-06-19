# API Spec: `app/security/errors.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class DenyReasonCode` (line 6)

### `class SecurityError(Exception)` (line 90)
- `def __init__(code, message)` (line 93)
- `def to_payload()` (line 106)
- `def to_dict()` (line 115)

### `class SecurityAuthenticationError(SecurityError, PermissionError)` (line 119)

### `class SecurityAuthorizationError(SecurityError, PermissionError)` (line 123)

### `class SecurityValidationError(SecurityError, ValueError)` (line 127)

### `class SecurityRateLimitError(SecurityError, RuntimeError)` (line 131)

## Symbols
- `def normalize_deny_reason(code)` (line 81)
- `def get_user_facing_error_text(code)` (line 86)
- `def serialize_security_error(error)` (line 135)
