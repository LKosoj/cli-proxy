# API Spec: `sessions/session_ui.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class SessionUI` (line 27)
- `def __init__(config, manager, send_message, edit_message, format_ts, short_label, on_close, on_before_close, mode_registry, is_session_allowed, bot_app)` (line 28)
- `def build_sessions_menu(chat_id, include_back, back_callback, back_text)` (line 138)
- `async def handle_pending_message(chat_id, text, context, message_thread_id)` (line 159)
- `async def handle_callback(query, chat_id, context)` (line 220)
