# API Spec: `sessions/session_run_service.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class SessionRunService` (line 38)
- `def __init__()` (line 39)
- `def start_session_task(session)` (line 131)
- `def start_prompt_task(session, prompt, dest, context)` (line 147)
- `async def run_prompt(session, prompt, dest, context)` (line 382)
- `async def run_mode_pipeline(session, prompt, dest, context)` (line 547)
- `async def dispatch_queued_input(session, next_prompt, next_dest, context)` (line 703)
- `def start_mode_task(session, prompt, dest, context)` (line 733)
