# API Spec: `app/security/audit.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class AuditStoreError(RuntimeError)` (line 18)
*Raised when persistent audit storage cannot be initialized.*

### `class EventBusAuditService` (line 22)
- `def __init__()` (line 25)
- `async def emit(record)` (line 34)
- `def list_records()` (line 62)

### `class SqliteAuditLogStore` (line 75)
- `def __init__()` (line 78)
- `def ensure_schema()` (line 132)
- `def append(record)` (line 165)
- `def list_records()` (line 192)

### `class PersistentAuditService` (line 257)
- `def __init__()` (line 258)
- `async def emit(record)` (line 269)
- `def list_records()` (line 276)

## Symbols
- `def build_audit_service(audit_config)` (line 294)
