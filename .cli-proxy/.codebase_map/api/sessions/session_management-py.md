# API Spec: `sessions/session_management.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class PendingInput` (line 31)

### `class SessionManagement` (line 39)
*Class containing session management functionality for the Telegram bot.*
- `def __init__(bot_app)` (line 44)
- `def set_html_process_pool(pool)` (line 114)
- `async def send_output(session, dest, output, context)` (line 232)
- `async def run_prompt(session, prompt, dest, context)` (line 255)
- `def start_prompt_task(session, prompt, dest, context)` (line 264)
- `async def run_mode_pipeline(session, prompt, dest, context)` (line 283)
- `async def run_prompt_raw(prompt, session_id)` (line 347)
- `def format_interrupt_user_message(report, lang)` (line 433)
- `async def interrupt_session_runtime(session)` (line 440)
- `def interrupt_session_now(session)` (line 459)
- `async def ensure_scope_session(chat_id, context)` (line 520)
- `def start_mode_task(session, prompt, dest, context)` (line 569)
