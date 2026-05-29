# API Spec: `modes/admin/chat_gateway.py`

Generated: 2026-04-27T22:43:22Z

## Classes
### `class ChatDecision` (line 66)
*Result of a single chat-gateway turn.*
- `def as_dict()` (line 77)

### `class PendingApproval` (line 94)
*Serialized pending-approval record stored alongside chat history.*
- `def as_dict()` (line 104)

### `class AdminChatGatewayError(RuntimeError)` (line 115)

### `class AdminChatGateway` (line 119)
*Turn free-form admin text into a validated intent + reply.*
- `def __init__()` (line 122)
- `def memory()` (line 149)
- `def load_prompts()` (line 152)
- `def build_system_prompt()` (line 168)
- `def build_user_prompt(user_text)` (line 175)
- `async def handle(user_text)` (line 194)
