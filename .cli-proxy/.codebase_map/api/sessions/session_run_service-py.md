# API Spec: `sessions/session_run_service.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class SessionRunService` (line 42)
- `def __init__()` (line 43)
- `def start_session_task(session)` (line 150)
- `def start_prompt_task(session, prompt, dest, context)` (line 166)
- `async def run_prompt(session, prompt, dest, context)` (line 443)
- `async def run_mode_pipeline(session, prompt, dest, context)` (line 611)
- `async def dispatch_queued_input(session, next_prompt, next_dest, context)` (line 773)
- `def start_mode_task(session, prompt, dest, context)` (line 793)
