# API Spec: `sessions/session_management.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class PendingInput` (line 28)

### `class SessionManagement` (line 36)
*Class containing session management functionality for the Telegram bot.*
- `def __init__(bot_app)` (line 41)
- `def set_html_process_pool(pool)` (line 111)
- `async def send_output(session, dest, output, context)` (line 229)
- `async def run_prompt(session, prompt, dest, context)` (line 252)
- `def start_prompt_task(session, prompt, dest, context)` (line 261)
- `async def run_mode_pipeline(session, prompt, dest, context)` (line 280)
- `async def run_prompt_raw(prompt, session_id)` (line 344)
- `def format_interrupt_user_message(report)` (line 430)
- `async def interrupt_session_runtime(session)` (line 437)
- `def interrupt_session_now(session)` (line 456)
- `async def ensure_scope_session(chat_id, context)` (line 517)
- `def start_mode_task(session, prompt, dest, context)` (line 565)
