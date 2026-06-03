# API Spec: `modes/admin/chat_memory.py`

Generated: 2026-06-03T02:24:28Z

## Classes
### `class ChatMessage` (line 22)
- `def as_dict()` (line 29)
- `def from_dict(cls, data)` (line 42)

### `class ChatMemoryError(RuntimeError)` (line 66)

### `class ChatMemory` (line 70)
*Per-session admin chat memory: rolling message history + free-form MEMORY.md.*
- `def __init__(workdir)` (line 73)
- `def memory_json_path()` (line 89)
- `def memory_md_path()` (line 93)
- `def load_messages()` (line 96)
- `def append()` (line 123)
- `def tail(n)` (line 152)
- `def clear()` (line 158)
- `def read_memory_md()` (line 175)
- `def append_memory_md(text)` (line 183)
- `def overwrite_memory_md(text)` (line 197)

### `class ChatPendingStore` (line 209)
*Per-session storage for pending chat approvals (one file per approval).*
- `def __init__(workdir)` (line 212)
- `def dir_path()` (line 220)
- `def save(approval_id, payload)` (line 229)
- `def get(approval_id)` (line 239)
- `def pop(approval_id)` (line 256)
- `def list_ids()` (line 266)
- `def list_pending()` (line 275)

## Symbols
- `def chat_dir(workdir)` (line 54)
- `def memory_json_path(workdir)` (line 58)
- `def memory_md_path(workdir)` (line 62)
