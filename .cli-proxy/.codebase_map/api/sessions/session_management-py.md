# API Spec: `sessions/session_management.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class PendingInput` (line 29)

### `class SessionManagement` (line 37)
*Class containing session management functionality for the Telegram bot.*
- `def __init__(bot_app)` (line 42)
- `def set_html_process_pool(pool)` (line 112)
- `async def send_output(session, dest, output, context)` (line 230)
- `async def run_prompt(session, prompt, dest, context)` (line 253)
- `def start_prompt_task(session, prompt, dest, context)` (line 262)
- `async def run_mode_pipeline(session, prompt, dest, context)` (line 281)
- `async def run_prompt_raw(prompt, session_id)` (line 345)
- `def format_interrupt_user_message(report)` (line 431)
- `async def interrupt_session_runtime(session)` (line 438)
- `def interrupt_session_now(session)` (line 457)
- `async def ensure_scope_session(chat_id, context)` (line 518)
- `def start_mode_task(session, prompt, dest, context)` (line 566)
