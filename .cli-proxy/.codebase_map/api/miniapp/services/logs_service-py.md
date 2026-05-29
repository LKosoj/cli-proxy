# API Spec: `miniapp/services/logs_service.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class LogsServiceError(Exception)` (line 13)

### `class LogTypeError(LogsServiceError)` (line 17)

### `class LogAccessDeniedError(LogsServiceError)` (line 21)

### `class ParsedLogEntry` (line 26)
- `def to_payload()` (line 36)

### `class LogEntryAccumulator` (line 54)
- `def __init__()` (line 65)
- `def feed_line(line)` (line 119)
- `def flush_stale()` (line 137)
- `def flush_all()` (line 149)

### `class LogsService` (line 157)
- `def history_options()` (line 163)
- `def list_log_types()` (line 169)
- `def resolve_log_path(log_type)` (line 189)
- `def file_end_position(log_type)` (line 197)
- `def list_session_filters()` (line 204)
- `def allowed_session_uids()` (line 255)
- `def allowed_session_pairs()` (line 278)
- `def ensure_session_scope_allowed()` (line 338)
- `def entry_allowed(entry)` (line 414)
- `def parse_lines(lines)` (line 439)
- `def read_history()` (line 449)
