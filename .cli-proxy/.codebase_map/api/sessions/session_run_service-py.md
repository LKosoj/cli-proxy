# API Spec: `sessions/session_run_service.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class SessionRunService` (line 40)
- `def __init__()` (line 41)
- `def start_session_task(session)` (line 142)
- `def start_prompt_task(session, prompt, dest, context)` (line 158)
- `async def run_prompt(session, prompt, dest, context)` (line 436)
- `async def run_mode_pipeline(session, prompt, dest, context)` (line 604)
- `async def dispatch_queued_input(session, next_prompt, next_dest, context)` (line 766)
- `def start_mode_task(session, prompt, dest, context)` (line 786)
