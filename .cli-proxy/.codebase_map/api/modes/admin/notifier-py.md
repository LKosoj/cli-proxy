# API Spec: `modes/admin/notifier.py`

Generated: 2026-06-03T02:24:29Z

## Classes
### `class AdminNotifierError(RuntimeError)` (line 10)
*Raised when notifier input is invalid.*

### `class AdminNotificationResult` (line 15)

### `class _AdminNotifierStateStore(Protocol)` (line 22)
- `def get_session_state(session_id)` (line 23)

### `class _AdminNotifierMessaging(Protocol)` (line 27)
- `async def send_text(chat_id, text)` (line 28)

### `class AdminNotifier` (line 43)
- `def __init__()` (line 44)
- `async def notify_incident()` (line 53)
- `async def notify_action()` (line 70)
