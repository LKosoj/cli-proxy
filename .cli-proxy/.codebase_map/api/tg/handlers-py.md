# API Spec: `tg/handlers.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class TelegramRuntimePayload(BaseModel)` (line 66)

### `class BotHandlers` (line 79)
*Class containing command handlers for the Telegram bot.*
- `def __init__(bot_app)` (line 84)
- `async def notify_pending_selfupdate(application)` (line 247)
- `def build_user_project_picker(owner_chat_id)` (line 594)
- `def build_sessions_active_overview(chat_id)` (line 680)
- `async def show_new_session_menu(chat_id, context, edit_message, reply_kwargs)` (line 791)
- `async def cmd_tools(update, context)` (line 842)
- `async def cmd_new(update, context)` (line 860)
- `async def cmd_newpath(update, context)` (line 891)
- `async def cmd_sessions(update, context)` (line 936)
- `async def cmd_close(update, context)` (line 952)
- `async def cmd_status(update, context)` (line 986)
- `async def cmd_limits(update, context)` (line 995)
- `async def cmd_mode(update, context, mode_id)` (line 1038)
- `async def cmd_interrupt(update, context)` (line 1148)
- `async def cmd_queue(update, context)` (line 1177)
- `async def cmd_clearqueue(update, context)` (line 1189)
- `async def cmd_rename(update, context)` (line 1200)
- `async def cmd_dirs(update, context)` (line 1252)
- `async def cmd_cwd(update, context)` (line 1271)
- `async def cmd_git(update, context)` (line 1317)
- `async def cmd_selfupdate(update, context)` (line 1346)
- `async def cmd_setprompt(update, context)` (line 1516)
- `async def cmd_resume(update, context)` (line 1592)
- `async def cmd_state(update, context)` (line 1608)
- `async def cmd_send(update, context)` (line 1690)
- `async def set_bot_commands(app)` (line 1723)
- `async def cmd_files(update, context)` (line 1735)
- `async def cmd_preset(update, context)` (line 1920)
- `async def cmd_metrics(update, context)` (line 1936)
- `async def cmd_lint_evolution_status(update, context)` (line 1966)
- `async def cmd_lint_autopause_resume(update, context)` (line 2016)
- `async def cmd_lint_schema_history(update, context)` (line 2058)
- `async def cmd_lint_gate_dry_run(update, context)` (line 2086)

## Symbols
- `def format_session_state(st, updated_at_str)` (line 53)
  - *Форматирует объект SessionState в читаемую строку для отображения в Telegram.*
