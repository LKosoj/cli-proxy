# API Spec: `tg/handlers.py`

Generated: 2026-04-27T22:43:23Z

## Classes
### `class PendingInput` (line 44)

### `class TelegramRuntimePayload(BaseModel)` (line 54)

### `class BotHandlers` (line 67)
*Class containing command handlers for the Telegram bot.*
- `def __init__(bot_app)` (line 72)
- `async def notify_pending_selfupdate(application)` (line 215)
- `def build_user_project_picker(owner_chat_id)` (line 550)
- `def build_sessions_active_overview(chat_id)` (line 636)
- `async def show_new_session_menu(chat_id, context, edit_message, reply_kwargs)` (line 747)
- `async def cmd_tools(update, context)` (line 798)
- `async def cmd_new(update, context)` (line 816)
- `async def cmd_newpath(update, context)` (line 847)
- `async def cmd_sessions(update, context)` (line 892)
- `async def cmd_close(update, context)` (line 908)
- `async def cmd_status(update, context)` (line 942)
- `async def cmd_limits(update, context)` (line 951)
- `async def cmd_mode(update, context, mode_id)` (line 994)
- `async def cmd_interrupt(update, context)` (line 1103)
- `async def cmd_queue(update, context)` (line 1132)
- `async def cmd_clearqueue(update, context)` (line 1144)
- `async def cmd_rename(update, context)` (line 1155)
- `async def cmd_dirs(update, context)` (line 1207)
- `async def cmd_cwd(update, context)` (line 1226)
- `async def cmd_git(update, context)` (line 1272)
- `async def cmd_selfupdate(update, context)` (line 1301)
- `async def cmd_setprompt(update, context)` (line 1471)
- `async def cmd_resume(update, context)` (line 1494)
- `async def cmd_state(update, context)` (line 1510)
- `async def cmd_send(update, context)` (line 1600)
- `async def set_bot_commands(app)` (line 1633)
- `async def cmd_files(update, context)` (line 1645)
- `async def cmd_preset(update, context)` (line 1783)
- `async def cmd_metrics(update, context)` (line 1799)
- `async def cmd_lint_evolution_status(update, context)` (line 1829)
- `async def cmd_lint_autopause_resume(update, context)` (line 1879)
- `async def cmd_lint_schema_history(update, context)` (line 1921)
- `async def cmd_lint_gate_dry_run(update, context)` (line 1949)
