# API Spec: `app/security/rate_limits.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class RateLimitStoreError(RuntimeError)` (line 18)
*Raised when rate limit storage cannot be initialized.*

### `class RateLimitPolicy` (line 23)

### `class InMemoryRateLimitService` (line 30)
- `def __init__()` (line 31)
- `def consume(scope, subject)` (line 44)

### `class SqliteSlidingWindowRateLimitService` (line 91)
- `def __init__()` (line 94)
- `def ensure_schema()` (line 141)
- `def consume(scope, subject)` (line 162)

## Symbols
- `def build_rate_limit_service(rate_limit_config)` (line 394)
