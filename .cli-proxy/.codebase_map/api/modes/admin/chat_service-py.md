# API Spec: `modes/admin/chat_service.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class AutopilotVerdict` (line 35)

### `class AdminChatService` (line 43)
*Pure service encapsulating admin-chat operations for both Telegram and UI clients.*
- `def __init__()` (line 50)
- `def list_messages(workdir)` (line 66)
- `def list_pending(workdir)` (line 71)
- `def get_memory_md(workdir)` (line 76)
- `def save_memory_md(workdir)` (line 80)
- `def counters(workdir)` (line 84)
- `def reject_pending(workdir)` (line 95)
- `def build_gateway()` (line 123)
- `async def send()` (line 157)
- `async def execute_pending()` (line 332)
