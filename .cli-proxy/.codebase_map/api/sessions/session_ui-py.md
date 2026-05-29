# API Spec: `sessions/session_ui.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class SessionUI` (line 24)
- `def __init__(config, manager, send_message, edit_message, format_ts, short_label, on_close, on_before_close, mode_registry, is_session_allowed, bot_app)` (line 25)
- `def build_sessions_menu(chat_id, include_back, back_callback, back_text)` (line 135)
- `async def handle_pending_message(chat_id, text, context, message_thread_id)` (line 155)
- `async def handle_callback(query, chat_id, context)` (line 214)
