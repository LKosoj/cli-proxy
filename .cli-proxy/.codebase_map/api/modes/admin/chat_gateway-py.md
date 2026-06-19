# API Spec: `modes/admin/chat_gateway.py`

Generated: 2026-06-17T10:46:18Z

## Classes
### `class ChatDecision` (line 67)
*Result of a single chat-gateway turn.*
- `def as_dict()` (line 78)

### `class PendingApproval` (line 95)
*Serialized pending-approval record stored alongside chat history.*
- `def as_dict()` (line 105)

### `class AdminChatGatewayError(RuntimeError)` (line 116)

### `class AdminChatGateway` (line 120)
*Turn free-form admin text into a validated intent + reply.*
- `def __init__()` (line 123)
- `def memory()` (line 150)
- `def load_prompts()` (line 153)
- `def build_system_prompt()` (line 170)
- `def build_user_prompt(user_text)` (line 177)
- `async def handle(user_text, lang)` (line 196)
