# API Spec: `app/services/cli_limits_service.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class CliProjectRef` (line 27)

### `class CliLimitsSnapshot` (line 34)

### `class CliLimitsService` (line 41)
*Собирает доступные лимиты и usage по активным CLI-сессиям.*
- `def __init__()` (line 55)
- `async def describe_for_sessions(sessions)` (line 72)
- `async def collect_for_sessions(sessions)` (line 91)
- `def format_snapshots(snapshots)` (line 116)
