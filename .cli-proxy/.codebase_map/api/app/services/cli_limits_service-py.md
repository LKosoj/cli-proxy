# API Spec: `app/services/cli_limits_service.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class CliProjectRef` (line 30)

### `class CliLimitsSnapshot` (line 37)

### `class CliLimitsService` (line 44)
*Собирает доступные лимиты и usage по активным CLI-сессиям.*
- `def __init__()` (line 57)
- `def set_gemini_oauth_client_secret(value)` (line 78)
- `async def describe_for_sessions(sessions)` (line 81)
- `async def collect_for_sessions(sessions)` (line 100)
- `def format_snapshots(snapshots)` (line 125)
