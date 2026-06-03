# API Spec: `modes/admin/chat_service.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class AutopilotVerdict` (line 34)

### `class AdminChatService` (line 42)
*Pure service encapsulating admin-chat operations for both Telegram and UI clients.*
- `def __init__()` (line 49)
- `def list_messages(workdir)` (line 65)
- `def list_pending(workdir)` (line 70)
- `def get_memory_md(workdir)` (line 75)
- `def save_memory_md(workdir)` (line 79)
- `def counters(workdir)` (line 83)
- `def reject_pending(workdir)` (line 94)
- `def build_gateway()` (line 122)
- `async def send()` (line 156)
- `async def execute_pending()` (line 328)
